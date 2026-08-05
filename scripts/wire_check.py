#!/usr/bin/env python3
"""Layer 3 of the anti-regression scheme (docs/designs/multi-provider-design.md
Sec 11.2): a dated, real wire attestation for each provider's native `computer`
tool declaration.

Sends ONE minimal live request per provider - the EXACT tool declaration
`providers.py`'s `Dialect.declare()` would emit for a real session (no
duplicated wire shape to drift out of sync with what the bundle actually
ships), no desktop needed - and records provider, model, tool_type, HTTP
status, error text (if any), and a UTC timestamp into
`tests/fixtures/wire-check.json`.

This script is the ONLY part of layer 3 that touches the network. It is a
ship-gate tool in the same family as `scripts/verify_coexistence.py`
(CONTRIBUTING.md, "The ship gate"): real, live, needs real credentials, NOT
run in default CI, and run manually/periodically by a human before shipping.

The six-lens review (docs/designs/phase2-plans.md, "Council items still
open") found the ORIGINAL design for this layer advisory: "it records; it
does not assert... CI prints its age" - a human is expected to notice a
printed number and act on it, which is the exact class of failure ("393
green tests sitting on a wire nobody had actually exercised") this layer
exists to catch, reproduced by a different name. This script only produces
the attestation; `tests/test_wire_attestation_freshness.py` is the actual
gate - it runs in the NORMAL (offline, no-network, no-keys) test suite and
FAILS the build if the attestation this script writes is missing, records a
rejection, or has gone stale. Refreshing the attestation (this script) and
enforcing its freshness (that test) are deliberately two different files:
one needs the network and real credentials and cannot run in default CI;
the other must run in default CI and must never need either.

Usage:
    .venv/bin/python scripts/wire_check.py                    # all providers
    .venv/bin/python scripts/wire_check.py --only anthropic    # one provider

Requires ANTHROPIC_API_KEY and/or OPENAI_API_KEY in the environment for
whichever provider(s) are being checked - fails loud (not a silent skip) if
the key for a requested provider is missing, per this bundle's own
no-fallbacks rule.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "modules" / "tool-computer-use"))

from amplifier_module_tool_computer_use.providers import ANTHROPIC, OPENAI  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "wire-check.json"

#: One verified (model, tool_type) pair per dialect to probe against the real
#: API. Deliberately NOT `dialect.models` verbatim (that table also carries
#: pairs known to be REJECTED, e.g. Anthropic's wrong-generation pairings in
#: `tool_versions.py`'s evidence table) - this is the one pairing each dialect
#: expects to be ACCEPTED, kept next to (and reviewed alongside) that
#: evidence table so the two cannot silently drift apart.
_PROBE_TARGETS: dict[str, dict[str, str]] = {
    "anthropic": {
        "model": "claude-sonnet-4-5-20250929",
        "tool_type": "computer_20250124",
    },
    "openai": {"model": "gpt-5.5", "tool_type": "computer"},
}


def _commit_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _post(
    url: str, body: dict[str, Any], headers: dict[str, str], model: str, tool_type: str
) -> dict[str, Any]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {
                "model": model,
                "tool_type": tool_type,
                "http_status": resp.status,
                "error": None,
                "checked_at": checked_at,
            }
    except urllib.error.HTTPError as exc:
        return {
            "model": model,
            "tool_type": tool_type,
            "http_status": exc.code,
            "error": exc.read()[:500].decode("utf-8", errors="replace"),
            "checked_at": checked_at,
        }


def _check_anthropic(model: str, tool_type: str) -> dict[str, Any]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set - cannot attest Anthropic's wire shape "
            "live. Fail loud, no silent skip: set the key or omit anthropic via "
            "--only."
        )
    tool = ANTHROPIC.declare(tool_type, width=1280, height=800, enable_zoom=False)
    beta = ANTHROPIC.beta_headers.get(tool_type)
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "reply with the single word: ok"}],
        "tools": [tool],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if beta:
        headers["anthropic-beta"] = beta
    return _post(
        "https://api.anthropic.com/v1/messages", body, headers, model, tool_type
    )


def _check_openai(model: str, tool_type: str) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set - cannot attest OpenAI's wire shape live. "
            "Fail loud, no silent skip: set the key or omit openai via --only."
        )
    tool = OPENAI.declare(tool_type, width=1280, height=800, enable_zoom=False)
    body: dict[str, Any] = {
        "model": model,
        "input": [{"role": "user", "content": "reply with the single word: ok"}],
        "tools": [tool],
        "truncation": "auto",
    }
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    return _post("https://api.openai.com/v1/responses", body, headers, model, tool_type)


_CHECKERS = {"anthropic": _check_anthropic, "openai": _check_openai}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        nargs="+",
        choices=sorted(_CHECKERS),
        default=sorted(_CHECKERS),
        help="Limit to specific provider(s) (default: all).",
    )
    args = parser.parse_args(argv)

    existing: dict[str, Any] = {}
    if FIXTURE_PATH.is_file():
        existing = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    commit_sha = _commit_sha()
    failed: list[str] = []
    for provider in args.only:
        target = _PROBE_TARGETS[provider]
        result = _CHECKERS[provider](target["model"], target["tool_type"])
        result["commit_sha"] = commit_sha
        existing[provider] = result
        status = "OK" if result["http_status"] == 200 else "REJECTED"
        print(
            f"[{status}] {provider}: model={result['model']} "
            f"tool_type={result['tool_type']} http_status={result['http_status']}"
        )
        if result["http_status"] != 200:
            print(f"    error: {result['error']}")
            failed.append(provider)

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE_PATH.write_text(
        json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"\nwrote {FIXTURE_PATH.relative_to(ROOT)}")

    if failed:
        print(
            f"\nFAILED (rejected by the live API, real evidence of wire "
            f"drift): {failed}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
