"""Transport selection for the MCP server (Y2.2c).

stdio (default) keeps the current Claude Desktop / CLI behavior unchanged.
streamable-http is OPT-IN and binds to 127.0.0.1 (loopback) by default — it does
NOT expose the server to the network on its own. DNS-rebinding / Origin
protection is on by default (FastMCP ships a localhost-only allowlist). Real
remote exposure requires a deliberate host/origin change AND the authorization
layer (Y2.2d) in front; until that lands, do not bind a public interface.

Env knobs (all optional; absent = stdio, unchanged behavior):
  EVENT_INTEL_TRANSPORT          stdio | streamable-http  (alias: http)
  EVENT_INTEL_HTTP_HOST          default 127.0.0.1
  EVENT_INTEL_HTTP_PORT          default 8000
  EVENT_INTEL_HTTP_ALLOWED_HOSTS    comma list (default: FastMCP localhost allowlist)
  EVENT_INTEL_HTTP_ALLOWED_ORIGINS  comma list (default: FastMCP localhost allowlist)

stdlib + errors only at import (cold-start safe); the mcp transport-security type
is imported lazily inside apply_to_app.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from event_intel.errors import ErrorCode, MCPError, Stage

_VALID_TRANSPORTS: tuple[str, ...] = ("stdio", "streamable-http")
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


@dataclass(frozen=True)
class TransportConfig:
    transport: str = "stdio"
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    @property
    def is_http(self) -> bool:
        return self.transport == "streamable-http"

    @property
    def binds_non_loopback(self) -> bool:
        return self.host not in _LOOPBACK_HOSTS


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _config_error(message: str, **hint: Any) -> MCPError:
    return MCPError(
        error_code=ErrorCode.CONFIG_ERROR,
        stage=Stage.PREFLIGHT,
        message=message,
        hint=hint,
        retryable=False,
    )


def resolve_transport_config() -> TransportConfig:
    """Resolve transport settings from the environment. Cold-start safe.

    Invalid values fail loud as CONFIG_ERROR so a deploy misconfig surfaces at
    startup rather than mid-serve.
    """
    raw_t = os.environ.get("EVENT_INTEL_TRANSPORT")
    transport = (raw_t or "stdio").strip() or "stdio"
    if transport == "http":  # convenience alias
        transport = "streamable-http"
    if transport not in _VALID_TRANSPORTS:
        raise _config_error(
            f"invalid EVENT_INTEL_TRANSPORT: {transport!r}",
            env_var="EVENT_INTEL_TRANSPORT",
            allowed=list(_VALID_TRANSPORTS),
            fix="Set EVENT_INTEL_TRANSPORT to one of: stdio, streamable-http",
        )

    host = (os.environ.get("EVENT_INTEL_HTTP_HOST") or _DEFAULT_HOST).strip() or _DEFAULT_HOST

    raw_port = os.environ.get("EVENT_INTEL_HTTP_PORT")
    if raw_port and raw_port.strip():
        try:
            port = int(raw_port.strip())
        except ValueError as exc:
            raise _config_error(
                f"EVENT_INTEL_HTTP_PORT must be an integer, got {raw_port!r}",
                env_var="EVENT_INTEL_HTTP_PORT",
                fix="Set EVENT_INTEL_HTTP_PORT to a port number in 1..65535",
            ) from exc
        if not (1 <= port <= 65535):
            raise _config_error(
                f"EVENT_INTEL_HTTP_PORT out of range: {port}",
                env_var="EVENT_INTEL_HTTP_PORT",
                fix="Set EVENT_INTEL_HTTP_PORT to a port number in 1..65535",
            )
    else:
        port = _DEFAULT_PORT

    return TransportConfig(
        transport=transport,
        host=host,
        port=port,
        allowed_hosts=_split_csv(os.environ.get("EVENT_INTEL_HTTP_ALLOWED_HOSTS")),
        allowed_origins=_split_csv(os.environ.get("EVENT_INTEL_HTTP_ALLOWED_ORIGINS")),
    )


def apply_to_app(app: Any, cfg: TransportConfig) -> None:
    """Apply an http TransportConfig to a FastMCP app's settings.

    Only mutates settings for the http transport. When the operator supplies
    explicit allowlists we build a TransportSecuritySettings with DNS-rebinding
    protection ON; otherwise FastMCP's default localhost allowlist stands.
    """
    if not cfg.is_http:
        return
    app.settings.host = cfg.host
    app.settings.port = cfg.port
    if cfg.allowed_hosts or cfg.allowed_origins:
        from mcp.server.transport_security import TransportSecuritySettings

        app.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(cfg.allowed_hosts),
            allowed_origins=list(cfg.allowed_origins),
        )
