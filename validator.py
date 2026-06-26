"""Validate discovered tokens by sending lightweight test requests.

Determines protocol compatibility (OpenAI / Anthropic / Responses),
measures latency, and optionally discovers available models.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from sources.base import DiscoveredToken

log = logging.getLogger(__name__)


class Protocol(str, Enum):
    OPENAI_CHAT = "openai_chat"       # /v1/chat/completions
    OPENAI_RESPONSES = "responses"    # /v1/responses
    ANTHROPIC = "anthropic"           # /v1/messages
    UNKNOWN = "unknown"


@dataclass
class ValidationResult:
    token: DiscoveredToken
    protocol: Protocol = Protocol.UNKNOWN
    is_healthy: bool = False
    latency_ms: int = 0
    http_status: int = 0
    error: str = ""
    discovered_models: list[str] = field(default_factory=list)
    rate_limited: bool = False

    @property
    def app_type(self) -> str:
        """Map protocol to cc-switch app_type."""
        if self.protocol == Protocol.OPENAI_RESPONSES:
            return "codex"
        if self.protocol == Protocol.OPENAI_CHAT:
            return "openclaw"
        if self.protocol == Protocol.ANTHROPIC:
            return "claude"
        return "openclaw"


class TokenValidator:
    """Async validator that probes endpoints concurrently."""

    def __init__(self, network_cfg: dict, validator_cfg: dict):
        self.proxy = network_cfg.get("proxy")
        self.timeout = validator_cfg.get("request_timeout_seconds", 10)
        self.max_concurrent = validator_cfg.get("max_concurrent", 10)
        self.test_model_openai = validator_cfg.get("test_model_openai", "gpt-4o")
        self.test_model_anthropic = validator_cfg.get(
            "test_model_anthropic", "claude-sonnet-4-20250514"
        )
        self.user_agent = network_cfg.get("user_agent", "Mozilla/5.0")

    async def validate_all(self, tokens: list[DiscoveredToken]) -> list[ValidationResult]:
        """Validate a batch of tokens concurrently."""
        sem = asyncio.Semaphore(self.max_concurrent)
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            proxy=self.proxy,
            headers={"User-Agent": self.user_agent},
        ) as client:
            tasks = [self._validate_one(client, sem, t) for t in tokens]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        valid: list[ValidationResult] = []
        for t, res in zip(tokens, results):
            if isinstance(res, Exception):
                log.warning("Validation exception for %s: %s", t.uid, res)
                continue
            if res.is_healthy:
                valid.append(res)
                log.info(
                    "  OK  %s  %s  %sms  models=%s",
                    res.protocol.value, t.base_url, res.latency_ms,
                    ",".join(res.discovered_models[:3]) if res.discovered_models else "-",
                )
            else:
                reason = "rate_limited" if res.rate_limited else res.error or f"HTTP {res.http_status}"
                log.info("  FAIL %s  %s", t.base_url, reason)
        return valid

    # -- Internal probes -----------------------------------------------

    async def _validate_one(
        self, client: httpx.AsyncClient, sem: asyncio.Semaphore, token: DiscoveredToken
    ) -> ValidationResult:
        async with sem:
            vr = ValidationResult(token=token)

            # 1) Try OpenAI chat completions
            ok = await self._probe_openai_chat(client, token, vr)
            if ok:
                return vr

            # 2) Try OpenAI responses API (Codex format)
            ok = await self._probe_openai_responses(client, token, vr)
            if ok:
                return vr

            # 3) Try Anthropic messages
            ok = await self._probe_anthropic(client, token, vr)
            if ok:
                return vr

            return vr

    def _build_headers(self, token: DiscoveredToken, extra: dict | None = None) -> dict:
        h: dict[str, str] = {"User-Agent": self.user_agent}
        if extra:
            h.update(extra)
        return h

    async def _probe_openai_chat(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = f"{token.base_url.rstrip('/')}/chat/completions"
        headers = self._build_headers(token, {
            "Authorization": f"Bearer {token.api_key}",
            "Content-Type": "application/json",
        })
        body = {
            "model": token.raw_models[0] if token.raw_models else self.test_model_openai,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
        return await self._send_probe(client, url, headers, body, vr, Protocol.OPENAI_CHAT)

    async def _probe_openai_responses(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = f"{token.base_url.rstrip('/')}/responses"
        headers = self._build_headers(token, {
            "Authorization": f"Bearer {token.api_key}",
            "Content-Type": "application/json",
        })
        body = {
            "model": token.raw_models[0] if token.raw_models else self.test_model_openai,
            "input": "hi",
            "max_output_tokens": 1,
        }
        return await self._send_probe(client, url, headers, body, vr, Protocol.OPENAI_RESPONSES)

    async def _probe_anthropic(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = f"{token.base_url.rstrip('/')}/messages"
        headers = self._build_headers(token, {
            "x-api-key": token.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        })
        body = {
            "model": token.raw_models[0] if token.raw_models else self.test_model_anthropic,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
        return await self._send_probe(client, url, headers, body, vr, Protocol.ANTHROPIC)

    async def _send_probe(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict,
        body: dict,
        vr: ValidationResult,
        protocol: Protocol,
    ) -> bool:
        """Send a probe request. Returns True if the endpoint is healthy."""
        t0 = time.monotonic()
        try:
            r = await client.post(url, headers=headers, json=body)
            elapsed = int((time.monotonic() - t0) * 1000)
            vr.http_status = r.status_code
            vr.latency_ms = elapsed

            if r.status_code == 429:
                vr.rate_limited = True
                vr.error = "rate_limited"
                return False

            if r.status_code in (401, 403):
                vr.error = f"auth_error_{r.status_code}"
                return False

            # Try to parse response for success signals
            try:
                data = r.json()
            except Exception:
                vr.error = f"non_json_response_{r.status_code}"
                return False

            # Check for valid OpenAI chat response
            if protocol == Protocol.OPENAI_CHAT:
                if "choices" in data or "id" in data:
                    vr.protocol = protocol
                    vr.is_healthy = True
                    # Try to extract model info
                    model = data.get("model")
                    if model:
                        vr.discovered_models = [model]
                    return True
                # Some proxies return 200 with error in body
                err = data.get("error", {})
                if isinstance(err, dict) and err.get("code") in ("model_not_found",):
                    vr.error = "model_not_found"
                    return False
                # 200 but no choices — still treat as alive if no hard error
                if r.status_code == 200 and "error" not in data:
                    vr.protocol = protocol
                    vr.is_healthy = True
                    return True

            # Check for valid OpenAI responses response
            if protocol == Protocol.OPENAI_RESPONSES:
                if "id" in data or "output" in data:
                    vr.protocol = protocol
                    vr.is_healthy = True
                    model = data.get("model")
                    if model:
                        vr.discovered_models = [model]
                    return True
                err = data.get("error", {})
                if isinstance(err, dict) and err.get("code") in ("model_not_found",):
                    vr.error = "model_not_found"
                    return False
                if r.status_code == 200 and "error" not in data:
                    vr.protocol = protocol
                    vr.is_healthy = True
                    return True

            # Check for valid Anthropic response
            if protocol == Protocol.ANTHROPIC:
                if data.get("type") == "message" or "content" in data:
                    vr.protocol = protocol
                    vr.is_healthy = True
                    model = data.get("model")
                    if model:
                        vr.discovered_models = [model]
                    return True
                err_type = data.get("type") or ""
                if err_type == "error" or "error" in data:
                    vr.error = str(data.get("error", {}).get("message", "anthropic_error"))
                    return False
                if r.status_code == 200:
                    vr.protocol = protocol
                    vr.is_healthy = True
                    return True

            vr.error = f"unexpected_response_{r.status_code}"
            return False

        except httpx.TimeoutException:
            vr.error = "timeout"
            return False
        except Exception as e:
            vr.error = str(e)[:120]
            return False

    async def discover_models(self, token: DiscoveredToken) -> list[str]:
        """Try GET /models to list available models."""
        url = f"{token.base_url.rstrip('/')}/models"
        headers = {
            "Authorization": f"Bearer {token.api_key}",
            "User-Agent": self.user_agent,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, proxy=self.proxy, follow_redirects=True
            ) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    models = data.get("data", [])
                    return [m.get("id", "") for m in models if isinstance(m, dict)]
        except Exception:
            pass
        return []
    def _build_headers(self, token: DiscoveredToken, extra: dict | None = None) -> dict:
        h: dict[str, str] = {"User-Agent": self.user_agent}
        if extra:
            h.update(extra)
        return h

    async def _probe_openai_chat(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = f"{token.base_url.rstrip('/')}/chat/completions"
        auth_headers: dict[str, str] = {}
        if token.api_key:
            auth_headers["Authorization"] = f"Bearer {token.api_key}"
        headers = self._build_headers(token, {
            **auth_headers,
            "Content-Type": "application/json",
        })
        body = {
            "model": token.raw_models[0] if token.raw_models else self.test_model_openai,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
        return await self._send_probe(client, url, headers, body, vr, Protocol.OPENAI_CHAT)

    async def _probe_openai_responses(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = f"{token.base_url.rstrip('/')}/responses"
        auth_headers: dict[str, str] = {}
        if token.api_key:
            auth_headers["Authorization"] = f"Bearer {token.api_key}"
        headers = self._build_headers(token, {
            **auth_headers,
            "Content-Type": "application/json",
        })
        body = {
            "model": token.raw_models[0] if token.raw_models else self.test_model_openai,
            "input": "hi",
            "max_output_tokens": 1,
        }
        return await self._send_probe(client, url, headers, body, vr, Protocol.OPENAI_RESPONSES)

    async def _probe_anthropic(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = f"{token.base_url.rstrip('/')}/messages"
        auth_headers: dict[str, str] = {}
        if token.api_key:
            auth_headers["x-api-key"] = token.api_key
            auth_headers["anthropic-version"] = "2023-06-01"
        headers = self._build_headers(token, {
            **auth_headers,
            "Content-Type": "application/json",
        })
        body = {
            "model": token.raw_models[0] if token.raw_models else self.test_model_anthropic,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }
        return await self._send_probe(client, url, headers, body, vr, Protocol.ANTHROPIC)
