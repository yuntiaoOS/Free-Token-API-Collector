"""Scrape free API tokens from GitHub repo READMEs."""

from __future__ import annotations

import logging
import re
import time
from base64 import b64decode

from .base import BaseSource, DiscoveredToken

log = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"(?:`|\")?(https?://[^\s\)\]\|\"'<>`,]+)(?:`|\")?",
    re.IGNORECASE,
)
_API_URL_RE = re.compile(
    r"(?:`|\")?(https?://(?:api\.|ai|openai|claude|gpt|chat)[^\s\)\]\|\"'<>`,]*(?:/v1)?)(?:`|\")?",
    re.IGNORECASE,
)
_KEY_RE = re.compile(
    r"(?:`|\")?(sk-[A-Za-z0-9_\-]{20,}|sk-or-v1-[A-Za-z0-9_\-]{20,}|"
    r"sk-proj-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|"
    r"tp-[A-Za-z0-9_\-]{20,}|"
    r"AIza[A-Za-z0-9_\-]{30,}|"
    r"gsk_[A-Za-z0-9_\-]{20,}|"
    r"xai-[A-Za-z0-9_\-]{20,})(?:`|\")?",
)
_MODEL_RE = re.compile(
    r"\b(gpt-[\w.\-]+|claude-[\w.\-]+|deepseek-[\w.\-]+|gemini-[\w.\-]+|"
    r"qwen-[\w.\-]+|mistral-[\w.\-]+|llama-[\w.\-]+|mimo-[\w.\-]+|"
    r"grok-[\w.\-]+|o[1-9][\w.\-]*)\b",
    re.IGNORECASE,
)
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$", re.MULTILINE)
_FAKE_KEY_PATTERNS = [
    re.compile(r"sk-(?:abcdef|123456|abcd1234|1234abcd|5678efgh)"),
    re.compile(r"sk-[a-f0-9]{4,}$", re.IGNORECASE),
    re.compile(r"(sk-)\1{2,}"),
]


def _is_fake_key(key: str) -> bool:
    """Skip obvious placeholder keys from README examples."""
    if len(set(key[3:])) < 8:
        return True
    return any(pat.search(key) for pat in _FAKE_KEY_PATTERNS)


class GitHubReadmeSource(BaseSource):
    """Parse README.md of configured GitHub repos for API URLs + keys."""

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
                    found = self._extract_tokens(readme, source_tag)
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

    def _extract_tokens(self, text: str, source_tag: str) -> list[DiscoveredToken]:
        """Extract URL + key pairs from README text."""
        results: list[DiscoveredToken] = []
        seen: set[str] = set()

        global_urls: list[str] = []
        for match in _API_URL_RE.finditer(text):
            url = self._normalize_url(match.group(1))
            if url not in global_urls and self._looks_like_api_url(url):
                global_urls.append(url)

        for match in re.finditer(r"`(https?://[^`]+/v1[^`]*)`", text):
            url = self._normalize_url(match.group(1))
            if url not in global_urls and self._looks_like_api_url(url):
                global_urls.append(url)

        for row_match in _TABLE_ROW_RE.finditer(text):
            row_text = row_match.group(1)
            urls = [self._normalize_url(url) for url in _URL_RE.findall(row_text)]
            urls = [url for url in urls if self._looks_like_api_url(url)]
            keys = [key for key in _KEY_RE.findall(row_text) if not _is_fake_key(key)]
            models = list(_MODEL_RE.findall(row_text))

            if not urls and global_urls and keys:
                urls = [global_urls[0]]

            for url in urls:
                for key in keys:
                    self._append_result(results, seen, source_tag, url, key, models)

        if not results:
            for url_match in _API_URL_RE.finditer(text):
                url = self._normalize_url(url_match.group(1))
                if not self._looks_like_api_url(url):
                    continue
                window = text[url_match.start(): url_match.end() + 200]
                keys = [key for key in _KEY_RE.findall(window) if not _is_fake_key(key)]
                models = list(_MODEL_RE.findall(window))
                for key in keys:
                    self._append_result(results, seen, source_tag, url, key, models)

        if not results and global_urls:
            for key_match in _KEY_RE.finditer(text):
                key = key_match.group(1)
                if _is_fake_key(key):
                    continue
                self._append_result(results, seen, source_tag, global_urls[0], key, [])

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
        if not key:
            return
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
    def _looks_like_api_url(url: str) -> bool:
        lower = url.lower()
        return any(
            keyword in lower
            for keyword in (
                "/v1",
                "/api",
                "api.",
                "openai",
                "claude",
                "gpt",
                "chat",
                "aiapiv2",
                "freetheai",
                "okrouter",
                "chatanywhere",
                "relay",
                "router",
                "proxy",
                "llm",
            )
        )

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = url.rstrip(".,;:)}]").rstrip("/")
        for suffix in ("/chat/completions", "/messages", "/responses", "/models", "/health"):
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)]
        return url
