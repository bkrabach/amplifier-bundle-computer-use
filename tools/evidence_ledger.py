"""The evidence ledger: makes "I verified X" a checkable artifact.

This project has been burned three times by a report that did not match the
tree: a `GUARD_MEASURED` flag reported `True` that was actually still
`False` (twice, on two different platforms), and a harness that reported
PASS while it was actually swallowing its own `TypeError`. Each was caught
only by a human's manual spot-check - there was no artifact a reviewer
could check *instead of* re-deriving the whole investigation by hand.

The fix here is not "try harder to be honest" - it's a data structure that
makes an unverified or false claim structurally visible:

* A `claim` is just a sentence someone wants to assert is true.
* A `verification` is something the LEDGER ITSELF ran (a subprocess, a
  callable) and the REAL output it observed - never a self-report the
  caller hands in unchecked.
* A claim with zero verifications attached is `UNVERIFIED` in the summary,
  full stop - it never silently reads as "fine" the way an omitted line in
  a hand-written report does.
* A verification that raises is a FAILED verification, with the exception
  recorded as the actual output - never silently treated as a pass. This is
  the direct fix for incident 3 (a harness that reported PASS on its own
  `TypeError`): route the check through `verify_callable`/`run_command` and
  a raised exception cannot become a silent pass, because the ledger is the
  one catching it, not the caller's own try/except.

No fallbacks: `all_verified()` is `True` only when every registered claim
has at least one verification and every verification's verdict is `True`.
There is no partial-credit mode.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class EvidenceLedgerError(RuntimeError):
    """Programming error against the ledger API itself (e.g. unknown claim id)."""


@dataclass(frozen=True)
class Verification:
    """One real check attached to a claim.

    `command` is a human-readable description of what was actually run
    (a shell command line, or a short description of a callable/comparison).
    `actual_output` is the REAL thing observed - real stdout/stderr, a
    real `repr()` of a returned value, or `"EXCEPTION: ..."` if the checked
    code raised. `verdict` is `True` (matched expectation), `False` (did
    not), or `None` only for a verification that recorded output but made no
    pass/fail judgement (rare - prefer a passing/failing verdict whenever
    there is anything to compare against).
    """

    command: str
    actual_output: str
    verdict: bool | None
    duration_s: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class Claim:
    """One claim under evidence, plus every verification attached to it."""

    text: str
    verifications: list[Verification] = field(default_factory=list)

    @property
    def status(self) -> str:
        """`"unverified"` (no verifications), `"failed"` (>=1 verification
        with verdict False), or `"verified"` (>=1 verification, all True)."""
        if not self.verifications:
            return "unverified"
        if any(v.verdict is False for v in self.verifications):
            return "failed"
        if all(v.verdict is True for v in self.verifications):
            return "verified"
        return "inconclusive"  # every verdict is None - recorded but unjudged


class EvidenceLedger:
    """Trivially usable from a probe script:

    ledger = EvidenceLedger()
    ledger.claim("backend reports available")
    ledger.verify_callable(
        "backend reports available",
        "backend.probe().available",
        lambda: backend.probe().available,
    )
    ...
    print(ledger.summary())
    assert ledger.all_verified()
    """

    def __init__(self) -> None:
        self._claims: dict[str, Claim] = {}
        self._order: list[str] = []

    # -- claim registration ---------------------------------------------

    def claim(self, text: str) -> str:
        """Register a claim (idempotent - registering the same text twice
        returns the same claim, it does not reset its verifications).
        Returns the claim id (the text itself - claims are keyed by their
        own sentence, so no separate id-tracking is needed by callers).
        """
        if text not in self._claims:
            self._claims[text] = Claim(text=text)
            self._order.append(text)
        return text

    def _get(self, claim_id: str) -> Claim:
        claim = self._claims.get(claim_id)
        if claim is None:
            raise EvidenceLedgerError(
                f"no such claim {claim_id!r} - call .claim(...) before verifying it"
            )
        return claim

    # -- verification: run a real subprocess -----------------------------

    def run_command(
        self,
        claim_id: str,
        cmd: Sequence[str] | str,
        *,
        expect_returncode: int | None = 0,
        expect_stdout_contains: str | None = None,
        timeout_s: float = 30.0,
        shell: bool = False,
    ) -> Verification:
        """Actually execute `cmd` (never trust a caller's self-report of what
        a command printed) and record the real returncode/stdout/stderr.

        A `subprocess.TimeoutExpired` or any other exception raised while
        running the command is caught here and recorded as a FAILED
        verification with the exception as `actual_output` - it is never
        allowed to propagate into a silent pass, and never allowed to crash
        the caller either (the caller's job is to read the ledger, not to
        wrap every check in its own try/except).
        """
        claim = self._get(claim_id)
        description = (
            cmd if isinstance(cmd, str) else " ".join(shlex.quote(c) for c in cmd)
        )
        start = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                shell=shell,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 - any failure is a FAILED verification, never silent
            duration_s = time.monotonic() - start
            verification = Verification(
                command=description,
                actual_output=f"EXCEPTION running command: {type(exc).__name__}: {exc}",
                verdict=False,
                duration_s=duration_s,
            )
            claim.verifications.append(verification)
            return verification

        duration_s = time.monotonic() - start
        ok = True
        reasons: list[str] = []
        if expect_returncode is not None and proc.returncode != expect_returncode:
            ok = False
            reasons.append(
                f"returncode={proc.returncode} (expected {expect_returncode})"
            )
        if (
            expect_stdout_contains is not None
            and expect_stdout_contains not in proc.stdout
        ):
            ok = False
            reasons.append(f"stdout did not contain {expect_stdout_contains!r}")

        actual = (
            f"returncode={proc.returncode}\n"
            f"stdout={proc.stdout.strip()[:2000]!r}\n"
            f"stderr={proc.stderr.strip()[:2000]!r}"
        )
        if reasons:
            actual += f"\nMISMATCH: {'; '.join(reasons)}"
        verification = Verification(
            command=description, actual_output=actual, verdict=ok, duration_s=duration_s
        )
        claim.verifications.append(verification)
        return verification

    # -- verification: run a real callable -------------------------------

    def verify_callable(
        self,
        claim_id: str,
        description: str,
        fn: Callable[[], Any],
        *,
        expect: Callable[[Any], bool] | None = None,
    ) -> Verification:
        """Actually CALL `fn()` and record what really happened.

        This is the direct fix for "a harness that reported PASS on its own
        TypeError": any exception `fn()` raises is caught here and recorded
        as a FAILED verification with the exception's repr as the actual
        output - it is structurally impossible for a raised exception to
        become a passing verdict through this path, because nothing between
        `fn()` and the recorded verdict has a chance to swallow it first.

        `expect(result) -> bool` judges the returned value; if omitted, the
        verdict is `bool(result)` (truthy pass, falsy fail) - useful for
        `lambda: backend.probe().available` style checks.
        """
        claim = self._get(claim_id)
        start = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad: this IS the catch point
            duration_s = time.monotonic() - start
            verification = Verification(
                command=description,
                actual_output=f"EXCEPTION: {type(exc).__name__}: {exc}",
                verdict=False,
                duration_s=duration_s,
            )
            claim.verifications.append(verification)
            return verification

        duration_s = time.monotonic() - start
        try:
            ok = expect(result) if expect is not None else bool(result)
        except Exception as exc:  # noqa: BLE001 - a broken `expect` predicate is also a failure, not a crash
            verification = Verification(
                command=description,
                actual_output=(
                    f"result={result!r}; EXCEPTION evaluating expect(): "
                    f"{type(exc).__name__}: {exc}"
                ),
                verdict=False,
                duration_s=duration_s,
            )
            claim.verifications.append(verification)
            return verification

        verification = Verification(
            command=description,
            actual_output=repr(result),
            verdict=ok,
            duration_s=duration_s,
        )
        claim.verifications.append(verification)
        return verification

    # -- verification: record an already-known value ---------------------

    def verify_equals(
        self, claim_id: str, description: str, *, actual: Any, expected: Any
    ) -> Verification:
        """Record a direct comparison of two already-computed values (no
        subprocess, no callable - for when the "verification" is simply
        reading a value that was already the product of real code running,
        e.g. `GUARD_MEASURED["macos"]`). The comparison itself still happens
        here, in the ledger, not as an unchecked assertion in the caller.
        """
        claim = self._get(claim_id)
        ok = actual == expected
        verification = Verification(
            command=description,
            actual_output=f"actual={actual!r} expected={expected!r}",
            verdict=ok,
        )
        claim.verifications.append(verification)
        return verification

    # -- reading the ledger -----------------------------------------------

    def unverified_claims(self) -> list[Claim]:
        """Every claim registered but never backed by a single verification."""
        return [
            self._claims[t]
            for t in self._order
            if self._claims[t].status == "unverified"
        ]

    def failed_claims(self) -> list[Claim]:
        """Every claim with at least one verification whose verdict was False."""
        return [
            self._claims[t] for t in self._order if self._claims[t].status == "failed"
        ]

    def verified_claims(self) -> list[Claim]:
        """Every claim with >=1 verification and every verdict True."""
        return [
            self._claims[t] for t in self._order if self._claims[t].status == "verified"
        ]

    def all_verified(self) -> bool:
        """True only if every registered claim is `verified` - zero
        unverified, zero failed, zero inconclusive. No partial credit."""
        if not self._order:
            return False
        return all(self._claims[t].status == "verified" for t in self._order)

    def claims(self) -> list[Claim]:
        return [self._claims[t] for t in self._order]

    # -- reporting ----------------------------------------------------------

    def summary(self) -> str:
        """Human-readable report: every claim, its status, and the real
        evidence (command + actual output) behind it - or the explicit
        absence of any evidence."""
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append("EVIDENCE LEDGER")
        lines.append("=" * 78)
        for text in self._order:
            claim = self._claims[text]
            status = claim.status.upper()
            lines.append("")
            lines.append(f"[{status}] {claim.text}")
            if not claim.verifications:
                lines.append("    NO VERIFICATION ATTACHED - this claim is unchecked.")
            for v in claim.verifications:
                mark = {"True": "PASS", "False": "FAIL", "None": "N/A"}[str(v.verdict)]
                lines.append(f"    [{mark}] ran: {v.command}")
                for out_line in v.actual_output.splitlines() or [""]:
                    lines.append(f"          {out_line}")
        lines.append("")
        lines.append("-" * 78)
        n = len(self._order)
        n_verified = len(self.verified_claims())
        n_failed = len(self.failed_claims())
        n_unverified = len(self.unverified_claims())
        lines.append(
            f"claims: {n}  verified: {n_verified}  failed: {n_failed}  unverified: {n_unverified}"
        )
        lines.append(f"VERDICT: {'PASS' if self.all_verified() else 'FAIL'}")
        lines.append("-" * 78)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claims": [
                {
                    "text": self._claims[t].text,
                    "status": self._claims[t].status,
                    "verifications": [
                        {
                            "command": v.command,
                            "actual_output": v.actual_output,
                            "verdict": v.verdict,
                            "duration_s": v.duration_s,
                            "timestamp": v.timestamp,
                        }
                        for v in self._claims[t].verifications
                    ],
                }
                for t in self._order
            ],
            "all_verified": self.all_verified(),
        }

    def save(self, path: str | Path) -> None:
        """Persist the ledger as JSON, so "I verified X" is a file on disk a
        reviewer can open, not just a sentence in a chat transcript."""
        import json

        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
