"""login_chatgpt MCP tool — #14 P1.2 (in-app setup parity).

Lets a non-developer authenticate the ChatGPT OAuth LLM path from inside Claude
Desktop instead of running `event-intel login-chatgpt` in a terminal. ChatGPT
OAuth is the default zero-cost onboarding funnel ([[inapp-setup-parity]]).

NON-BLOCKING: the PKCE flow opens a browser and waits up to ~2 min for the
callback — far past the client request timeout. So the existing (blocking)
`ChatGPTOAuthProvider.login()` is run inside a background job: the tool opens the
browser and returns at once with status=pending; the background thread completes
the token exchange and saves the token. Poll `check_runtime` (or call again)
until status=logged_in. The terminal `login-chatgpt` keeps its inline behavior.

Module-reference imports for monkeypatch safety; cold-import safe (httpx /
webbrowser stay lazy inside the provider's PKCE method).
"""
from __future__ import annotations

from event_intel.errors import Stage, envelope_from_exception
from event_intel.providers import llm as _llm
from event_intel.runtime import async_job as _async_job

# Process-wide login job — one in-flight browser PKCE flow per server (the
# localhost:1455 listener can only bind once; idempotent start guards re-entry).
_login_job = _async_job.BackgroundJob("chatgpt-login")


def login_chatgpt(*, force: bool = False) -> dict:
    """Authenticate the ChatGPT OAuth path. Returns an envelope with ``status``:

    - ``logged_in`` — a valid cached token exists (or login just finished).
    - ``pending``   — a browser was opened; approve it, then poll check_runtime.
    - ``failed``    — the background login failed/timed out; ``error`` + retry.

    ``force=True`` re-authenticates even with a valid cached token.
    """
    try:
        provider = _llm.ChatGPTOAuthProvider()
        if not force:
            st = provider.auth_status()
            if st.get("logged_in"):
                return {
                    "ok": True,
                    "status": "logged_in",
                    "token_path": st.get("token_path"),
                    "message": "ChatGPT is already authenticated.",
                }

        if force:
            _login_job.reset()

        job = _login_job.start(lambda: provider.login(force=force), block=False)
        phase = job.get("phase")
        if phase == "done":
            detail = job.get("detail") or {}
            return {
                "ok": True,
                "status": "logged_in",
                "message": "ChatGPT login complete.",
                **{k: detail[k] for k in ("token_path", "model") if k in detail},
            }
        if phase == "failed":
            return {
                "ok": True,
                "status": "failed",
                "error": job.get("error"),
                "message": "ChatGPT login failed or timed out; call login_chatgpt again to retry.",
            }
        return {
            "ok": True,
            "status": "pending",
            "message": (
                "A browser window was opened for ChatGPT login — approve it there. "
                "Then call check_runtime to confirm (the login check will read "
                "logged_in once the token is saved)."
            ),
        }
    except Exception as exc:
        return envelope_from_exception(exc, stage=Stage.PREFLIGHT)
