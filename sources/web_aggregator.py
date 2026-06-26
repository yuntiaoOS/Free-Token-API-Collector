"""Scrape free API tokens from known web aggregator sites."""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

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


class WebAggregatorSource(BaseSource):
    """Fetch free API tokens from configurable web pages."""

    @property
    def name(self) -> str:
        return "web_aggregator"

    def collect(self) -> list[DiscoveredToken]:
        url_configs = self.config.get("urls", [])
        if not url_configs:
            log.info("No web_aggregator URLs configured, skipping")
            return []

        tokens: list[DiscoveredToken] = []
        with self._client() as client:
            for entry in url_configs:
                url = entry["url"]
                parser_type = entry.get("parser", "table")
                source_tag = f"web:{urlparse(url).netloc}"
                log.info("Scraping %s (%s)", url, parser_type)
                try:
                    r = client.get(url)
                    if r.status_code != 200:
                        log.warning("  HTTP %d", r.status_code)
                        continue

                    if parser_type == "json":
                        found = self._parse_json(r.text, source_tag)
                    else:
                        found = self._parse_html_table(r.text, source_tag)
                    log.info("  Found %d tokens", len(found))
                    tokens.extend(found)
                except Exception as e:
                    log.warning("  Failed %s: %s", url, e)

        return tokens

    def _parse_html_table(self, html: str, source_tag: str) -> list[DiscoveredToken]:
        soup = BeautifulSoup(html, "lxml")
        results: list[DiscoveredToken] = []
        seen: set[str] = set()

        for table in soup.find_all("table"):
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                row_text = " ".join(c.get_text(separator=" ") for c in cells)
                urls = _URL_RE.findall(row_text)
                keys = _KEY_RE.findall(row_text)
                models = _MODEL_RE.findall(row_text)

                for url in urls:
                    base = self._normalize_url(url)
                    for key in keys:
                        uid = f"{base}|{key}"
                        if uid in seen:
                            continue
                        seen.add(uid)
                        results.append(DiscoveredToken(
                            source=source_tag,
                            base_url=base,
                            api_key=key,
                            raw_models=list(models),
                        ))

        # Fallback: scan all text
        if not results:
            text = soup.get_text(separator=" ")
            for url_m in _URL_RE.finditer(text):
                url = url_m.group(0)
                base = self._normalize_url(url)
                window = text[url_m.start(): url_m.end() + 200]
                for key in _KEY_RE.findall(window):
                    uid = f"{base}|{key}"
                    if uid in seen:
                        continue
                    seen.add(uid)
                    results.append(DiscoveredToken(
                        source=source_tag,
                        base_url=base,
                        api_key=key,
                    ))

        return results

    def _parse_json(self, text: str, source_tag: str) -> list[DiscoveredToken]:
        """Parse JSON API responses. Tries common field name patterns."""
        results: list[DiscoveredToken] = []
        seen: set[str] = set()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return results

        items = data if isinstance(data, list) else data.get("data", data.get("items", [data]))
        if not isinstance(items, list):
            items = [items]

        for item in items:
            if not isinstance(item, dict):
                continue
            url = (
                item.get("base_url")
                or item.get("baseUrl")
                or item.get("url")
                or item.get("endpoint")
                or ""
            )
            key = (
                item.get("api_key")
                or item.get("apiKey")
                or item.get("key")
                or item.get("token")
                or ""
            )
            if not url or not key:
                continue

            base = self._normalize_url(url)
            uid = f"{base}|{key}"
            if uid in seen:
                continue
            seen.add(uid)

            models_raw = item.get("models", [])
            if isinstance(models_raw, str):
                models_raw = [m.strip() for m in models_raw.split(",")]
            elif isinstance(models_raw, list):
                models_raw = [str(m) for m in models_raw]

            results.append(DiscoveredToken(
                source=source_tag,
                base_url=base,
                api_key=key,
                raw_models=models_raw,
            ))

        return results

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.rstrip(".,;:)}]")
        url = url.rstrip("/")
        for suffix in ["/chat/completions", "/messages", "/responses"]:
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)]
        return url
