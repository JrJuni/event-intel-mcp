"""Host critique protocol — BD critique harness S3.

The judge is the HOST (in-session Claude), not an API LLM (plan Q3). So this
module does not call any model: it loads the multi-lens panel prompt and renders
a single host-ready *brief* (panel rubric + product context + the S/A picks + the
JSON contract + the pair/packet_sha to echo). The host reads the brief, applies
the panel, and emits the critique JSON validated by critique_packet.parse_critique.

stdlib only at import (cold-start safe).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def load_panel_prompt(lang: str = "en") -> str:
    """Load critique_panel.txt for ``lang``, falling back to en."""
    base = Path(__file__).resolve().parents[1] / "prompts"
    for p in (base / lang / "critique_panel.txt", base / "en" / "critique_panel.txt"):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError("critique_panel.txt prompt not found")


def render_critique_brief(packet: dict[str, Any], *, lang: str = "en") -> str:
    """Combine the panel prompt + a readable rendering of the packet into one
    host-ready brief. The host critiques from this text and echoes pair/packet_sha.
    """
    prompt = load_panel_prompt(lang)
    picks = packet.get("picks", []) or []
    lines = [
        prompt.strip(),
        "",
        "--- BRIEF ---",
        f"pair: {packet.get('pair', '')}",
        f"packet_sha: {packet.get('packet_sha', '')}",
        "",
        "PRODUCT:",
        packet.get("product_header", "") or "(none)",
        "",
        f"S/A PICKS ({len(picks)}):",
    ]
    for i, p in enumerate(picks, 1):
        ev = "; ".join(
            f"{e.get('type')}:{e.get('url')}" for e in (p.get("evidence") or [])
        ) or "(none)"
        lines += [
            f"{i}. {p.get('name', '')} — tier {p.get('tier')}, "
            f"score {p.get('final_score')}, capability_fit {p.get('capability_fit')}",
            f"   rationale: {p.get('rationale') or '(none)'}",
            f"   evidence: {ev}",
        ]
    return "\n".join(lines)
