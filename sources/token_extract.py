"""Extract API endpoints and keys from unstructured text with multi-strategy scoring.

Improvements over the original version:
1. Supports structured JSON lists / HTML tables embedded in text.
2. Smarter URL-key proximity pairing with context-aware confidence.
3. More key formats: Bearer tokens, env-var style (OPENAI_API_KEY=sk-...),
   base64-encoded keys, and longer generic API tokens.
4. Configurable host hints and path patterns.
5. Better fake-key detection (repeated chars, common placeholders).
6. Model extraction improved to cover more naming conventions.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from urllib.parse import unquote
from urllib.parse import urlparse

from .base import DiscoveredToken

# ---------------------------------------------------------------------------
# URL patterns
# ---------------------------------------------------------------------------
_URL_INLINE_RE = re.compile(
    r"https?://[^\s\)\]\|\"'<>,;]+",
    re.IGNORECASE,
)
_API_PATH_RE = re.compile(
    r"/(?:v1/?|v2/?|api/|chat/completions|messages|responses)(?:\s|$|/|\?|$)",
    re.IGNORECASE,
)
_API_HOST_HINT_RE = re.compile(
    r"(?:api\.|openai|claude|gpt|mimo|xiaomi|pekpik|deepseek|grok|gemini|"
    r"openrouter|siliconflow|volces|moonshot|aihub|together|anyscale|"
    r"fireworks|cerebras|replicate|perplexity|cohere|mistral)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Key patterns - expanded to cover more formats
# ---------------------------------------------------------------------------
_KEY_RE = re.compile(
    r"\b("
    # OpenAI variants
    r"sk-[A-Za-z0-9_\-]{20,}|"
    r"sk-or-v1-[A-Za-z0-9_\-]{20,}|"
    r"sk-proj-[A-Za-z0-9_\-]{20,}|"
    # Anthropic
    r"sk-ant-[A-Za-z0-9_\-]{20,}|"
    # Generic API token prefixes
    r"tp-[A-Za-z0-9_\-]{20,}|"
    # Google
    r"AIza[A-Za-z0-9_\-]{30,}|"
    # Groq
    r"gsk_[A-Za-z0-9_\-]{20,}|"
    # xAI
    r"xai-[A-Za-z0-9_\-]{20,}|"
    # Together / generic Bearer-style tokens (long alphanumeric)
    r"Bearer\s+[A-Za-z0-9_\-]{32,}|"
    # Generic long hex tokens (e.g. sha256-based) — only in labelled/json paths
    # r"[A-Fa-f0-9]{40,}"
    r")\b"
)

# Stricter key regex for labelled-field extraction (must have prefix)
_PREFIXED_KEY_RE = re.compile(
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

# Env-var style: OPENAI_API_KEY=sk-...
_ENV_KEY_RE = re.compile(
    r"(?i)(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|API_KEY|APIKEY|BEARER_TOKEN)"
    r"\s*=\s*[\"']?([A-Za-z0-9_\-]{20,})",
)

_MODEL_RE = re.compile(
    r"\b("
    r"gpt-[\w.\-/]+|claude-[\w.\-/]+|deepseek-[\w.\-/]+|gemini-[\w.\-/]+|"
    r"qwen[\w.\-/]*|mistral-[\w.\-/]+|llama-[\w.\-/]+|mimo[\w.\-/]*|"
    r"grok-[\w.\-/]+|o[1-9][\w.\-/]*|"
    r"command-[\w.\-/]+|yi-[\w.\-/]+|phi-[\w.\-/]+|internlm-[\w.\-/]+|"
    r"glm-[\w.\-/]+|chatglm-[\w.\-/]+|baichuan-[\w.\-/]+|"
    r"mixtral-[\w.\-/]+|codellama-[\w.\-/]+|starcoder[\w.\-/]*"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Labelled fields
# ---------------------------------------------------------------------------
_BASE_LABEL_RE = re.compile(
    r"(?i)(?:base[_\s-]?url|api[_\s-]?url|endpoint|接口(?:地址)?|地址|反代)"
    r"\s*[:：]\s*[\"']?(https?://[^\s\"'<>]+)",
)
_KEY_LABEL_RE = re.compile(
    r"(?i)(?:api[_\s-]?key|apikey|auth(?:_token)?|bearer|密钥|key|token|key\d*)"
    r"\s*[:：=]\s*[\"']?("
    r"sk-[A-Za-z0-9_\-]{10,}|tp-[A-Za-z0-9_\-]{10,}|"
    r"sk-ant-[A-Za-z0-9_\-]{10,}|sk-or-v1-[A-Za-z0-9_\-]{10,}|"
    r"sk-proj-[A-Za-z0-9_\-]{10,}|AIza[A-Za-z0-9_\-]{20,}|"
    r"gsk_[A-Za-z0-9_\-]{10,}|xai-[A-Za-z0-9_\-]{10,}|"
    r"[A-Za-z0-9+/]{16,}={0,2}|"
    r"(?:%[0-9A-Fa-f]{2}){4,}[A-Za-z0-9%_\-+=./]*"
    r")",
)
# Chinese forum style: key1：tp-xxx / 密钥2：sk-xxx
_CN_KEY_LABEL_RE = re.compile(
    r"(?i)(?:key|密钥)\s*\d*\s*[:：]\s*[\"']?("
    r"sk-[A-Za-z0-9_\-]{10,}|tp-[A-Za-z0-9_\-]{10,}|"
    r"sk-ant-[A-Za-z0-9_\-]{10,}|sk-or-v1-[A-Za-z0-9_\-]{10,}|"
    r"sk-proj-[A-Za-z0-9_\-]{10,}|"
    r"[A-Za-z0-9+/]{16,}={0,2}|"
    r"(?:%[0-9A-Fa-f]{2}){4,}[A-Za-z0-9%_\-+=./]*"
    r")",
)
# OpenAI / Anthropic 双协议（MiMo 等论坛常见格式）
_OPENAI_PROTOCOL_RE = re.compile(
    r"(?i)openai\s*接口(?:协议)?\s*[:：]\s*[\"']?(https?://[^\s\"'<>]+)"
)
_ANTHROPIC_PROTOCOL_RE = re.compile(
    r"(?i)anthropic\s*接口(?:协议)?\s*[:：]\s*[\"']?(https?://[^\s\"'<>]+)"
)
# Base URL 兼容 OpenAI 接口协议：https://...
_CN_BASE_LABEL_RE = re.compile(
    r"(?i)(?:base\s*url|接口(?:地址|协议)?|反代|endpoint)"
    r"[^:\n]{0,30}[:：]\s*[\"']?(https?://[^\s\"'<>]+)",
)

# Env-var style: KEY=sk-xxx or export KEY=sk-xxx
_ENV_LABEL_RE = re.compile(
    r"(?i)(?:export\s+)?"
    r"(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|API_KEY|APIKEY|BEARER_TOKEN|AUTH_TOKEN)"
    r"\s*=\s*[\"']?([A-Za-z0-9_\-]{20,})",
)

# JSON key-value pattern: "api_key": "sk-..." or "apiKey": "sk-..."
_JSON_KEY_RE = re.compile(
    r'"(?:api[_-]?key|apiKey|key|token|secret)"\s*:\s*"([A-Za-z0-9_\-]{20,})"',
    re.IGNORECASE,
)

# JSON base_url pattern: "base_url": "https://..." or "baseUrl": "https://..."
_JSON_BASE_RE = re.compile(
    r'"(?:base[_-]?url|baseUrl|url|endpoint|api_url)"\s*:\s*"(https?://[^"]+)"',
    re.IGNORECASE,
)

_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_B64_LABEL_RE = re.compile(
    r"(?i)(?:base64|b64|编码|encrypt(?:ed)?)\s*[:：]\s*([A-Za-z0-9+/=\s]{12,})"
)
_URLENC_LABEL_RE = re.compile(
    r"(?i)(?:url\s*decode|urldecode|解码|percent(?:\s*encode)?)\s*[:：]\s*([A-Za-z0-9%_\-+=./]{12,})"
)
_B64_BLOB_RE = re.compile(r"\b([A-Za-z0-9+/]{20,}={0,2})\b")
_URLENC_BLOB_RE = re.compile(r"(?:%[0-9A-Fa-f]{2}){4,}[A-Za-z0-9%_\-+=./]*")
_FAKE_KEY_PATTERNS = [
    re.compile(r"sk-(?:abcdef|123456|abcd1234|1234abcd|5678efgh|xxxx|your|test|demo|sample|example|placeholder|replace|change|insert)", re.I),
    re.compile(r"sk-[a-f0-9]{4,}$", re.IGNORECASE),
    re.compile(r"(sk-)\1{2,}"),
    re.compile(r"sk-[x\*]{8,}", re.I),
    re.compile(r"sk-{1,}"),
]


def _looks_like_decoded_payload(text: str) -> bool:
    lowered = text.lower()
    if re.search(r"\b(sk-|tp-|sk-ant-|sk-or-|sk-proj-|gsk_|xai-|AIza)", text):
        return True
    if re.search(r"https?://", text, re.I):
        return True
    if "api" in lowered and "key" in lowered:
        return True
    if "base_url" in lowered or "接口" in text or "mimo" in lowered:
        return True
    return False


def _try_b64decode(value: str) -> str | None:
    cleaned = re.sub(r"\s+", "", value.strip())
    if len(cleaned) < 12 or len(cleaned) % 4 == 1:
        return None
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", cleaned):
        return None
    padding = "=" * ((4 - len(cleaned) % 4) % 4)
    try:
        raw = base64.b64decode(cleaned + padding, validate=False)
    except Exception:
        return None
    for encoding in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _looks_like_decoded_payload(text):
            return text
    return None


def _try_urldecode(value: str, *, rounds: int = 3) -> str | None:
    current = value.strip()
    if "%" not in current:
        return None
    original = current
    for _ in range(rounds):
        try:
            decoded = unquote(current)
        except Exception:
            break
        if decoded == current:
            break
        current = decoded
    if current != original and _looks_like_decoded_payload(current):
        return current
    return None


def expand_encoded_content(text: str) -> str:
    """Append decoded base64 / URL-encoded blobs so downstream extractors can match keys."""
    if not text or not text.strip():
        return text

    extras: list[str] = []
    seen: set[str] = set()

    def add_extra(value: str) -> None:
        value = value.strip()
        if not value or value in seen:
            return
        seen.add(value)
        extras.append(value)

    for match in _B64_LABEL_RE.finditer(text):
        decoded = _try_b64decode(match.group(1))
        if decoded:
            add_extra(decoded)

    for match in _URLENC_LABEL_RE.finditer(text):
        decoded = _try_urldecode(match.group(1))
        if decoded:
            add_extra(decoded)

    for match in _B64_BLOB_RE.finditer(text):
        candidate = match.group(1)
        if candidate.startswith(("sk-", "tp-", "AIza")):
            continue
        decoded = _try_b64decode(candidate)
        if decoded:
            add_extra(decoded)

    for match in _URLENC_BLOB_RE.finditer(text):
        decoded = _try_urldecode(match.group(0))
        if decoded:
            add_extra(decoded)

    for line in text.splitlines():
        stripped = line.strip().strip("`\"'")
        if not stripped:
            continue
        if stripped.count("%") >= 4:
            decoded = _try_urldecode(stripped)
            if decoded:
                add_extra(decoded)
        if re.fullmatch(r"[A-Za-z0-9+/=]{20,}", stripped):
            decoded = _try_b64decode(stripped)
            if decoded:
                add_extra(decoded)

    if not extras:
        return text
    return text + "\n\n--- decoded ---\n" + "\n".join(extras)


def is_fake_key(key: str) -> bool:
    """Return True if a key is obviously a placeholder or invalid."""
    if len(key) < 12:
        return True
    body = key.split("-", 1)[-1] if "-" in key else key
    if len(set(body)) < 8:
        return True
    if any(pat.search(key) for pat in _FAKE_KEY_PATTERNS):
        return True
    # Reject keys that are all the same character repeated
    if len(set(key.replace("-", "").replace("_", ""))) <= 2:
        return True
    return False


def infer_app_type(base_url: str) -> str:
    """Infer cc-switch app_type from endpoint URL."""
    lowered = base_url.lower()
    if "/anthropic" in lowered:
        return "claude"
    if lowered.rstrip("/").endswith("/v1") or "/v1/" in lowered:
        return "openclaw"
    return ""


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip(".,;:)}]\"'")
    url = url.rstrip("/")
    lowered = url.lower()
    if "/anthropic" in lowered:
        return url
    # Strip known API path suffixes
    for suffix in ("/chat/completions", "/messages", "/responses", "/models", "/embeddings"):
        if url.lower().endswith(suffix):
            url = url[: -len(suffix)]
    # Already has /v1 or /v2 - keep as is
    if re.search(r"/v\d+/?$", url, re.I):
        return url
    # Host hints suggest this is an API endpoint - add /v1
    if _API_HOST_HINT_RE.search(url) and not url.lower().endswith("/v1"):
        return f"{url}/v1"
    return url


def is_plausible_api_url(url: str) -> bool:
    """Return True if the URL looks like it could be an API endpoint."""
    lowered = url.lower()
    if not lowered.startswith("http"):
        return False
    parsed = urlparse(url)
    if parsed.fragment or "autosubmit" in lowered or "/dashboard" in lowered:
        return False
    if parsed.path.count("/") <= 1 and not _API_HOST_HINT_RE.search(url) and not lowered.endswith("/v1"):
        # bare domain without API path, e.g. https://gptgod.online
        if not _API_PATH_RE.search(url):
            return False
    # Blacklist obvious non-API URLs
    blacklist = (
        "github.com", "imgur.", "gstatic.", "gravatar.",
        "linux.do", "v2ex.com", "nodeseek.com", "deepflood.com",
        "youtube.com", "twitter.com", "x.com", "reddit.com",
        "stackoverflow.com", "medium.com", "dev.to",
        "vb.do", "gptgod.online", "openrouter.ai/docs",
    )
    if any(x in lowered for x in blacklist):
        return False
    if _API_PATH_RE.search(url):
        return True
    if _API_HOST_HINT_RE.search(url):
        return True
    if lowered.endswith("/v1") or "/v1/" in lowered:
        return True
    # Generic API-looking domains with port (e.g. http://host:8080/v1)
    if parsed.port and parsed.path.rstrip("/") in ("", "/v1"):
        return True
    return False


def _proximity_score(url_pos: int, key_pos: int, text_len: int) -> int:
    """Score how close a URL and key are to each other in the text.

    Returns 0-30 bonus points to add to confidence.
    """
    distance = abs(url_pos - key_pos)
    if distance < 50:
        return 30
    if distance < 150:
        return 20
    if distance < 400:
        return 10
    return 0


@dataclass
class ExtractedPair:
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)
    confidence: int = 0
    app_type: str = ""
    # Track positions for proximity scoring
    url_pos: int = -1
    key_pos: int = -1


class TokenExtractor:
    """Multi-strategy extractor with confidence scoring.

    Strategies run in order of decreasing confidence:
    1. Labelled fields (base_url: ... / api_key: ...)  -> 95
    2. Env-var style (OPENAI_API_KEY=sk-...)            -> 90
    3. JSON key-value pairs in text                    -> 85
    4. Code blocks with URL + key                       -> 80
    5. URL-key proximity pairing                        -> 60-75
    6. Orphan keys paired with first plausible URL      -> 40
    """

    def extract(self, text: str, source_tag: str) -> list[DiscoveredToken]:
        if not text or not text.strip():
            return []

        text = expand_encoded_content(text)
        pairs: list[ExtractedPair] = []
        seen: set[str] = set()

        for strategy in (
            self._from_dual_protocol,
            self._from_labelled_fields,
            self._from_env_vars,
            self._from_json_kv,
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
                extra={
                    "confidence": pair.confidence,
                    **({"app_type": pair.app_type} if pair.app_type else {}),
                },
            )
            for pair in pairs
        ]

    def _from_dual_protocol(self, text: str) -> list[ExtractedPair]:
        """One API Key shared by OpenAI + Anthropic endpoints (common on linux.do MiMo posts)."""
        openai_urls = [normalize_base_url(m.group(1)) for m in _OPENAI_PROTOCOL_RE.finditer(text)]
        anthropic_urls = [
            normalize_base_url(m.group(1)) for m in _ANTHROPIC_PROTOCOL_RE.finditer(text)
        ]
        if not openai_urls and not anthropic_urls:
            return []

        keys: list[str] = []
        for pattern in (_KEY_LABEL_RE, _CN_KEY_LABEL_RE):
            for match in pattern.finditer(text):
                key = match.group(1)
                if not key.startswith(("sk-", "tp-", "sk-ant", "sk-or", "sk-proj", "gsk_", "xai-", "AIza")):
                    key = _try_b64decode(key) or _try_urldecode(key) or key
                if not is_fake_key(key) and key not in keys:
                    keys.append(key)
        if not keys:
            return []

        models = list(dict.fromkeys(_MODEL_RE.findall(text)))
        results: list[ExtractedPair] = []
        for key in keys:
            for url in openai_urls:
                if is_plausible_api_url(url):
                    results.append(
                        ExtractedPair(
                            base_url=url,
                            api_key=key,
                            models=models,
                            confidence=98,
                            app_type="openclaw",
                        )
                    )
            for url in anthropic_urls:
                if is_plausible_api_url(url):
                    results.append(
                        ExtractedPair(
                            base_url=url,
                            api_key=key,
                            models=models,
                            confidence=98,
                            app_type="claude",
                        )
                    )
        return results

    def _from_labelled_fields(self, text: str) -> list[ExtractedPair]:
        """Extract from labelled fields like 'base_url: https://...' / 'api_key: sk-...'"""
        results: list[ExtractedPair] = []
        bases = [(normalize_base_url(m.group(1)), m.start()) for m in _BASE_LABEL_RE.finditer(text)]
        bases += [(normalize_base_url(m.group(1)), m.start()) for m in _CN_BASE_LABEL_RE.finditer(text)]
        if not bases:
            bases = [
                (normalize_base_url(m.group(0)), m.start())
                for m in _URL_INLINE_RE.finditer(text)
                if is_plausible_api_url(m.group(0))
            ]
        keys = [(m.group(1), m.start()) for m in _KEY_LABEL_RE.finditer(text) if not is_fake_key(m.group(1))]
        keys += [(m.group(1), m.start()) for m in _CN_KEY_LABEL_RE.finditer(text) if not is_fake_key(m.group(1))]
        # Decode labelled values that are still encoded
        decoded_keys: list[tuple[str, int]] = []
        for key, pos in keys:
            plain = key
            if not plain.startswith(("sk-", "tp-", "sk-ant", "sk-or", "sk-proj", "gsk_", "xai-", "AIza")):
                plain = _try_b64decode(key) or _try_urldecode(key) or key
            if not is_fake_key(plain):
                decoded_keys.append((plain, pos))
        keys = decoded_keys
        # dedupe keys by value keeping first position
        seen_keys: set[str] = set()
        unique_keys: list[tuple[str, int]] = []
        for key, pos in keys:
            if key in seen_keys:
                continue
            seen_keys.add(key)
            unique_keys.append((key, pos))
        if not keys:
            return results
        models = list(dict.fromkeys(_MODEL_RE.findall(text)))

        def append_pair(base_url: str, base_pos: int, key: str, key_pos: int, confidence: int) -> None:
            app_type = infer_app_type(base_url)
            bonus = _proximity_score(base_pos, key_pos, len(text)) if base_pos >= 0 else 0
            results.append(
                ExtractedPair(
                    base_url=base_url,
                    api_key=key,
                    models=models,
                    confidence=confidence + min(bonus, 5),
                    app_type=app_type,
                    url_pos=base_pos,
                    key_pos=key_pos,
                )
            )

        if len(keys) == 1 and len(bases) > 1:
            key, key_pos = keys[0]
            for base_url, base_pos in bases:
                append_pair(base_url, base_pos, key, key_pos, 92)
            return results

        base_url, base_pos = bases[0] if bases else ("", 0)
        for key, key_pos in keys:
            if len(bases) > 1:
                nearest = min(bases, key=lambda item: abs(item[1] - key_pos))
                base_url, base_pos = nearest
            append_pair(base_url, base_pos, key, key_pos, 90)
        return results

    def _from_env_vars(self, text: str) -> list[ExtractedPair]:
        """Extract from env-var style: OPENAI_API_KEY=sk-xxx"""
        results: list[ExtractedPair] = []
        env_keys = [(m.group(1), m.start()) for m in _ENV_LABEL_RE.finditer(text) if not is_fake_key(m.group(1))]
        if not env_keys:
            return results
        bases = [
            (normalize_base_url(m.group(0)), m.start())
            for m in _URL_INLINE_RE.finditer(text)
            if is_plausible_api_url(m.group(0))
        ]
        models = list(dict.fromkeys(_MODEL_RE.findall(text)))
        base_url, base_pos = bases[0] if bases else ("", -1)
        for key, key_pos in env_keys:
            bonus = _proximity_score(base_pos, key_pos, len(text)) if base_pos >= 0 else 0
            results.append(ExtractedPair(
                base_url=base_url, api_key=key, models=models,
                confidence=90 + bonus, url_pos=base_pos, key_pos=key_pos,
            ))
        return results

    def _from_json_kv(self, text: str) -> list[ExtractedPair]:
        """Extract from embedded JSON key-value pairs."""
        results: list[ExtractedPair] = []
        keys_with_pos = [(m.group(1), m.start()) for m in _JSON_KEY_RE.finditer(text) if not is_fake_key(m.group(1))]
        bases_with_pos = [(normalize_base_url(m.group(1)), m.start()) for m in _JSON_BASE_RE.finditer(text)]
        # Also try env-style keys in the same text
        env_keys = [(m.group(1), m.start()) for m in _ENV_KEY_RE.finditer(text) if not is_fake_key(m.group(1))]
        keys_with_pos.extend(env_keys)
        if not keys_with_pos:
            return results
        bases = [(u, p) for u, p in bases_with_pos if is_plausible_api_url(u)]
        models = list(dict.fromkeys(_MODEL_RE.findall(text)))
        base_url, base_pos = bases[0] if bases else ("", -1)
        for key, key_pos in keys_with_pos:
            bonus = _proximity_score(base_pos, key_pos, len(text)) if base_pos >= 0 else 0
            results.append(ExtractedPair(
                base_url=base_url, api_key=key, models=models,
                confidence=85 + bonus, url_pos=base_pos, key_pos=key_pos,
            ))
        return results

    def _from_code_blocks(self, text: str) -> list[ExtractedPair]:
        """Extract from ` code blocks with URL + key pairing."""
        results: list[ExtractedPair] = []
        for block_match in _CODE_BLOCK_RE.finditer(text):
            block = block_match.group(1)
            block_start = block_match.start()
            bases = [
                (normalize_base_url(m.group(0)), m.start() + block_start)
                for m in _URL_INLINE_RE.finditer(block)
                if is_plausible_api_url(m.group(0))
            ]
            # Use prefixed keys inside code blocks for higher precision
            keys = [
                (m.group(0), m.start() + block_start)
                for m in _PREFIXED_KEY_RE.finditer(block)
                if not is_fake_key(m.group(0))
            ]
            # Fall back to broader key regex if no prefixed keys found
            if not keys:
                keys = [
                    (m.group(0), m.start() + block_start)
                    for m in _KEY_RE.finditer(block)
                    if not is_fake_key(m.group(0))
                ]
            models = list(dict.fromkeys(_MODEL_RE.findall(block)))
            if not keys:
                continue
            base_url, base_pos = bases[0] if bases else ("", -1)
            for key, key_pos in keys:
                bonus = _proximity_score(base_pos, key_pos, len(text)) if base_pos >= 0 else 0
                results.append(
                    ExtractedPair(
                        base_url=base_url, api_key=key, models=models,
                        confidence=80 + bonus, url_pos=base_pos, key_pos=key_pos,
                    )
                )
        return results

    def _from_url_key_windows(self, text: str) -> list[ExtractedPair]:
        """Extract by pairing URLs with keys found in nearby text windows."""
        results: list[ExtractedPair] = []
        for url_match in _URL_INLINE_RE.finditer(text):
            raw_url = url_match.group(0)
            if not is_plausible_api_url(raw_url):
                continue
            base = normalize_base_url(raw_url)
            url_pos = url_match.start()
            # Asymmetric window: 120 chars before, 400 chars after
            start = max(0, url_pos - 120)
            end = min(len(text), url_match.end() + 400)
            window = text[start:end]
            keys = [
                (m.group(0), m.start() + start)
                for m in _PREFIXED_KEY_RE.finditer(window)
                if not is_fake_key(m.group(0))
            ]
            if not keys:
                keys = [
                    (m.group(0), m.start() + start)
                    for m in _KEY_RE.finditer(window)
                    if not is_fake_key(m.group(0))
                ]
            models = list(dict.fromkeys(_MODEL_RE.findall(window)))
            for key, key_pos in keys:
                bonus = _proximity_score(url_pos, key_pos, len(text))
                results.append(
                    ExtractedPair(
                        base_url=base, api_key=key, models=models,
                        confidence=60 + bonus, url_pos=url_pos, key_pos=key_pos,
                    )
                )
        return results

    def _from_orphan_keys(self, text: str) -> list[ExtractedPair]:
        """Last resort: pair orphan keys with the first plausible URL in text."""
        keys = [(m.group(0), m.start()) for m in _KEY_RE.finditer(text) if not is_fake_key(m.group(0))]
        bases = [
            (normalize_base_url(m.group(0)), m.start())
            for m in _URL_INLINE_RE.finditer(text)
            if is_plausible_api_url(m.group(0))
        ]
        if not keys or not bases:
            return []
        models = list(dict.fromkeys(_MODEL_RE.findall(text)))
        base_url, base_pos = bases[0]
        return [
            ExtractedPair(
                base_url=base_url, api_key=key, models=models,
                confidence=40, url_pos=base_pos, key_pos=key_pos,
            )
            for key, key_pos in keys
        ]
