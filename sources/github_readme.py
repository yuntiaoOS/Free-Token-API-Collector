"""Scrape free API tokens from GitHub repo READMEs."""

from __future__ import annotations

import re
import time
from base64 import b64decode
from urllib.parse import urlparse

from .base import BaseSource, DiscoveredToken
import logging

log = logging.getLogger(__name__)

# Patterns that look like API endpoints
_URL_RE = re.compile(
    r"https?://[^\s\)\]\|\"'<>]+(?:/v1|/v1/|/api|/chat/completions|/messages)[^\s\)\]\|\"'<>]*",
    re.IGNORECASE,
)
# Patterns that look like API keys
_KEY_RE = re.compile(
    r"\b(sk-[A-Za-z0-9_\-]{20,}|sk-or-v1-[A-Za-z0-9_\-]{20,}|"
    r"sk-proj-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|"
    r"tp-[A-Za-z0-9_\-]{20,}|"
    r"AIza[A-Za-z0-9_\-]{30,}|"
    r"gsk_[A-Za-z0-9_\-]{20,}|"
    r"xai-[A-Za-z0-9_\-]{20,})\b",
)
# Model names commonly listed in tables
_MODEL_RE = re.compile(
    r"\b(gpt-[\w.\-]+|claude-[\w.\-]+|deepseek-[\w.\-]+|gemini-[\w.\-]+|"
    r"qwen-[\w.\-]+|mistral-[\w.\-]+|llama-[\w.\-]+|mimo-[\w.\-]+|"
    r"grok-[\w.\-]+|o[1-9][\w.\-]*)\b",
    re.IGNORECASE,
)

