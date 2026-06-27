"""Platform-specific forum scrapers with pagination and full-thread fetch."""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_DISCOURSE_TOPIC_HREF_RE = re.compile(r"^/t/[^/]+/\d+")
_DISCOURSE_TOPIC_IN_HTML_RE = re.compile(r'href="(/t/[^"]+/\d+)"')
_V2EX_TOPIC_HREF_RE = re.compile(r"^/t/(\d{6,})$")
_V2EX_TOPIC_RE = re.compile(r"/t/(\d{6,})")
_NODESEEK_POST_RE = re.compile(r"/post-(\d+)-\d+")


@dataclass(frozen=True)
class TopicEntry:
    ref: str
    title: str = ""
    score: int = 0
    prefetched_text: str = ""


class ForumAdapter(ABC):
    platform: str

    def __init__(
        self,
        base_url: str,
        cookie: str,
        request_fn,
        delay: float,
        site_config: dict | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie
        self._request = request_fn
        self.delay = delay
        self.site_config = site_config or {}
        self._loaded_feeds: set[str] = set()

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
            if not self.cookie:
                return []
            return self._search_topics(entry_url, max_topics=max_topics)

        feed_urls = list(self.site_config.get("feed_urls") or [])
        rss_url = self._rss_url_for_entry(entry_url)
        if rss_url and rss_url not in feed_urls:
            feed_urls.append(rss_url)

        entries: list[TopicEntry] = []
        seen: set[str] = set()

        for feed_url in feed_urls:
            if feed_url in self._loaded_feeds:
                continue
            self._loaded_feeds.add(feed_url)
            self._sleep()
            for entry in self._discover_from_rss(feed_url, max_topics=max_topics):
                if entry.ref in seen:
                    continue
                seen.add(entry.ref)
                entries.append(entry)
                if len(entries) >= max_topics:
                    return entries

        skip_html = not self.cookie and bool(self.site_config.get("feed_urls"))
        if skip_html:
            return entries

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
        if response.status_code == 200:
            return self._parse_json_thread(response.json(), max_posts=max_posts)

        rss_text = self._fetch_thread_from_rss(topic_ref)
        if rss_text.strip():
            return rss_text
        raise RuntimeError(f"HTTP {response.status_code}")

    def _parse_json_thread(self, data: dict, *, max_posts: int) -> str:
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

    def _fetch_thread_from_rss(self, topic_ref: str) -> str:
        rss_url = f"{self.base_url}{topic_ref.rstrip('/')}.rss"
        response = self._request(rss_url, accept_rss=True)
        if response.status_code != 200 or "<rss" not in response.text[:800].lower():
            return ""
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            return ""

        chunks: list[str] = []
        for item in root.findall(".//item"):
            title_el = item.find("title")
            desc_el = item.find("description")
            if title_el is not None and title_el.text:
                chunks.append(title_el.text.strip())
            if desc_el is not None and desc_el.text:
                chunks.append(BeautifulSoup(unescape(desc_el.text), "lxml").get_text(separator="\n"))
        return "\n".join(chunks)

    @staticmethod
    def _rss_url_for_entry(entry_url: str) -> str:
        parsed = urlparse(entry_url)
        path = parsed.path.rstrip("/")
        if not path or path.endswith(".rss"):
            return entry_url if entry_url.endswith(".rss") else ""
        if "/search" in entry_url:
            return ""
        for suffix in ("/l/latest", "/latest"):
            if path.endswith(suffix):
                path = path[: -len(suffix)]
                break
        return f"{parsed.scheme}://{parsed.netloc}{path}.rss"

    def _discover_from_rss(self, feed_url: str, *, max_topics: int) -> list[TopicEntry]:
        response = self._request(feed_url, accept_rss=True)
        if response.status_code != 200:
            log.warning("  RSS feed failed %s: HTTP %s", feed_url, response.status_code)
            return []
        if "<rss" not in response.text[:800].lower():
            log.warning("  RSS feed %s: response is not RSS (maybe blocked)", feed_url)
            return []

        entries: list[TopicEntry] = []
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            return []

        for item in root.findall(".//item")[:max_topics]:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            title = (title_el.text or "").strip() if title_el is not None else ""
            link = (link_el.text or "").strip() if link_el is not None else ""
            if not link:
                continue
            ref = urlparse(link).path
            if not _DISCOURSE_TOPIC_HREF_RE.match(ref):
                continue
            description = desc_el.text if desc_el is not None and desc_el.text else ""
            if description:
                description = BeautifulSoup(description, "lxml").get_text(separator="\n")
            entries.append(
                TopicEntry(
                    ref=ref,
                    title=title,
                    prefetched_text=description,
                )
            )
        if entries:
            log.info("  RSS feed %s -> %d topics", feed_url, len(entries))
        return entries

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
        min_topic_id = int(self.site_config.get("min_topic_id", 500000))
        feed_urls = list(self.site_config.get("feed_urls") or [])
        if not feed_urls and "/go/" in entry_url:
            node = urlparse(entry_url).path.rstrip("/").split("/")[-1]
            feed_urls.append(f"{self.base_url}/feed/{node}.json")

        entries: list[TopicEntry] = []
        seen: set[str] = set()

        for feed_url in feed_urls:
            if feed_url in self._loaded_feeds:
                continue
            self._loaded_feeds.add(feed_url)
            for entry in self._discover_from_json_feed(feed_url, min_topic_id=min_topic_id):
                if entry.ref in seen:
                    continue
                seen.add(entry.ref)
                entries.append(entry)
                if len(entries) >= max_topics:
                    return entries

        for page in range(1, max_pages + 1):
            page_url = entry_url if page == 1 else self._page_url(entry_url, page)
            response = self._request(page_url)
            if response.status_code != 200:
                break
            page_entries = self._discover_from_html(response.text, min_topic_id=min_topic_id)
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

    def _discover_from_json_feed(self, feed_url: str, *, min_topic_id: int) -> list[TopicEntry]:
        response = self._request(feed_url, accept_json=True)
        if response.status_code != 200:
            return []
        try:
            payload = response.json()
        except Exception:
            return []

        items = payload.get("items", []) if isinstance(payload, dict) else []
        entries: list[TopicEntry] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("id") or "")
            match = _V2EX_TOPIC_HREF_RE.match(urlparse(url).path)
            if not match:
                continue
            topic_id = int(match.group(1))
            if topic_id < min_topic_id:
                continue
            content = str(item.get("content_text") or item.get("content_html") or "")
            entries.append(
                TopicEntry(
                    ref=f"/t/{topic_id}",
                    title=str(item.get("title") or ""),
                    prefetched_text=content,
                )
            )
        if entries:
            log.info("  JSON feed %s -> %d topics", feed_url, len(entries))
        return entries

    @staticmethod
    def _discover_from_html(html: str, *, min_topic_id: int) -> list[TopicEntry]:
        entries: list[TopicEntry] = []
        seen: set[str] = set()
        soup = BeautifulSoup(html, "lxml")
        for link in soup.select('a[href^="/t/"]'):
            href = (link.get("href") or "").split("?")[0]
            match = _V2EX_TOPIC_HREF_RE.match(href)
            if not match or href in seen:
                continue
            if int(match.group(1)) < min_topic_id:
                continue
            seen.add(href)
            title = link.get_text(strip=True)
            entries.append(TopicEntry(ref=href, title=title))
        return entries

    def fetch_thread(self, topic_ref: str, *, max_posts: int) -> str:
        topic_id = topic_ref.rstrip("/").split("/")[-1]
        api_url = f"{self.base_url}/api/topics/show.json?id={topic_id}"
        response = self._request(api_url, accept_json=True)
        if response.status_code == 200:
            try:
                text = self._text_from_topic_api(response.json())
                replies_text = self._fetch_replies_from_api(
                    topic_id,
                    max_posts=max(0, max_posts - 1),
                )
                if replies_text:
                    text = f"{text}\n{replies_text}" if text else replies_text
                if text.strip():
                    return text
            except Exception as e:
                log.debug("V2EX API parse failed for %s: %s", topic_ref, e)

        topic_url = f"{self.base_url}{topic_ref}"
        response = self._request(topic_url)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}")
        return self._text_from_html(response.text)

    def _fetch_replies_from_api(self, topic_id: str, *, max_posts: int) -> str:
        if max_posts <= 0:
            return ""

        chunks: list[str] = []
        page = 1
        while len(chunks) < max_posts:
            url = f"{self.base_url}/api/replies/show.json?topic_id={topic_id}&page={page}"
            response = self._request(url, accept_json=True)
            if response.status_code != 200:
                break

            replies = self._unwrap_v2ex_list(response.json())
            if not replies:
                break

            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                content = reply.get("content")
                if content:
                    chunks.append(str(content))
                if len(chunks) >= max_posts:
                    break

            if len(replies) < 50:
                break
            page += 1
            self._sleep()

        return "\n".join(chunks)

    @staticmethod
    def _unwrap_v2ex_root(data) -> dict:
        if isinstance(data, list) and data:
            first = data[0]
            return first if isinstance(first, dict) else {}
        if isinstance(data, dict):
            return data
        return {}

    @staticmethod
    def _unwrap_v2ex_list(data) -> list:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            replies = data.get("replies")
            if isinstance(replies, list):
                return [item for item in replies if isinstance(item, dict)]
        return []

    @staticmethod
    def _text_from_topic_api(data) -> str:
        root = V2exAdapter._unwrap_v2ex_root(data)
        chunks: list[str] = []
        if root.get("title"):
            chunks.append(str(root["title"]))
        if root.get("content"):
            chunks.append(str(root["content"]))
        content_rendered = root.get("content_rendered")
        if content_rendered and not root.get("content"):
            chunks.append(BeautifulSoup(str(content_rendered), "lxml").get_text(separator=" "))
        return "\n".join(chunks)

    @staticmethod
    def _page_url(entry_url: str, page: int) -> str:
        parsed = urlparse(entry_url)
        params = parse_qs(parsed.query)
        params["p"] = [str(page)]
        query = urlencode(params, doseq=True)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{query}"

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


def build_adapter(
    platform: str,
    base_url: str,
    cookie: str,
    request_fn,
    delay: float,
    site_config: dict | None = None,
) -> ForumAdapter:
    adapters: dict[str, type[ForumAdapter]] = {
        "discourse": DiscourseAdapter,
        "v2ex": V2exAdapter,
        "nodeseek": NodeSeekAdapter,
    }
    cls = adapters.get(platform, V2exAdapter)
    return cls(base_url, cookie, request_fn, delay, site_config=site_config)


def score_title(title: str, include: list[str], exclude: list[str]) -> int:
    normalized = title.lower()
    if exclude and any(keyword in normalized for keyword in exclude):
        return -1
    if not include:
        return 1 if title else 0
    hits = sum(1 for keyword in include if keyword in normalized)
    return hits if hits > 0 else -1
