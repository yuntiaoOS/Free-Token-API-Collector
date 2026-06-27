"""Scrape free API tokens from GitHub repo READMEs."""

from __future__ import annotations

import logging
import time
from base64 import b64decode

from .base import BaseSource, DiscoveredToken
from .token_extract import TokenExtractor

log = logging.getLogger(__name__)


class GitHubReadmeSource(BaseSource):
    """Parse README.md of configured GitHub repos for API URLs + keys."""

    def __init__(self, config: dict, network_config: dict):
        super().__init__(config, network_config)
        self._extractor = TokenExtractor()

    @property
    def name(self) -> str:
        return "github_readme"

    def collect(self) -> list[DiscoveredToken]:
        repos = self.config.get("repos", [])
        gh_token = self.config.get("_github_token", "")
        tokens: list[DiscoveredToken] = []
        delay = self.config.get("_request_delay", 2)

        with self._client() as client:
            for repo_cfg in repos:
                owner = repo_cfg["owner"]
                repo = repo_cfg["repo"]
                source_tag = f"github:{owner}/{repo}"
                log.info("Scraping %s", source_tag)
                try:
                    readme = self._fetch_readme(client, owner, repo, gh_token)
                    found = self._extractor.extract(readme, source_tag)
                    log.info("  Found %d tokens in %s", len(found), source_tag)
                    tokens.extend(found)
                except Exception as e:
                    log.warning("  Failed %s: %s", source_tag, e)
                time.sleep(delay)

        return tokens

    def _fetch_readme(self, client, owner: str, repo: str, gh_token: str) -> str:
        """Fetch README content, preferring raw content for public repos."""
        for branch in ("main", "master"):
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            response = client.get(raw_url, headers={"User-Agent": self.user_agent})
            if response.status_code == 200:
                return response.text

        headers = {"Accept": "application/vnd.github.v3+json"}
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"

        api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        response = client.get(api_url, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(f"Cannot fetch README for {owner}/{repo}: {response.status_code}")

        data = response.json()
        content = data.get("content", "")
        if data.get("encoding", "base64") == "base64":
            return b64decode(content).decode("utf-8", errors="replace")
        return content

