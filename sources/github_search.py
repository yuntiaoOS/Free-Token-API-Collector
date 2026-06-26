"""Discover free API repos via GitHub Search API, then scrape their READMEs."""

from __future__ import annotations

import logging
import time

from .base import BaseSource, DiscoveredToken
from .github_readme import GitHubReadmeSource

log = logging.getLogger(__name__)


class GitHubSearchSource(BaseSource):
    """Search GitHub for repos sharing free API keys, then parse their READMEs."""

    @property
    def name(self) -> str:
        return "github_search"

    def collect(self) -> list[DiscoveredToken]:
        queries = self.config.get("queries", [])
        max_repos = self.config.get("max_repos", 10)
        gh_token = self.config.get("_github_token", "")
        delay = self.config.get("_request_delay", 2)

        discovered_repos: list[dict] = []
        seen_ids: set[int] = set()

        with self._client() as client:
            headers = {"Accept": "application/vnd.github.v3+json"}
            if gh_token:
                headers["Authorization"] = f"token {gh_token}"

            for q in queries:
                log.info("GitHub search: %s", q)
                try:
                    r = client.get(
                        "https://api.github.com/search/repositories",
                        params={"q": q, "sort": "stars", "order": "desc", "per_page": max_repos},
                        headers=headers,
                    )
                    if r.status_code != 200:
                        log.warning("  Search failed (%d): %s", r.status_code, r.text[:200])
                        time.sleep(delay)
                        continue

                    items = r.json().get("items", [])
                    for it in items:
                        rid = it["id"]
                        if rid in seen_ids:
                            continue
                        seen_ids.add(rid)
                        discovered_repos.append({
                            "owner": it["owner"]["login"],
                            "repo": it["name"],
                            "stars": it["stargazers_count"],
                        })
                    log.info("  Found %d repos (total unique: %d)", len(items), len(discovered_repos))
                except Exception as e:
                    log.warning("  Search error: %s", e)
                time.sleep(delay)

        # Now scrape each discovered repo's README using the github_readme source
        readme_cfg = {
            "repos": discovered_repos,
            "_github_token": gh_token,
            "_request_delay": delay,
        }
        readme_source = GitHubReadmeSource(readme_cfg, {
            "proxy": self.proxy,
            "timeout_seconds": self.timeout,
            "user_agent": self.user_agent,
        })
        return readme_source.collect()
