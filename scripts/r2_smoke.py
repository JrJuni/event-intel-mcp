"""R2 smoke campaign runner (ZNC plan) — registered in Windows Task Scheduler
every 4 hours (no shell policy involved; the env python runs this directly):

    schtasks /create /tn EventIntelR2Smoke /sc HOURLY /mo 4 /st HH:MM
      /tr "<env-python> <repo>\\scripts\\r2_smoke.py"

Each run: 3 zero-config builds (benchmarks/r2_pairs.yaml — ddgs + Google News
RSS keyless, ChatGPT OAuth LLM) + a retry-stats snapshot. Logs and snapshots
land in benchmarks/_local/ (gitignored). After >=10 runs spread across
time-of-day, R3 codifies the retry policy from the aggregate diagnostics.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG_DIR = REPO / "benchmarks" / "_local"


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / "r2_smoke.log"

    # The campaign measures the KEYLESS lanes (failure patterns for the retry
    # playbook) and must not burn the free Brave quota — pin provider=ddgs
    # regardless of the machine's BRAVE_API_KEY (default is now `auto`).
    keyless_cfg = LOG_DIR / "r2_keyless_config.yaml"
    keyless_cfg.write_text(
        "llm:\n  provider: chatgpt_oauth\nsearch:\n  provider: ddgs\n",
        encoding="utf-8",
    )
    env = {**__import__("os").environ, "EVENT_INTEL_CONFIG": str(keyless_cfg)}

    def run(args: list[str]) -> int:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n=== {stamp} $ {' '.join(args)}\n")
            log.flush()
            proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [sys.executable, "-m", "event_intel.cli", *args],
                cwd=REPO, stdout=log, stderr=subprocess.STDOUT, env=env,
            )
            log.write(f"=== exit {proc.returncode}\n")
            return proc.returncode

    rc1 = run([
        "benchmark", "smoke-batch",
        "--spec", "benchmarks/r2_pairs.yaml",
        "--batch-id", f"r2-{stamp}",
    ])
    rc2 = run([
        "benchmark", "retry-stats",
        "--out", str(LOG_DIR / f"retry_stats_{stamp}.json"),
    ])
    return rc1 or rc2


if __name__ == "__main__":
    raise SystemExit(main())
