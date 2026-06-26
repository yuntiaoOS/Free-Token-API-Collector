"""Platform-specific forum scrapers with pagination and full-thread fetch."""

from __future__ import annotations

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_DISCOURSE_TOPIC_HREF_RE = re.compile(r"^/t/[^/]+/\d+")
_DISCOURSE_TOPIC_IN_HTML_RE = re.compile(r'href="(/t/[^"]+/\d+)"')
_V2EX_TOPIC_RE = re.compile(r"/t/(\d+)")
_NODESEEK_POST_RE = re.compile(r"/post-(\d+)-\d+")


@dataclass(frozen=True)
class TopicEntry:
    ref: str
    title: str = ""
    score: int = 0


class ForumAdapter(ABC):
    platform: str

    def __init__(self, base_url: str, cookie: str, request_fn, delay: float):
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self._request = request_fn
        self.delay = delay

    @abstractmethod
    def discover(self, entry_url: str, *, max_pages: int, max_topics: int) -> list[TopicEntry]:
        ...

    @abstractmethod
    def fetch_thread(self, topic_ref: str, *, max_posts: int) -> str:
        ...

    def _sleep(self) -> None:
        if self.delay > 0:
            time.sleep(self.delay)


class DiscourseAdapter(ForumAdapter):
    platform = "discourse"

    def discover(self, entry_url: str, *, max_pages: int, max_topics: int) -> list[TopicEntry]:
        if "/search" in entry_url:
            return self._search_topics(entry_url, max_topics=max_topics)

        entries: list[TopicEntry] = []
        seen: set[str] = set()
        for page in range(max_pages):
            page_entries = self._listing_page(entry_url, page=page)
            if not page_entries:
                break
            for entry in page_entries:
                if entry.ref in seen:
                    continue
                seen.add(entry.ref)
                entries.append(entry)
                if len(entries) >= max_topics:
                    return entries
            self._sleep()
        return entries

    def fetch_thread(self, topic_ref: str, *, max_posts: int) -> str:
        json_url = f"{self.base_url}{topic_ref}.json"
        response = self._request(json_url, accept_json=True)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")

        data = response.json()
        post_stream = data.get("post_stream", {})
        posts = list(post_stream.get("posts", []))
        stream_ids = post_stream.get("stream", [])

        fetched_ids = {p.get("id") for p in posts}
        missing = [pid for pid in stream_ids if pid not in fetched_ids][: max(0, max_posts - len(posts))]
        if missing:
            posts.extend(self._fetch_posts_by_ids(missing))

        chunks: list[str] = []
        title = str(data.get("title") or "")
        if title:
            chunks.append(title)
        for post in posts[:max_posts]:
            cooked = post.get("cooked", "")
            if cooked:
                chunks.append(unescape(BeautifulSoup(cooked, "lxml").get_text(separator=" ")))
            raw = post.get("raw", "")
            if raw:
                chunks.append(raw)
        return "\n".join(chunks)

    def _fetch_posts_by_ids(self, post_ids: list[int]) -> list[dict]:
        if not post_ids:
            return []
        # Discourse allows batch fetch in chunks of ~20
        results: list[dict] = []
        chunk_size = 20
        for i in range(0, len(post_ids), chunk_size):
            chunk = post_ids[i : i + chunk_size]
            query = urlencode([("post_ids[]", pid) for pid in chunk])
            url = f"{self.base_url}/posts.json?{query}"
            response = self._request(url, accept_json=True)
            if response.status_code != 200:
                log.debug("Failed batch posts fetch: HTTP %s", response.status_code)
                continue
            payload = response.json()
            batch = payload.get("post_stream", {}).get("posts") or payload.get("posts") or []
            if isinstance(batch, list):
                results.extend(batch)
            self._sleep()
        return results

    def _listing_page(self, entry_url: str, *, page: int) -> list[TopicEntry]:
        parsed = urlparse(entry_url)
        path = parsed.path.rstrip("/")
        suffix = f"{path}.json"
        if page > 0:
            suffix += f"?page={page + 1}"
        json_url = f"{self.base_url}{suffix}"
        response = self._request(json_url, accept_json=True)
        if response.status_code != 200:
            if page == 0:
                return self._listing_html(entry_url)
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

    def _listing_html(self, entry_url: str) -> list[TopicEntry]:
        response = self._request(entry_url)
        if response.status_code != 200:
            log.warning("  HTTP %d for %s", response.status_code, entry_url)
            return []
        return self._parse_html_listing(response.text)

    @staticmethod
    def _parse_html_listing(html: str) -> list[TopicEntry]:
        entries: list[TopicEntry] = []
        seen: set[str] = set()
        soup = BeautifulSoup(html, "lxml")
        for link in soup.select('a[href*="/t/"]'):
            href = (link.get("href") or "").split("?")[0]
            if not _DISCOURSE_TOPIC_HREF_RE.match(href) or href in seen:
                continue
            seen.add(href)
            entries.append(TopicEntry(ref=href, title=link.get_text(strip=True)))
        if entries:
            return entries
        return [TopicEntry(ref=ref) for ref in dict.fromkeys(_DISCOURSE_TOPIC_IN_HTML_RE.findall(html))]

    def _search_topics(self, entry_url: str, *, max_topics: int) -> list[TopicEntry]:
        parsed = urlparse(entry_url)
        params = parse_qs(parsed.query)
        query = params.get("q", params.get("query", [""]))[0]
        search_url = f"{self.base_url}/search.json?q={query}"
        response = self._request(search_url, accept_json=True)
        if response.status_code != 200:
            log.warning("  HTTP %d for %s", response.status_code, search_url)
            return []

        topics = response.json().get("topics", [])
        entries: list[TopicEntry] = []
        for topic in topics[:max_topics]:
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