# Markdown table row
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$", re.MULTILINE)


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

    # -- HTTP ----------------------------------------------------------

    def _fetch_readme(self, client, owner: str, repo: str, gh_token: str) -> str:
        """Fetch README content via GitHub API (supports both rendered and raw)."""
        headers = {"Accept": "application/vnd.github.v3+json"}
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"

        # Try the repos API for readme
        url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        r = client.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            content = data.get("content", "")
            encoding = data.get("encoding", "base64")
            if encoding == "base64":
                return b64decode(content).decode("utf-8", errors="replace")
            return content

        # Fallback: raw content
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md"
        r2 = client.get(raw_url, headers={"User-Agent": self.user_agent})
        if r2.status_code == 200:
            return r2.text

        # Try master branch
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md"
        r3 = client.get(raw_url, headers={"User-Agent": self.user_agent})
        if r3.status_code == 200:
            return r3.text

        raise RuntimeError(f"Cannot fetch README for {owner}/{repo}: {r.status_code}")

    # -- Extraction ----------------------------------------------------

    def _extract_tokens(self, text: str, source_tag: str) -> list[DiscoveredToken]:
        """Extract (URL, key) pairs from README text."""
        results: list[DiscoveredToken] = []
        seen: set[str] = set()

        # Strategy 1: parse markdown table rows — most repos use tables
        for row_match in _TABLE_ROW_RE.finditer(text):
            row_text = row_match.group(1)
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

        # Strategy 2: loose scan for URL + nearby key (within 200 chars)
        if not results:
            for url_m in _URL_RE.finditer(text):
                url = url_m.group(0)
                base = self._normalize_url(url)
                window = text[url_m.start(): url_m.end() + 200]
                keys = _KEY_RE.findall(window)
                models = _MODEL_RE.findall(window)
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

        # Strategy 3: standalone keys that look like API keys (some repos just list keys)
        if not results:
            for key_m in _KEY_RE.finditer(text):
                key = key_m.group(0)
                uid = f"unknown|{key}"
                if uid in seen:
                    continue
                seen.add(uid)
                # Try to find a nearby URL
                window = text[max(0, key_m.start() - 300): key_m.end()]
                urls = _URL_RE.findall(window)
                base = self._normalize_url(urls[0]) if urls else "https://api.openai.com/v1"
                results.append(DiscoveredToken(
                    source=source_tag,
                    base_url=base,
                    api_key=key,
                ))

        return results

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip trailing punctuation and path noise, keep up to /v1 or /api."""
        url = url.rstrip(".,;:)}]")
        # Remove trailing slashes
        url = url.rstrip("/")
        # If URL ends with a path segment like /chat/completions, strip it
        for suffix in ["/chat/completions", "/messages", "/responses"]:
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)]
        return url
"""Scrape free API tokens from GitHub repo READMEs."""

from __future__ import annotations

import re
import time
from base64 import b64decode
from urllib.parse import urlparse

from .base import BaseSource, DiscoveredToken
import logging

log = logging.getLogger(__name__)

# Patterns that look like API endpoints — relaxed to also match relay domains
_URL_RE = re.compile(
    r"(?:`|\")?(https?://[^\s\)\]\|\"'<>`,]+)(?:`|\")?",
    re.IGNORECASE,
)
# More specific: URLs that look like API endpoints
_API_URL_RE = re.compile(
    r"(?:`|\")?(https?://(?:api\.|ai|openai|claude|gpt|chat)[^\s\)\]\|\"'<>`,]*(?:/v1)?)(?:`|\")?",
    re.IGNORECASE,
)
# Patterns that look like API keys
_KEY_RE = re.compile(
    r"(?:`|\")?(sk-[A-Za-z0-9_\-]{20,}|sk-or-v1-[A-Za-z0-9_\-]{20,}|"
    r"sk-proj-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|"
    r"tp-[A-Za-z0-9_\-]{20,}|"
    r"AIza[A-Za-z0-9_\-]{30,}|"
    r"gsk_[A-Za-z0-9_\-]{20,}|"
    r"xai-[A-Za-z0-9_\-]{20,})(?:`|\")?",
)
# Fake key patterns to skip
_FAKE_KEY_PATTERNS = [
    re.compile(r"sk-(?:abcdef|123456|abcd1234|1234abcd|5678efgh)"),  # obvious placeholders
    re.compile(r"sk-[a-f0-9]{4,}$", re.IGNORECASE),  # hex-only short patterns
    re.compile(r"(sk-)\1{2,}"),  # repeating segments
]
# Model names commonly listed in tables
_MODEL_RE = re.compile(
    r"\b(gpt-[\w.\-]+|claude-[\w.\-]+|deepseek-[\w.\-]+|gemini-[\w.\-]+|"
    r"qwen-[\w.\-]+|mistral-[\w.\-]+|llama-[\w.\-]+|mimo-[\w.\-]+|"
    r"grok-[\w.\-]+|o[1-9][\w.\-]*)\b",
    re.IGNORECASE,
)

# Markdown table row
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$", re.MULTILINE)


def _is_fake_key(key: str) -> bool:
    """Check if a key looks like a placeholder/fake."""
    # Keys with repeating character groups
    if len(set(key[3:])) < 8:  # too few unique chars after prefix
        return True
    for pat in _FAKE_KEY_PATTERNS:
        if pat.search(key):
            return True
    return False


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

    # -- HTTP ----------------------------------------------------------

    def _fetch_readme(self, client, owner: str, repo: str, gh_token: str) -> str:
        """Fetch README content via GitHub raw content."""
        # Try raw content first (more reliable, no rate limit for public repos)
        for branch in ["main", "master"]:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            r = client.get(raw_url, headers={"User-Agent": self.user_agent})
            if r.status_code == 200:
                return r.text

        # Fallback: API (needs auth for higher rate limits)
        headers = {"Accept": "application/vnd.github.v3+json"}
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"

        url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        r = client.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            content = data.get("content", "")
            encoding = data.get("encoding", "base64")
            if encoding == "base64":
                return b64decode(content).decode("utf-8", errors="replace")
            return content

        raise RuntimeError(f"Cannot fetch README for {owner}/{repo}: {r.status_code}")

    # -- Extraction ----------------------------------------------------

    def _extract_tokens(self, text: str, source_tag: str) -> list[DiscoveredToken]:
        """Extract (URL, key) pairs from README text."""
        results: list[DiscoveredToken] = []
        seen: set[str] = set()

        # Step 1: Discover all API relay URLs in the README (global context)
        global_urls: list[str] = []
        for m in _API_URL_RE.finditer(text):
            url = self._normalize_url(m.group(1))
            if url not in global_urls and self._looks_like_api_url(url):
                global_urls.append(url)

        # Also look for relay URLs in backticks that contain /v1
        for m in re.finditer(r"`(https?://[^`]+/v1[^`]*)`", text):
            url = self._normalize_url(m.group(1))
            if url not in global_urls and self._looks_like_api_url(url):
                global_urls.append(url)

        if global_urls:
            log.debug("  Global API URLs: %s", global_urls[:5])

        # Step 2: Parse table rows for (URL, key, model) tuples
        for row_match in _TABLE_ROW_RE.finditer(text):
            row_text = row_match.group(1)
            urls = [self._normalize_url(u) for u in _URL_RE.findall(row_text)]
            keys = _KEY_RE.findall(row_text)
            models = _MODEL_RE.findall(row_text)

            # Filter fake keys
            keys = [k for k in keys if not _is_fake_key(k)]

            # If no URL in this row, use the first global URL
            if not urls and global_urls:
                urls = [global_urls[0]]

            for url in urls:
                if not self._looks_like_api_url(url):
                    continue
                for key in keys:
                    uid = f"{url}|{key}"
                    if uid in seen:
                        continue
                    seen.add(uid)
                    results.append(DiscoveredToken(
                        source=source_tag,
                        base_url=url,
                        api_key=key,
                        raw_models=list(models),
                    ))

            # If row has a URL but no key, add it as a keyless endpoint
            if urls and not keys:
                for url in urls:
                    if not self._looks_like_api_url(url):
                        continue
                    uid = f"{url}|"
                    if uid in seen:
                        continue
                    seen.add(uid)
                    results.append(DiscoveredToken(
                        source=source_tag,
                        base_url=url,
                        api_key="",
                        raw_models=list(models),
                    ))

        # Step 3: Loose scan for URL + nearby key (within 200 chars)
        if not results:
            for url_m in _API_URL_RE.finditer(text):
                url = self._normalize_url(url_m.group(1))
                if not self._looks_like_api_url(url):
                    continue
                window = text[url_m.start(): url_m.end() + 200]
                keys = [k for k in _KEY_RE.findall(window) if not _is_fake_key(k)]
                models = _MODEL_RE.findall(window)
                for key in keys:
                    uid = f"{url}|{key}"
                    if uid in seen:
                        continue
                    seen.add(uid)
                    results.append(DiscoveredToken(
                        source=source_tag,
                        base_url=url,
                        api_key=key,
                        raw_models=list(models),
                    ))

        # Step 4: Standalone keys (no URL nearby) — pair with global URL
        if not results and global_urls:
            for key_m in _KEY_RE.finditer(text):
                key = key_m.group(1)
                if _is_fake_key(key):
                    continue
                uid = f"{global_urls[0]}|{key}"
                if uid in seen:
                    continue
                seen.add(uid)
                results.append(DiscoveredToken(
                    source=source_tag,
                    base_url=global_urls[0],
                    api_key=key,
                ))

        # Step 5: If README only mentions relay URLs (no keys), add them as keyless
        if not results and global_urls:
            for url in global_urls[:3]:  # limit to top 3
                uid = f"{url}|"
                if uid in seen:
                    continue
                seen.add(uid)
                results.append(DiscoveredToken(
                    source=source_tag,
                    base_url=url,
                    api_key="",
                ))

        return results

    @staticmethod
    def _looks_like_api_url(url: str) -> bool:
        """Check if a URL looks like an API endpoint."""
        lower = url.lower()
        return any(k in lower for k in [
            "/v1", "/api", "api.", "openai", "claude", "gpt", "chat",
            "aiapiv2", "freetheai", "okrouter", "chatanywhere",
            "relay", "router", "proxy", "llm",
        ])

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip trailing punctuation and path noise, keep up to /v1 or /api."""
        url = url.rstrip(".,;:)}]")
        url = url.rstrip("/")
        for suffix in ["/chat/completions", "/messages", "/responses", "/models", "/health"]:
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)]
        return url
