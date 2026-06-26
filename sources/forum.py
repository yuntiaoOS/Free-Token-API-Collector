"""Scrape free API tokens from community forum sites."""

from __future__ import annotations

import logging
import time
from urllib.parse import urljoin, urlparse

from .base import BaseSource, DiscoveredToken
from .forum_adapters import TopicEntry, build_adapter, score_title
from .token_extract import TokenExtractor

log = logging.getLogger(__name__)


class ForumSource(BaseSource):
    """Collect tokens via platform adapters, pagination, and smart extraction."""

    def __init__(self, config: dict, network_config: dict):
        super().__init__(config, network_config)
        self._extractor = TokenExtractor()
        self._retry_count = int(config.get("retry_count", 3))
        self._retry_backoff = float(config.get("retry_backoff_seconds", 2))

    @property
    def name(self) -> str:
        return "forum"

    def collect(self) -> list[DiscoveredToken]:
        sites = self.config.get("sites", [])
        if not sites:
            log.info("No forum sites configured, skipping")
            return []

        delay = float(self.config.get("request_delay_seconds", 2))
        max_topics = int(self.config.get("max_topics_per_entry", 20))
        max_pages = int(self.config.get("max_pages_per_entry", 3))
        max_posts = int(self.config.get("max_posts_per_topic", 50))

        tokens: list[DiscoveredToken] = []
        seen_uids: set[str] = set()

        with self._client() as client:
            for site in sites:
                site_name = site.get("name") or urlparse(site["base_url"]).netloc
                base_url = site["base_url"].rstrip("/")
                platform = site.get("platform", "v2ex")
                cookie = site.get("cookie", "")
                source_tag = f"forum:{site_name}"
                entry_urls = site.get("entry_urls", [base_url])
                site_max_topics = int(site.get("max_topics_per_entry", max_topics))
                site_max_pages = int(site.get("max_pages_per_entry", max_pages))
                site_max_posts = int(site.get("max_posts_per_topic", max_posts))
                title_include = [str(x).lower() for x in site.get("topic_title_include", [])]
                title_exclude = [str(x).lower() for x in site.get("topic_title_exclude", [])]

                adapter = build_adapter(
                    platform,
                    base_url,
                    cookie,
                    lambda url, accept_json=False: self._request_with_retry(
                        client, url, cookie, accept_json=accept_json
                    ),
                    delay,
                )

                log.info("Scraping forum %s (%s)", site_name, platform)
                topic_entries = self._collect_topics(
                    adapter,
                    entry_urls,
                    base_url,
                    title_include,
                    title_exclude,
                    site_max_topics,
                    site_max_pages,
                )
                log.info("  Queued %d topics on %s", len(topic_entries), site_name)

                for entry in topic_entries:
                    try:
                        text = adapter.fetch_thread(entry.ref, max_posts=site_max_posts)
                        found = self._extractor.extract(text, source_tag)
                        if found:
                            log.info(
                                "  Topic %s — %d token(s)%s",
                                entry.ref,
                                len(found),
                                f" | {entry.title[:40]}" if entry.title else "",
                            )
                        for token in found:
                            if token.uid not in seen_uids:
                                seen_uids.add(token.uid)
                                tokens.append(token)
                    except Exception as e:
                        log.warning("  Failed topic %s: %s", entry.ref, e)
                    time.sleep(delay)

        return tokens

    def _collect_topics(
        self,
        adapter,
        entry_urls: list[str],
        base_url: str,
        title_include: list[str],
        title_exclude: list[str],
        max_topics: int,
        max_pages: int,
    ) -> list[TopicEntry]:
        ranked: list[TopicEntry] = []
        seen_refs: set[str] = set()
        skipped_by_title = 0

        for entry_url in entry_urls:
            full_entry = (
                entry_url
                if entry_url.startswith("http")
                else urljoin(base_url + "/", entry_url.lstrip("/"))
            )
            try:
                discovered = adapter.discover(
                    full_entry,
                    max_pages=max_pages,
                    max_topics=max_topics * 2,
                )
            except Exception as e:
                log.warning("  Failed listing %s: %s", full_entry, e)
                continue

            for entry in discovered:
                if entry.ref in seen_refs:
                    continue
                title_score = score_title(entry.title, title_include, title_exclude)
                if title_score < 0:
                    skipped_by_title += 1
                    continue
                seen_refs.add(entry.ref)
                ranked.append(
                    TopicEntry(ref=entry.ref, title=entry.title, score=title_score)
                )

        if skipped_by_title:
            log.info("  Skipped %d off-topic titles", skipped_by_title)

        ranked.sort(key=lambda item: item.score, reverse=True)
        return ranked[:max_topics]

    def _request_with_retry(self, client, url: str, cookie: str, *, accept_json: bool = False):
        headers = self._headers()
        if accept_json:
            headers["Accept"] = "application/json, text/plain, */*"
        if cookie:
            headers["Cookie"] = cookie

        last_response = None
        for attempt in range(self._retry_count):
            response = client.get(url, headers=headers)
            last_response = response
            if response.status_code in (429, 500, 502, 503, 504):
                wait = self._retry_backoff * (attempt + 1)
                log.debug("  Retry %s after HTTP %s (wait %.1fs)", url, response.status_code, wait)
                time.sleep(wait)
                continue
            break

        if last_response and last_response.status_code == 403 and "Just a moment" in last_response.text:
            log.warning(
                "  Cloudflare blocked %s — set cookie in config for this site",
                urlparse(url).netloc,
            )
        return last_response