class V2exAdapter(ForumAdapter):
    platform = "v2ex"

    def discover(self, entry_url: str, *, max_pages: int, max_topics: int) -> list[TopicEntry]:
        entries: list[TopicEntry] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            page_url = entry_url if page == 1 else self._page_url(entry_url, page)
            response = self._request(page_url)
            if response.status_code != 200:
                break
            ids = _V2EX_TOPIC_RE.findall(response.text)
            if not ids:
                break
            for topic_id in dict.fromkeys(ids):
                ref = f"/t/{topic_id}"
                if ref in seen:
                    continue
                seen.add(ref)
                entries.append(TopicEntry(ref=ref))
                if len(entries) >= max_topics:
                    return entries
            self._sleep()
        return entries

    def fetch_thread(self, topic_ref: str, *, max_posts: int) -> str:
        topic_id = topic_ref.rstrip("/").split("/")[-1]
        api_url = f"{self.base_url}/api/topics/show.json?id={topic_id}"
        response = self._request(api_url, accept_json=True)
        if response.status_code == 200:
            return self._text_from_api(response.json(), max_posts=max_posts)

        topic_url = f"{self.base_url}{topic_ref}"
        response = self._request(topic_url)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        return self._text_from_html(response.text)

    @staticmethod
    def _page_url(entry_url: str, page: int) -> str:
        parsed = urlparse(entry_url)
        params = parse_qs(parsed.query)
        params["p"] = [str(page)]
        query = urlencode(params, doseq=True)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{query}"

    @staticmethod
    def _text_from_api(data: dict, *, max_posts: int) -> str:
        chunks: list[str] = []
        root = data[0] if isinstance(data, list) and data else data
        if not isinstance(root, dict):
            return ""
        if root.get("title"):
            chunks.append(str(root["title"]))
        if root.get("content"):
            chunks.append(str(root["content"]))
        for reply in root.get("replies", [])[: max(0, max_posts - 1)]:
            content = reply.get("content")
            if content:
                chunks.append(str(content))
        return "\n".join(chunks)

    @staticmethod
    def _text_from_html(html: str) -> str:
        soup = BeautifulSoup(html, "lxml")
        parts = soup.select(".topic_content, .reply_content, .markdown_body")
        if parts:
            return "\n".join(part.get_text(separator=" ") for part in parts)
        return soup.get_text(separator=" ")


class NodeSeekAdapter(ForumAdapter):
    platform = "nodeseek"

    def discover(self, entry_url: str, *, max_pages: int, max_topics: int) -> list[TopicEntry]:
        entries: list[TopicEntry] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            page_url = entry_url if page == 1 else self._page_url(entry_url, page)
            response = self._request(page_url)
            if response.status_code != 200:
                break
            posts = _NODESEEK_POST_RE.findall(response.text)
            if not posts:
                break
            for post_id in dict.fromkeys(posts):
                ref = f"/post-{post_id}-1"
                if ref in seen:
                    continue
                seen.add(ref)
                entries.append(TopicEntry(ref=ref))
                if len(entries) >= max_topics:
                    return entries
            self._sleep()
        return entries

    def fetch_thread(self, topic_ref: str, *, max_posts: int) -> str:
        topic_url = urljoin(self.base_url + "/", topic_ref.lstrip("/"))
        response = self._request(topic_url)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        soup = BeautifulSoup(response.text, "lxml")
        parts = soup.select(".post-content, .comment-content, .content, article")
        if parts:
            return "\n".join(part.get_text(separator=" ") for part in parts[:max_posts])
        return soup.get_text(separator=" ")

    @staticmethod
    def _page_url(entry_url: str, page: int) -> str:
        parsed = urlparse(entry_url)
        sep = "&" if parsed.query else "?"
        return f"{entry_url}{sep}page={page}"


def build_adapter(platform: str, base_url: str, cookie: str, request_fn, delay: float) -> ForumAdapter:
    adapters: dict[str, type[ForumAdapter]] = {
        "discourse": DiscourseAdapter,
        "v2ex": V2exAdapter,
        "nodeseek": NodeSeekAdapter,
    }
    cls = adapters.get(platform, V2exAdapter)
    return cls(base_url, cookie, request_fn, delay)


def score_title(title: str, include: list[str], exclude: list[str]) -> int:
    normalized = title.lower()
    if exclude and any(keyword in normalized for keyword in exclude):
        return -1
    if not include:
        return 1 if title else 0
    hits = sum(1 for keyword in include if keyword in normalized)
    return hits if hits > 0 else -1