"""Scrape free API tokens from GitHub repo READMEs."""

from __future__ import annotations

import re
import time
from base64 import b64decode
from urllib.parse import urlparse

from .base import BaseSource, DiscoveredToken
import logging

log = logging.getLogger(__name__)

# Patterns that look like API endpoints — relaxed to also match relay domains
_URL_RE = re.compile(
    r"(?:`|\")?(https?://[^\s\)\]\|\"'<>`,]+)(?:`|\")?",
    re.IGNORECASE,
)
# More specific: URLs that look like API endpoints
_API_URL_RE = re.compile(
    r"(?:`|\")?(https?://(?:api\.|ai|openai|claude|gpt|chat)[^\s\)\]\|\"'<>`,]*(?:/v1)?)(?:`|\")?",
    re.IGNORECASE,
)
# Patterns that look like API keys
_KEY_RE = re.compile(
    r"(?:`|\")?(sk-[A-Za-z0-9_\-]{20,}|sk-or-v1-[A-Za-z0-9_\-]{20,}|"
    r"sk-proj-[A-Za-z0-9_\-]{20,}|sk-ant-[A-Za-z0-9_\-]{20,}|"
    r"tp-[A-Za-z0-9_\-]{20,}|"
    r"AIza[A-Za-z0-9_\-]{30,}|"
    r"gsk_[A-Za-z0-9_\-]{20,}|"
    r"xai-[A-Za-z0-9_\-]{20,})(?:`|\")?",
)
# Fake key patterns to skip
_FAKE_KEY_PATTERNS = [
    re.compile(r"sk-(?:abcdef|123456|abcd1234|1234abcd|5678efgh)"),  # obvious placeholders
    re.compile(r"sk-[a-f0-9]{4,}$", re.IGNORECASE),  # hex-only short patterns
    re.compile(r"(sk-)\1{2,}"),  # repeating segments
]
# Model names commonly listed in tables
_MODEL_RE = re.compile(
    r"\b(gpt-[\w.\-]+|claude-[\w.\-]+|deepseek-[\w.\-]+|gemini-[\w.\-]+|"
    r"qwen-[\w.\-]+|mistral-[\w.\-]+|llama-[\w.\-]+|mimo-[\w.\-]+|"
    r"grok-[\w.\-]+|o[1-9][\w.\-]*)\b",
    re.IGNORECASE,
)

