"""Base classes and data models for token sources."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx


@dataclass
class DiscoveredToken:
    """A single discovered API endpoint + key from any source."""

    source: str                    # e.g. "github:chatanywhere/GPT_API_free"
    base_url: str                  # e.g. "https://api.example.com/v1"
    api_key: str                   # bearer token
    raw_models: list[str] = field(default_factory=list)
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> str:
        """Stable identifier derived from base_url + api_key."""
        h = hashlib.sha256(f"{self.base_url}|{self.api_key}".encode()).hexdigest()[:12]
        return f"auto-{h}"

    def __repr__(self) -> str:
        return f"<Token {self.uid} url={self.base_url[:50]} key={self.api_key[:10]}...>"


class BaseSource(ABC):
    """Abstract base for all token sources."""

    def __init__(self, config: dict, network_config: dict):
        self.config = config
        self.proxy = network_config.get("proxy")
        self.timeout = network_config.get("timeout_seconds", 15)
        self.user_agent = network_config.get(
            "user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/137.0",
        )

    def _headers(self, extra: dict | None = None) -> dict:
        h = {"User-Agent": self.user_agent}
        if extra:
            h.update(extra)
        return h

    def _client(self, **kwargs) -> httpx.Client:
        kw: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            "headers": self._headers(),
        }
        if self.proxy:
            kw["proxy"] = self.proxy
        kw.update(kwargs)
        return httpx.Client(**kw)

    @abstractmethod
    def collect(self) -> list[DiscoveredToken]:
        """Run the source and return discovered tokens."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable source name."""
