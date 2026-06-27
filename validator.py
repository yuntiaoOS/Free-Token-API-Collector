"""Validate discovered tokens by sending lightweight test requests."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

import httpx

from sources.base import DiscoveredToken

log = logging.getLogger(__name__)


class Protocol(str, Enum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "responses"
    ANTHROPIC = "anthropic"
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
    sample_reply: str = ""

    @property
    def provider_id(self) -> str:
        return str(self.token.extra.get("provider_id") or self.token.uid)

    @property
    def app_type(self) -> str:
        if self.protocol == Protocol.OPENAI_RESPONSES:
            return "codex"
        if self.protocol == Protocol.OPENAI_CHAT:
            return "openclaw"
        if self.protocol == Protocol.ANTHROPIC:
            return "claude"
        return str(self.token.extra.get("app_type") or "openclaw")


class TokenValidator:
    """Async validator that probes endpoints concurrently."""

    _APP_TYPE_PROBE_ORDER: dict[str, tuple[str, ...]] = {
        "openclaw": ("openai_chat",),
        "codex": ("responses",),
        "claude": ("anthropic",),
    }

    def __init__(self, network_cfg: dict, validator_cfg: dict):
        self.proxy = network_cfg.get("proxy")
        self.timeout = validator_cfg.get("request_timeout_seconds", 10)
        self.max_concurrent = validator_cfg.get("max_concurrent", 10)
        self.discover_models_enabled = validator_cfg.get("discover_models", True)
        self.test_model_openai = validator_cfg.get("test_model_openai", "gpt-4o")
        self.test_model_anthropic = validator_cfg.get(
            "test_model_anthropic", "claude-sonnet-4-20250514"
        )
        self.test_prompt = validator_cfg.get(
            "test_prompt", "请用一句话回答：1+1等于几？只回复数字或简短答案。"
        )
        self.test_max_tokens = validator_cfg.get("test_max_tokens", 32)
        self.test_max_output_tokens = validator_cfg.get(
            "test_max_output_tokens", max(self.test_max_tokens * 4, 128)
        )
        self.min_reply_chars = validator_cfg.get("min_reply_chars", 1)
        self.prefer_codex = validator_cfg.get("prefer_codex", True)
        self.max_models_to_try = validator_cfg.get("max_models_to_try", 12)
        self.user_agent = network_cfg.get("user_agent", "Mozilla/5.0")

    async def prepare_tokens(self, tokens: list[DiscoveredToken]) -> None:
        """Optionally discover models via GET /models before validation."""
        if not self.discover_models_enabled:
            return

        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            proxy=self.proxy,
            headers={"User-Agent": self.user_agent},
        ) as client:
            for token in tokens:
                discovered = await self.discover_models(token, client=client)
                scraped = list(token.raw_models)
                merged: list[str] = []
                for model in discovered + scraped:
                    if model and model not in merged:
                        merged.append(model)
                if not merged:
                    merged = [self.test_model_openai]
                token.raw_models = merged[: self.max_models_to_try]

    async def validate_all(self, tokens: list[DiscoveredToken]) -> list[ValidationResult]:
        """Validate a batch of tokens concurrently and keep both success and failure results."""
        await self.prepare_tokens(tokens)
        sem = asyncio.Semaphore(self.max_concurrent)
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            proxy=self.proxy,
            headers={"User-Agent": self.user_agent},
        ) as client:
            tasks = [self._validate_one(client, sem, token) for token in tokens]
            raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: list[ValidationResult] = []
        for token, raw in zip(tokens, raw_results):
            if isinstance(raw, Exception):
                vr = ValidationResult(token=token, error=str(raw)[:120])
                log.warning("Validation exception for %s: %s", token.uid, raw)
            else:
                vr = raw

            results.append(vr)
            if vr.is_healthy:
                reply_preview = vr.sample_reply[:60] + ("..." if len(vr.sample_reply) > 60 else "")
                log.info(
                    "  OK  %s  %s  %sms  models=%s  reply=%r",
                    vr.protocol.value,
                    token.base_url,
                    vr.latency_ms,
                    ",".join(vr.discovered_models[:3]) if vr.discovered_models else "-",
                    reply_preview,
                )
            else:
                reason = "rate_limited" if vr.rate_limited else vr.error or f"HTTP {vr.http_status}"
                log.info("  FAIL %s  %s", token.base_url, reason)

        return results

    async def _validate_one(
        self, client: httpx.AsyncClient, sem: asyncio.Semaphore, token: DiscoveredToken
    ) -> ValidationResult:
        async with sem:
            vr = ValidationResult(token=token)
            if not token.base_url.strip():
                vr.error = "missing_base_url"
                return vr
            if not token.api_key.strip():
                vr.error = "missing_api_key"
                return vr

            known_app_type = str(token.extra.get("app_type") or "")
            strict_protocol = bool(known_app_type)
            probe_order = self._probe_order_for_app_type(known_app_type, strict=strict_protocol)
            if self._skip_anthropic_probe(token.base_url):
                probe_order = tuple(name for name in probe_order if name != "anthropic")

            for probe_name in probe_order:
                vr.rate_limited = False
                if await self._run_probe(client, token, vr, probe_name):
                    return vr
            return vr

    def _default_probe_order(self) -> tuple[str, ...]:
        if self.prefer_codex:
            return ("responses", "openai_chat", "anthropic")
        return ("openai_chat", "anthropic", "responses")

    @staticmethod
    def _skip_anthropic_probe(base_url: str) -> bool:
        lowered = base_url.lower()
        if "/anthropic" in lowered or "claude" in lowered:
            return False
        openai_hints = (
            "xiaomimimo", "mimo", "openai", "pekpik", "deepseek", "gptgod",
            "openrouter", "siliconflow", "moonshot", "grok", "gemini",
        )
        return any(hint in lowered for hint in openai_hints) or lowered.rstrip("/").endswith("/v1")

    def _probe_order_for_app_type(self, app_type: str, *, strict: bool = False) -> tuple[str, ...]:
        default = self._default_probe_order()
        preferred = self._APP_TYPE_PROBE_ORDER.get(app_type)
        if preferred and strict:
            return preferred
        if preferred:
            return preferred + tuple(name for name in default if name not in preferred)
        return default

    async def _run_probe(
        self,
        client: httpx.AsyncClient,
        token: DiscoveredToken,
        vr: ValidationResult,
        probe_name: str,
    ) -> bool:
        if probe_name == "openai_chat":
            return await self._probe_openai_chat(client, token, vr)
        if probe_name == "responses":
            return await self._probe_openai_responses(client, token, vr)
        if probe_name == "anthropic":
            return await self._probe_anthropic(client, token, vr)
        return False

    def _build_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent}
        if extra:
            headers.update(extra)
        return headers

    def _candidate_models(self, token: DiscoveredToken, protocol: Protocol) -> list[str]:
        fallback = self.test_model_openai
        if protocol == Protocol.ANTHROPIC:
            fallback = self.test_model_anthropic
        models: list[str] = []
        host = token.base_url.lower()
        if "xiaomimimo" in host or "mimo" in host:
            for model in ("mimo-v2.5", "mimo-v2-flash", "mimo-v2.5-pro"):
                if model not in models:
                    models.append(model)
        if protocol == Protocol.ANTHROPIC and ("xiaomimimo" in host or "mimo" in host):
            for model in ("mimo-v2.5", "mimo-v2.5-pro"):
                if model not in models:
                    models.insert(0, model)
        for model in token.raw_models:
            if model and model not in models:
                models.append(model)
        if fallback not in models:
            models.append(fallback)
        return models[: self.max_models_to_try]

    async def _probe_openai_chat(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = f"{token.base_url.rstrip('/')}/chat/completions"
        headers = self._build_headers(
            {
                "Authorization": f"Bearer {token.api_key}",
                "Content-Type": "application/json",
            }
        )
        for model in self._candidate_models(token, Protocol.OPENAI_CHAT):
            body = {
                "model": model,
                "messages": [{"role": "user", "content": self.test_prompt}],
                "max_tokens": self.test_max_tokens,
            }
            outcome = await self._send_probe(client, url, headers, body, vr, Protocol.OPENAI_CHAT)
            if outcome == "success":
                return True
            if outcome != "next_model":
                return False
        return False

    async def _probe_openai_responses(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = f"{token.base_url.rstrip('/')}/responses"
        headers = self._build_headers(
            {
                "Authorization": f"Bearer {token.api_key}",
                "Content-Type": "application/json",
            }
        )
        for model in self._candidate_models(token, Protocol.OPENAI_RESPONSES):
            body = {
                "model": model,
                "input": self.test_prompt,
                "max_output_tokens": self.test_max_output_tokens,
            }
            outcome = await self._send_probe(client, url, headers, body, vr, Protocol.OPENAI_RESPONSES)
            if outcome == "success":
                return True
            if outcome == "unsupported_protocol":
                return False
            if outcome != "next_model":
                return False
        return False

    @staticmethod
    def _anthropic_messages_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if normalized.lower().endswith("/v1"):
            return f"{normalized}/messages"
        return f"{normalized}/v1/messages"

    async def _probe_anthropic(
        self, client: httpx.AsyncClient, token: DiscoveredToken, vr: ValidationResult
    ) -> bool:
        url = self._anthropic_messages_url(token.base_url)
        headers = self._build_headers(
            {
                "x-api-key": token.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
        )
        for model in self._candidate_models(token, Protocol.ANTHROPIC):
            body = {
                "model": model,
                "messages": [{"role": "user", "content": self.test_prompt}],
                "max_tokens": self.test_max_tokens,
            }
            outcome = await self._send_probe(client, url, headers, body, vr, Protocol.ANTHROPIC)
            if outcome == "success":
                return True
            if outcome != "next_model":
                return False
        return False

    async def _send_probe(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
        vr: ValidationResult,
        protocol: Protocol,
    ) -> Literal["success", "fail", "next_model", "unsupported_protocol"]:
        """Send a probe request and only accept protocol-shaped successful responses."""
        t0 = time.monotonic()
        try:
            response = await client.post(url, headers=headers, json=body)
            vr.http_status = response.status_code
            vr.latency_ms = int((time.monotonic() - t0) * 1000)

            if response.status_code == 429:
                vr.rate_limited = True
                vr.error = "rate_limited"
                if protocol == Protocol.OPENAI_RESPONSES:
                    return "unsupported_protocol"
                return "fail"
            if response.status_code in (404, 405) and protocol == Protocol.OPENAI_RESPONSES:
                vr.error = f"unsupported_protocol_{response.status_code}"
                return "unsupported_protocol"
            if response.status_code in (401, 403):
                err_text = ""
                try:
                    err_text = self._extract_error(response.json(), "")
                except Exception:
                    err_text = response.text[:200]
                if self._is_model_access_denied(response.status_code, err_text):
                    vr.error = f"model_access_denied:{body.get('model', '')}"
                    return "next_model"
                vr.error = f"auth_error_{response.status_code}"
                return "fail"
            if response.status_code >= 500:
                vr.error = f"server_error_{response.status_code}"
                return "fail"

            try:
                data = response.json()
            except Exception:
                vr.error = f"non_json_response_{response.status_code}"
                if response.status_code in (404, 405) and protocol == Protocol.OPENAI_RESPONSES:
                    return "unsupported_protocol"
                return "fail"

            if protocol == Protocol.OPENAI_CHAT:
                if isinstance(data.get("choices"), list) and data["choices"]:
                    return "success" if self._mark_healthy(vr, protocol, data) else "fail"
                vr.error = self._extract_error(data, "missing_choices")
                if self._is_credit_exhausted(vr.error):
                    return "fail"
                if self._is_model_access_denied(response.status_code, vr.error):
                    return "next_model"
                return "fail"

            if protocol == Protocol.OPENAI_RESPONSES:
                output_text = data.get("output_text")
                if isinstance(output_text, str) and output_text.strip():
                    return "success" if self._mark_healthy(vr, protocol, data) else "fail"
                output = data.get("output")
                if (
                    data.get("object") == "response"
                    and isinstance(data.get("id"), str)
                    and isinstance(output, list)
                    and output
                ):
                    return "success" if self._mark_healthy(vr, protocol, data) else "fail"
                vr.error = self._extract_error(data, "invalid_responses_shape")
                if self._is_model_access_denied(response.status_code, vr.error):
                    return "next_model"
                return "fail"

            if protocol == Protocol.ANTHROPIC:
                content = data.get("content")
                if data.get("type") == "message" and isinstance(content, list):
                    return "success" if self._mark_healthy(vr, protocol, data) else "fail"
                vr.error = self._extract_error(data, "missing_message_content")
                if self._is_model_access_denied(response.status_code, vr.error):
                    return "next_model"
                return "fail"

            vr.error = "unsupported_protocol"
            return "fail"

        except httpx.TimeoutException:
            vr.error = "timeout"
            return "fail"
        except Exception as e:
            vr.error = str(e)[:120]
            return "fail"

    @staticmethod
    def _is_credit_exhausted(message: str) -> bool:
        lowered = (message or "").lower()
        hints = (
            "insufficient credits",
            "never purchased credits",
            "can only afford 0",
            "no endpoints found",
            "quota exceeded",
            "balance",
            "余额不足",
            "额度",
        )
        return any(hint in lowered for hint in hints)

    @staticmethod
    def _is_model_access_denied(status_code: int, message: str) -> bool:
        lowered = (message or "").lower()
        hints = (
            "no access to model",
            "does not have access",
            "model not found",
            "invalid model",
            "model_not_found",
            "unsupported model",
            "not allowed to use model",
        )
        if any(hint in lowered for hint in hints):
            return True
        return status_code == 404 and "model" in lowered

    @staticmethod
    def _extract_error(data: Any, fallback: str) -> str:
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err.get("code") or fallback)
            if isinstance(err, str) and err:
                return err
            if isinstance(data.get("message"), str) and data["message"]:
                return data["message"]
            if isinstance(data.get("type"), str) and data["type"] == "error":
                return str(data.get("message") or fallback)
        return fallback

    def _mark_healthy(self, vr: ValidationResult, protocol: Protocol, data: dict[str, Any]) -> bool:
        reply = self._extract_reply_content(protocol, data)
        if len(reply.strip()) < self.min_reply_chars:
            vr.error = "empty_or_too_short_reply"
            return False

        vr.protocol = protocol
        vr.is_healthy = True
        vr.sample_reply = reply.strip()[:500]
        validated_model = data.get("model")
        if isinstance(validated_model, str) and validated_model:
            vr.discovered_models = [validated_model]
        else:
            vr.discovered_models = []
        return True

    @staticmethod
    def _extract_reply_content(protocol: Protocol, data: dict[str, Any]) -> str:
        if protocol == Protocol.OPENAI_CHAT:
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0]
                if isinstance(choice, dict):
                    message = choice.get("message")
                    if isinstance(message, dict):
                        content = message.get("content")
                        if isinstance(content, str) and content.strip():
                            return content
                        reasoning = message.get("reasoning_content")
                        if isinstance(reasoning, str) and reasoning.strip():
                            return reasoning
                    text = choice.get("text")
                    if isinstance(text, str):
                        return text
            return ""

        if protocol == Protocol.OPENAI_RESPONSES:
            output_text = data.get("output_text")
            if isinstance(output_text, str) and output_text.strip():
                return output_text
            output = data.get("output")
            if isinstance(output, list):
                parts: list[str] = []
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "message":
                        content = item.get("content")
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get("type") == "output_text":
                                    text = block.get("text")
                                    if isinstance(text, str) and text:
                                        parts.append(text)
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        parts.append(text)
                return "".join(parts)
            return ""

        if protocol == Protocol.ANTHROPIC:
            content = data.get("content")
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text:
                            parts.append(text)
                    if block.get("type") == "thinking":
                        thinking = block.get("thinking")
                        if isinstance(thinking, str) and thinking:
                            parts.append(thinking)
                return "".join(parts)
            return ""

        return ""

    async def discover_models(
        self,
        token: DiscoveredToken,
        client: httpx.AsyncClient | None = None,
    ) -> list[str]:
        """Try GET /models to list available models."""
        if not token.api_key:
            return []

        url = f"{token.base_url.rstrip('/')}/models"
        headers = {
            "Authorization": f"Bearer {token.api_key}",
            "User-Agent": self.user_agent,
        }
        try:
            if client is None:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    proxy=self.proxy,
                    follow_redirects=True,
                ) as owned_client:
                    response = await owned_client.get(url, headers=headers)
            else:
                response = await client.get(url, headers=headers)

            if response.status_code == 200:
                data = response.json()
                models = data.get("data", [])
                return [m.get("id", "") for m in models if isinstance(m, dict) and m.get("id")]
            log.debug("Model discovery failed for %s: HTTP %s", token.base_url, response.status_code)
        except Exception as e:
            log.debug("Model discovery error for %s: %s", token.base_url, e)
        return []