# Markdown table row
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$", re.MULTILINE)


def _is_fake_key(key: str) -> bool:
    """Check if a key looks like a placeholder/fake."""
    # Keys with repeating character groups
    if len(set(key[3:])) < 8:  # too few unique chars after prefix
        return True
    for pat in _FAKE_KEY_PATTERNS:
        if pat.search(key):
            return True
    return False


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

    # -- HTTP ----------------------------------------------------------

    def _fetch_readme(self, client, owner: str, repo: str, gh_token: str) -> str:
        """Fetch README content via GitHub raw content."""
        # Try raw content first (more reliable, no rate limit for public repos)
        for branch in ["main", "master"]:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
            r = client.get(raw_url, headers={"User-Agent": self.user_agent})
            if r.status_code == 200:
                return r.text

        # Fallback: API (needs auth for higher rate limits)
        headers = {"Accept": "application/vnd.github.v3+json"}
        if gh_token:
            headers["Authorization"] = f"token {gh_token}"

        url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        r = client.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            content = data.get("content", "")
            encoding = data.get("encoding", "base64")
            if encoding == "base64":
                return b64decode(content).decode("utf-8", errors="replace")
            return content

        raise RuntimeError(f"Cannot fetch README for {owner}/{repo}: {r.status_code}")

    # -- Extraction ----------------------------------------------------

    def _extract_tokens(self, text: str, source_tag: str) -> list[DiscoveredToken]:
        """Extract (URL, key) pairs from README text."""
        results: list[DiscoveredToken] = []
        seen: set[str] = set()

        # Step 1: Discover all API relay URLs in the README (global context)
        global_urls: list[str] = []
        for m in _API_URL_RE.finditer(text):
            url = self._normalize_url(m.group(1))
            if url not in global_urls and self._looks_like_api_url(url):
                global_urls.append(url)

        # Also look for relay URLs in backticks that contain /v1
        for m in re.finditer(r"`(https?://[^`]+/v1[^`]*)`", text):
            url = self._normalize_url(m.group(1))
            if url not in global_urls and self._looks_like_api_url(url):
                global_urls.append(url)

        if global_urls:
            log.debug("  Global API URLs: %s", global_urls[:5])

        # Step 2: Parse table rows for (URL, key, model) tuples
        for row_match in _TABLE_ROW_RE.finditer(text):
            row_text = row_match.group(1)
            urls = [self._normalize_url(u) for u in _URL_RE.findall(row_text)]
            keys = _KEY_RE.findall(row_text)
            models = _MODEL_RE.findall(row_text)

            # Filter fake keys
            keys = [k for k in keys if not _is_fake_key(k)]

            # If no URL in this row, use the first global URL
            if not urls and global_urls:
                urls = [global_urls[0]]

            for url in urls:
                if not self._looks_like_api_url(url):
                    continue
                for key in keys:
                    uid = f"{url}|{key}"
                    if uid in seen:
                        continue
                    seen.add(uid)
                    results.append(DiscoveredToken(
                        source=source_tag,
                        base_url=url,
                        api_key=key,
                        raw_models=list(models),
                    ))

            # If row has a URL but no key, add it as a keyless endpoint
            if urls and not keys:
                for url in urls:
                    if not self._looks_like_api_url(url):
                        continue
                    uid = f"{url}|"
                    if uid in seen:
                        continue
                    seen.add(uid)
                    results.append(DiscoveredToken(
                        source=source_tag,
                        base_url=url,
                        api_key="",
                        raw_models=list(models),
                    ))

        # Step 3: Loose scan for URL + nearby key (within 200 chars)
        if not results:
            for url_m in _API_URL_RE.finditer(text):
                url = self._normalize_url(url_m.group(1))
                if not self._looks_like_api_url(url):
                    continue
                window = text[url_m.start(): url_m.end() + 200]
                keys = [k for k in _KEY_RE.findall(window) if not _is_fake_key(k)]
                models = _MODEL_RE.findall(window)
                for key in keys:
                    uid = f"{url}|{key}"
                    if uid in seen:
                        continue
                    seen.add(uid)
                    results.append(DiscoveredToken(
                        source=source_tag,
                        base_url=url,
                        api_key=key,
                        raw_models=list(models),
                    ))

        # Step 4: Standalone keys (no URL nearby) — pair with global URL
        if not results and global_urls:
            for key_m in _KEY_RE.finditer(text):
                key = key_m.group(1)
                if _is_fake_key(key):
                    continue
                uid = f"{global_urls[0]}|{key}"
                if uid in seen:
                    continue
                seen.add(uid)
                results.append(DiscoveredToken(
                    source=source_tag,
                    base_url=global_urls[0],
                    api_key=key,
                ))

        # Step 5: If README only mentions relay URLs (no keys), add them as keyless
        if not results and global_urls:
            for url in global_urls[:3]:  # limit to top 3
                uid = f"{url}|"
                if uid in seen:
                    continue
                seen.add(uid)
                results.append(DiscoveredToken(
                    source=source_tag,
                    base_url=url,
                    api_key="",
                ))

        return results

    @staticmethod
    def _looks_like_api_url(url: str) -> bool:
        """Check if a URL looks like an API endpoint."""
        lower = url.lower()
        return any(k in lower for k in [
            "/v1", "/api", "api.", "openai", "claude", "gpt", "chat",
            "aiapiv2", "freetheai", "okrouter", "chatanywhere",
            "relay", "router", "proxy", "llm",
        ])

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip trailing punctuation and path noise, keep up to /v1 or /api."""
        url = url.rstrip(".,;:)}]")
        url = url.rstrip("/")
        for suffix in ["/chat/completions", "/messages", "/responses", "/models", "/health"]:
            if url.lower().endswith(suffix):
                url = url[: -len(suffix)]
        return url
