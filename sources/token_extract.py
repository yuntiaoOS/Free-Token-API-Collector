"""Extract API endpoints and keys from unstructured forum/README text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import DiscoveredToken

# ── URL patterns ─────────────────────────────────────────────────────

_URL_INLINE_RE = re.compile(
    r"https?://[^\s\)\]\|\"'<>`,]+",
    re.IGNORECASE,
)
_API_PATH_RE = re.compile(
    r"/(?:v1/?|api/|chat/completions|messages|responses)(?:\s|$|/|\?)",
    re.IGNORECASE,
)
_API_HOST_HINT_RE = re.compile(
    r"(?:api\.|openai|claude|gpt|mimo|xiaomi|pekpik|deepseek|grok|gemini|"
    r"openrouter|siliconflow|volces|moonshot|aihub)",
    re.IGNORECASE,
)

# ── Key patterns ───────────────────────────────────────────────────

_KEY_RE = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_\-]{20,}|"
    r"sk-or-v1-[A-Za-z0-9_\-]{20,}|"
    r"sk-proj-[A-Za-z0-9_\-]{20,}|"
    r"sk-ant-[A-Za-z0-9_\-]{20,}|"
    r"tp-[A-Za-z0-9_\-]{20,}|"
    r"AIza[A-Za-z0-9_\-]{30,}|"
    r"gsk_[A-Za-z0-9_\-]{20,}|"
    r"xai-[A-Za-z0-9_\-]{20,}"
    r")\b"
)
_MODEL_RE = re.compile(
    r"\b("
    r"gpt-[\w.\-/]+|claude-[\w.\-/]+|deepseek-[\w.\-/]+|gemini-[\w.\-/]+|"
    r"qwen[\w.\-/]*|mistral-[\w.\-/]+|llama-[\w.\-/]+|mimo[\w.\-/]*|"
    r"grok-[\w.\-/]+|o[1-9][\w.\-/]*"
    r")\b",
    re.IGNORECASE,
)

# Labelled fields: base_url: https://...  /  API Key：sk-...
_BASE_LABEL_RE = re.compile(
    r"(?i)(?:base[_\s-]?url|api[_\s-]?url|endpoint|接口(?:地址)?|地址|反代)"
    r"\s*[:：=]\s*[`\"']?(https?://[^\s`\"'<>]+)",
)
_KEY_LABEL_RE = re.compile(
    r"(?i)(?:api[_\s-]?key|apikey|auth(?:_token)?|密钥|key|token)"
    r"\s*[:：=]\s*[`\"']?("
    r"sk-[A-Za-z0-9_\-]{10,}|tp-[A-Za-z0-9_\-]{10,}|"
    r"sk-ant-[A-Za-z0-9_\-]{10,}|sk-or-v1-[A-Za-z0-9_\-]{10,}"
    r")",
)
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_FAKE_KEY_PATTERNS = [
    re.compile(r"sk-(?:abcdef|123456|abcd1234|1234abcd|5678efgh|xxxx|your)", re.I),
    re.compile(r"sk-[a-f0-9]{4,}$", re.IGNORECASE),
    re.compile(r"(sk-)\1{2,}"),
    re.compile(r"sk-[x\*]{8,}", re.I),
]

@dataclass
class ExtractedPair:
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)
    confidence: int = 0


def is_fake_key(key: str) -> bool:
    if len(key) < 12:
        return True
    body = key.split("-", 1)[-1] if "-" in key else key
    if len(set(body)) < 8:
        return True
    return any(pat.search(key) for pat in _FAKE_KEY_PATTERNS)


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip(".,;:)}]`\"'")
    url = url.rstrip("/")
    for suffix in ("/chat/completions", "/messages", "/responses", "/models"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)]
    if url.lower().endswith("/v1"):
        return url
    if _API_HOST_HINT_RE.search(url) and not url.lower().endswith("/v1"):
        return f"{url}/v1"
    return url


def is_plausible_api_url(url: str) -> bool:
    lowered = url.lower()
    if not lowered.startswith("http"):
        return False
    if any(x in lowered for x in ("github.com", "imgur.", "gstatic.", "gravatar.", "linux.do", "v2ex.com")):
        return False
    if _API_PATH_RE.search(url):
        return True
    if _API_HOST_HINT_RE.search(url):
        return True
    return lowered.endswith("/v1") or "/v1/" in lowered


class TokenExtractor:
    """Multi-strategy extractor with confidence scoring."""

    def extract(self, text: str, source_tag: str) -> list[DiscoveredToken]:
        if not text or not text.strip():
            return []

        pairs: list[ExtractedPair] = []
        seen: set[str] = set()

        for strategy in (
            self._from_labelled_fields,
            self._from_code_blocks,
            self._from_url_key_windows,
            self._from_orphan_keys,
        ):
            for pair in strategy(text):
                uid = f"{pair.base_url}|{pair.api_key}"
                if uid in seen or is_fake_key(pair.api_key):
                    continue
                if not pair.base_url or not is_plausible_api_url(pair.base_url):
                    continue
                seen.add(uid)
                pairs.append(pair)

        pairs.sort(key=lambda p: p.confidence, reverse=True)
        return [
            DiscoveredToken(
                source=source_tag,
                base_url=pair.base_url,
                api_key=pair.api_key,
                raw_models=pair.models[:10],
                extra={"confidence": pair.confidence},
            )
            for pair in pairs
        ]

    def _from_labelled_fields(self, text: str) -> list[ExtractedPair]:
        results: list[ExtractedPair] = []
        bases = [normalize_base_url(m.group(1)) for m in _BASE_LABEL_RE.finditer(text)]
        if not bases:
            bases = [
                normalize_base_url(u)
                for u in _URL_INLINE_RE.findall(text)
                if is_plausible_api_url(u)
            ]
        keys = [m.group(1) for m in _KEY_LABEL_RE.finditer(text) if not is_fake_key(m.group(1))]
        models = list(dict.fromkeys(_MODEL_RE.findall(text)))
        if not keys:
            return results
        base = bases[0] if bases else ""
        for key in keys:
            results.append(ExtractedPair(base_url=base, api_key=key, models=models, confidence=90))
        return results

    def _from_code_blocks(self, text: str) -> list[ExtractedPair]:
        results: list[ExtractedPair] = []
        for block in _CODE_BLOCK_RE.findall(text):
            bases = [
                normalize_base_url(u)
                for u in _URL_INLINE_RE.findall(block)
                if is_plausible_api_url(u)
            ]
            keys = [k for k in _KEY_RE.findall(block) if not is_fake_key(k)]
            models = list(dict.fromkeys(_MODEL_RE.findall(block)))
            if not keys:
                continue
            base = bases[0] if bases else ""
            for key in keys:
                results.append(
                    ExtractedPair(base_url=base, api_key=key, models=models, confidence=80)
                )
        return results

    def _from_url_key_windows(self, text: str) -> list[ExtractedPair]:
        results: list[ExtractedPair] = []
        for match in _URL_INLINE_RE.finditer(text):
            raw_url = match.group(0)
            if not is_plausible_api_url(raw_url):
                continue
            base = normalize_base_url(raw_url)
            start = max(0, match.start() - 120)
            end = min(len(text), match.end() + 400)
            window = text[start:end]
            keys = [k for k in _KEY_RE.findall(window) if not is_fake_key(k)]
            models = list(dict.fromkeys(_MODEL_RE.findall(window)))
            for key in keys:
                results.append(
                    ExtractedPair(base_url=base, api_key=key, models=models, confidence=70)
                )
        return results

    def _from_orphan_keys(self, text: str) -> list[ExtractedPair]:
        keys = [k for k in _KEY_RE.findall(text) if not is_fake_key(k)]
        bases = [
            normalize_base_url(u)
            for u in _URL_INLINE_RE.findall(text)
            if is_plausible_api_url(u)
        ]
        if not keys or not bases:
            return []
        models = list(dict.fromkeys(_MODEL_RE.findall(text)))
        return [
            ExtractedPair(base_url=bases[0], api_key=key, models=models, confidence=40)
            for key in keys
        ]
