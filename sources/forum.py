"""Scrape free API tokens from community forum sites."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import BaseSource, DiscoveredToken

log = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"https?://[^\s\)\]\|\"'<>]+(?:/v1|/v1/|/api|/chat/completions|/messages)[^\s\)\]\|\"'<>]*",
    re.IGNORECASE,
)
_KEY_RE = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{20,}|sk-or-v1-[A-Za-z0-9_\-]{20,}|"
    r"sk-proj-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|"
    r"tp-[A-Za-z0-9_\-]{20,}|"
    r"AIza[A-Za-z0-9_\-]{30,}|"
    r"gsk_[A-Za-z0-9_\-]{20,}|"
    r"xai-[A-Za-z0-9_\-]{20,})\b",
)
_MODEL_RE = re.compile(
    r"\b(gpt-[\w.\-]+|claude-[\w.\-]+|deepseek-[\w.\-]+|gemini-[\w.\-]+|"
    r"qwen-[\w.\-]+|mistral-[\w.\-]+|llama-[\w.\-]+|mimo-[\w.\-]+|"
    r"grok-[\w.\-]+|o[1-9][\w.\-]*)\b",
    re.IGNORECASE,
)
_FAKE_KEY_PATTERNS = [
    re.compile(r"sk-(?:abcdef|123456|abcd1234|1234abcd|5678efgh)"),
    re.compile(r"sk-[a-f0-9]{4,}$", re.IGNORECASE),
    re.compile(r"(sk-)\1{2,}"),
]

_V2EX_TOPIC_RE = re.compile(r"/t/(\d+)")
_NODESEEK_POST_RE = re.compile(r"/post-(\d+)-\d+")
_DISCOURSE_TOPIC_HREF_RE = re.compile(r"^/t/[^/]+/\d+")
_DISCOURSE_TOPIC_IN_HTML_RE = re.compile(r'href="(/t/[^"]+/\d+)"')


@dataclass(frozen=True)
class TopicEntry:
    ref: str
    title: str = ""


def _is_fake_key(key: str) -> bool:
    if len(set(key[3:])) < 8:
        return True
    return any(pat.search(key) for pat in _FAKE_KEY_PATTERNS)


class ForumSource(BaseSource):
    """Collect tokens from forum listing/search pages and their topics."""

    @property
    def name(self) -> str:
        return "forum"

    def collect(self) -> list[DiscoveredToken]:
        sites = self.config.get("sites", [])
        if not sites:
            log.info("No forum sites configured, skipping")
            return []

        delay = self.config.get("request_delay_seconds", 2)
        max_topics = self.config.get("max_topics_per_entry", 20)
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
                site_max_topics = site.get("max_topics_per_entry", max_topics)
                title_include = [str(x).lower() for x in site.get("topic_title_include", [])]
                title_exclude = [str(x).lower() for x in site.get("topic_title_exclude", [])]

                log.info("Scraping forum %s (%s)", site_name, platform)
                topic_refs: list[str] = []
                seen_topics: set[str] = set()
                skipped_by_title = 0

                for entry_url in entry_urls:
                    full_entry = entry_url if entry_url.startswith("http") else urljoin(base_url + "/", entry_url.lstrip("/"))
                    try:
                        found = self._discover_topics(client, platform, base_url, full_entry, cookie)
                        added = 0
                        for entry in found:
                            if added >= site_max_topics:
                                break
                            if entry.ref in seen_topics:
                                continue
                            if not self._title_matches_filters(entry.title, title_include, title_exclude):
                                skipped_by_title += 1
                                continue
                            seen_topics.add(entry.ref)
                            topic_refs.append(entry.ref)
                            added += 1
                    except Exception as e:
                        log.warning("  Failed listing %s: %s", full_entry, e)
                    time.sleep(delay)

                if skipped_by_title:
                    log.info("  Skipped %d off-topic titles on %s", skipped_by_title, site_name)
                log.info("  Discovered %d topics on %s", len(topic_refs), site_name)
                for ref in topic_refs:
                    try:
                        text = self._fetch_topic_text(client, platform, base_url, ref, cookie)
                        for token in self._extract_tokens(text, source_tag):
                            if token.uid not in seen_uids:
                                seen_uids.add(token.uid)
                                tokens.append(token)
                    except Exception as e:
                        log.warning("  Failed topic %s: %s", ref, e)
                    time.sleep(delay)

        return tokens

    def _request(
        self,
        client,
        url: str,
        cookie: str = "",
        *,
        accept_json: bool = False,
    ):
        headers = self._headers()
        if accept_json:
            headers["Accept"] = "application/json, text/plain, */*"
        if cookie:
            headers["Cookie"] = cookie
        response = client.get(url, headers=headers)
        if response.status_code == 403 and "Just a moment" in response.text:
            log.warning(
                "  Cloudflare blocked %s — set cookie in config for this site",
                urlparse(url).netloc,
            )
        return response

    def _discover_topics(
        self,
        client,
        platform: str,
        base_url: str,
        entry_url: str,
        cookie: str,
    ) -> list[TopicEntry]:
        if platform == "discourse" and "/search" in entry_url:
            return self._discourse_search_topics(client, base_url, entry_url, cookie)

        if platform == "discourse":
            json_topics = self._discourse_json_listing_topics(client, base_url, entry_url, cookie)
            if json_topics:
                return json_topics

        response = self._request(client, entry_url, cookie)
        if response.status_code != 200:
            log.warning("  HTTP %d for %s", response.status_code, entry_url)
            return []

        if platform == "v2ex":
            ids = _V2EX_TOPIC_RE.findall(response.text)
            return [TopicEntry(ref=f"/t/{topic_id}") for topic_id in dict.fromkeys(ids)]

        if platform == "discourse":
            return self._discourse_html_listing_topics(response.text)

        if platform == "nodeseek":
            posts = _NODESEEK_POST_RE.findall(response.text)
            return [TopicEntry(ref=f"/post-{post_id}-1") for post_id in dict.fromkeys(posts)]

        return []

    @staticmethod
    def _title_matches_filters(
        title: str,
        include: list[str],
        exclude: list[str],
    ) -> bool:
        normalized = title.lower()
        if exclude and any(keyword in normalized for keyword in exclude):
            return False
        if include and not any(keyword in normalized for keyword in include):
            return False
        return True

    def _discourse_json_listing_topics(
        self,
        client,
        base_url: str,
        entry_url: str,
        cookie: str,
    ) -> list[TopicEntry]:
        parsed = urlparse(entry_url)
        json_url = f"{base_url}{parsed.path.rstrip('/')}.json"
        response = self._request(client, json_url, cookie, accept_json=True)
        if response.status_code != 200:
            return []

        data = response.json()
        topics = data.get("topic_list", {}).get("topics", [])
        entries: list[TopicEntry] = []
        for topic in topics:
            topic_id = topic.get("id")
            slug = topic.get("slug")
            if topic_id and slug:
                entries.append(
                    TopicEntry(
                        ref=f"/t/{slug}/{topic_id}",
                        title=str(topic.get("title") or ""),
                    )
                )
        return entries

    @staticmethod
    def _discourse_html_listing_topics(html: str) -> list[TopicEntry]:
        entries: list[TopicEntry] = []
        seen: set[str] = set()
        soup = BeautifulSoup(html, "lxml")
        for link in soup.select('a[href*="/t/"]'):
            href = (link.get("href") or "").split("?")[0]
            if not _DISCOURSE_TOPIC_HREF_RE.match(href) or href in seen:
                continue
            seen.add(href)
            title = link.get_text(strip=True)
            entries.append(TopicEntry(ref=href, title=title))
        if entries:
            return entries

        for ref in dict.fromkeys(_DISCOURSE_TOPIC_IN_HTML_RE.findall(html)):
            entries.append(TopicEntry(ref=ref))
        return entries

    def _discourse_search_topics(
        self,
        client,
        base_url: str,
        entry_url: str,
        cookie: str,
    ) -> list[TopicEntry]:
        parsed = urlparse(entry_url)
        query = ""
        if parsed.query:
            from urllib.parse import parse_qs

            params = parse_qs(parsed.query)
            query = params.get("q", params.get("query", [""]))[0]

        search_url = f"{base_url}/search.json?q={query}"
        response = self._request(client, search_url, cookie, accept_json=True)
        if response.status_code != 200:
            log.warning("  HTTP %d for %s", response.status_code, search_url)
            return []

        data = response.json()
        topics = data.get("topics", [])
        entries: list[TopicEntry] = []
        for topic in topics:
            topic_id = topic.get("id")
            slug = topic.get("slug")
            if topic_id and slug:
                entries.append(
                    TopicEntry(
                        ref=f"/t/{slug}/{topic_id}",
                        title=str(topic.get("title") or ""),
                    )
                )
        return entries

    def _fetch_topic_text(
        self,
        client,
        platform: str,
        base_url: str,
        topic_ref: str,
        cookie: str,
    ) -> str:
        if platform == "discourse":
            json_url = f"{base_url}{topic_ref}.json"
            response = self._request(client, json_url, cookie, accept_json=True)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            data = response.json()
            chunks: list[str] = []
            for post in data.get("post_stream", {}).get("posts", []):
                cooked = post.get("cooked", "")
                if cooked:
                    chunks.append(unescape(BeautifulSoup(cooked, "lxml").get_text(separator=" ")))
            return "\n".join(chunks)

        topic_url = f"{base_url}{topic_ref}"
        response = self._request(client, topic_url, cookie)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")

        soup = BeautifulSoup(response.text, "lxml")
        if platform == "v2ex":
            parts = soup.select(".topic_content, .reply_content, .markdown_body")
            if parts:
                return "\n".join(part.get_text(separator=" ") for part in parts)

        if platform == "nodeseek":
            parts = soup.select(".post-content, .comment-content, .content, article")
            if parts:
                return "\n".join(part.get_text(separator=" ") for part in parts)

        return soup.get_text(separator=" ")

    def _extract_tokens(self, text: str, source_tag: str) -> list[DiscoveredToken]:
        results: list[DiscoveredToken] = []
        seen: set[str] = set()

        for url_match in _URL_RE.finditer(text):
            base = self._normalize_url(url_match.group(0))
            window = text[url_match.start(): url_match.end() + 300]
            keys = [key for key in _KEY_RE.findall(window) if not _is_fake_key(key)]
            models = list(_MODEL_RE.findall(window))
            for key in keys:
                self._append_result(results, seen, source_tag, base, key, models)

        if not results:
            keys = [key for key in _KEY_RE.findall(text) if not _is_fake_key(key)]
            urls = [self._normalize_url(url) for url in _URL_RE.findall(text)]
            if keys and urls:
                for key in keys:
                    self._append_result(results, seen, source_tag, urls[0], key, [])

        return results

    @staticmethod
    def _append_result(
        results: list[DiscoveredToken],
        seen: set[str],
        source_tag: str,
        url: str,
        key: str,
        models: list[str],
    ) -> None:
        uid = f"{url}|{key}"
        if uid in seen:
            return
        seen.add(uid)
        results.append(
            DiscoveredToken(
                source=source_tag,
                base_url=url,
                api_key=key,
                raw_models=list(models),
            )
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.rstrip(".,;:)}]")
        url = url.rstrip("/")
        for suffix in ["/chat/completions", "/messages", "/responses"]:
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)]
        return url
