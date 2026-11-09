"""Explicit opt-in review decision settings; never inherit review credentials."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

from .decision_provider import digest
from .errors import ReviewInputError


@dataclass(frozen=True)
class DecisionConfiguration:
    enabled: bool = False
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    allow_remote: bool = False
    upstream_provider: str | None = None
    max_calls: int = 4
    max_input_bytes: int = 65_536
    max_output_bytes: int = 32_768
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or type(self.allow_remote) is not bool:
            raise ReviewInputError("decision flags must be booleans")
        for field, ceiling in (
            ("max_calls", 8),
            ("max_input_bytes", 262_144),
            ("max_output_bytes", 65_536),
            ("timeout_ms", 120_000),
        ):
            value = getattr(self, field)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ReviewInputError("decision work bound is invalid")
        if not self.enabled and all(
            v is None
            for v in (
                self.provider,
                self.model,
                self.base_url,
                self.api_key_env,
                self.upstream_provider,
            )
        ):
            return
        if (
            not isinstance(self.provider, str)
            or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.provider) is None
            or not isinstance(self.model, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", self.model)
        ):
            raise ReviewInputError("decision requires an explicit provider and model")
        endpoint = self.endpoint
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port
        except ValueError as exc:
            raise ReviewInputError("decision endpoint is invalid") from exc
        if (
            parsed.username
            or parsed.password
            or "@" in parsed.netloc
            or parsed.query
            or parsed.fragment
            or any(c.isspace() for c in endpoint)
        ):
            raise ReviewInputError("decision endpoint carries unsupported fields")
        if self.provider == "ollama":
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.path.rstrip("/") != "/api"
                or port == 0
                or self.api_key_env is not None
                or self.upstream_provider is not None
                or ":cloud" in self.model
                or self.model.endswith("-cloud")
            ):
                raise ReviewInputError(
                    "decision Ollama requires a literal loopback local endpoint and local model"
                )
        elif self.provider == "openrouter":
            if (
                not self.allow_remote
                or parsed.scheme != "https"
                or parsed.hostname != "openrouter.ai"
                or port not in {None, 443}
                or parsed.path.rstrip("/") != "/api/v1"
            ):
                raise ReviewInputError(
                    "decision OpenRouter requires explicit remote permission and its fixed HTTPS endpoint"
                )
            if not isinstance(self.api_key_env, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]{0,63}", self.api_key_env
            ):
                raise ReviewInputError(
                    "decision OpenRouter requires its own credential variable"
                )
            from .providers.openrouter import OpenRouterRoutingPolicy

            try:
                OpenRouterRoutingPolicy(upstream_provider=self.upstream_provider or "")
            except (ValueError, TypeError) as exc:
                raise ReviewInputError(
                    "decision OpenRouter requires explicit upstream routing"
                ) from exc
        else:
            if (
                self.base_url is None
                or parsed.path == ""
                or self.upstream_provider is not None
            ):
                raise ReviewInputError(
                    "custom decision adapters require an explicit endpoint"
                )
            local = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
            if not local and (
                not self.allow_remote or parsed.scheme != "https" or not parsed.hostname
            ):
                raise ReviewInputError(
                    "custom remote decision adapters require explicit permission"
                )
            if self.api_key_env is not None and (
                not isinstance(self.api_key_env, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", self.api_key_env)
            ):
                raise ReviewInputError("custom decision credential variable is invalid")

    @property
    def endpoint(self) -> str:
        return self.base_url or (
            "http://127.0.0.1:11434/api"
            if self.provider == "ollama"
            else "https://openrouter.ai/api/v1"
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def digest(self) -> str:
        return digest({"policy": "review-decision-v1", **self.to_dict()})

    @classmethod
    def from_dict(cls, value: object) -> DecisionConfiguration:
        if value is None:
            return cls()
        if not isinstance(value, dict) or set(value) - set(cls.__dataclass_fields__):
            raise ReviewInputError("decision configuration contains unsupported fields")
        return cls(**value)
