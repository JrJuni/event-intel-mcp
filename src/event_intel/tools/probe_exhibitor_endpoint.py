"""probe_exhibitor_endpoint MCP tool handler — Phase 18T T2.

Module-reference imports for monkeypatch safety (project DO NOT rule).
Cold-start safe: no heavy ML imports at module top.
"""
from __future__ import annotations

from event_intel.acquisition import probe as _probe
from event_intel.errors import Stage, envelope_from_exception


def probe_exhibitor_endpoint(
    url: str = "",
    hints: dict | None = None,
    *,
    lang: str = "en",
    allow_cross_origin: bool = False,
) -> dict:
    """Given analyzer hints, probe XHR/embedded-JSON endpoints deterministically.

    Chooses probe_embedded_json when hints contain embedded_json_selectors,
    probe_endpoints otherwise. Always 0 LLM calls.
    """
    try:
        if not url or not url.strip():
            from event_intel.errors import ErrorCode, MCPError
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.ACQUISITION,
                message="url is required",
                hint={"field": "url"},
                retryable=False,
            )

        hints_dict = hints or {}
        selectors = hints_dict.get("embedded_json_selectors")

        if selectors:
            result = _probe.probe_embedded_json(
                url=url,
                hints=hints_dict,
            )
        else:
            result = _probe.probe_endpoints(
                url=url,
                hints=hints_dict,
                lang=lang,
                allow_cross_origin=allow_cross_origin,
            )

        winner = result.winner
        return {
            "ok": True,
            "winner": {
                "url": winner.url,
                "method": winner.method,
                "status": winner.status,
                "score": round(winner.score, 3),
                "warning": winner.warning,
            } if winner else None,
            "attempts": [
                {
                    "url": a.url,
                    "method": a.method,
                    "status": a.status,
                    "score": round(a.score, 3),
                    "error_code": a.error_code.value if a.error_code else None,
                    "error_message": a.error_message,
                    "warning": a.warning,
                }
                for a in result.attempts
            ],
            "body_preview": (result.body or "")[:500] if result.body else None,
            "content_type": result.content_type,
        }

    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.ACQUISITION)
