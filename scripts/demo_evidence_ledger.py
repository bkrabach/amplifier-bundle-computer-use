#!/usr/bin/env python3
"""Demonstration: `tools.evidence_ledger.EvidenceLedger` catching a
deliberately false claim, an unverified claim, and a checked callable that
raises - the three real incidents cited in the task, reproduced here as a
live artifact rather than described in prose.

    .venv/bin/python scripts/demo_evidence_ledger.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.evidence_ledger import EvidenceLedger


def main() -> int:
    print(
        "=== evidence_ledger demonstration: catching false and missing evidence ===\n"
    )

    ledger = EvidenceLedger()

    # 1. A TRUE claim, backed by a real, passing subprocess.
    ledger.claim("the probe script actually exists and is executable")
    ledger.run_command(
        "the probe script actually exists and is executable",
        ["test", "-x", sys.executable],
    )

    # 2. A DELIBERATELY FALSE claim - "the script exited cleanly" - verified
    #    against a real subprocess (`false`) that always exits 1. This is
    #    the direct reproduction of "a harness that reported PASS" - except
    #    here the ledger, not a hand-written report, is what looks at it.
    ledger.claim("the probe script exited cleanly (rc=0)")
    ledger.run_command("the probe script exited cleanly (rc=0)", ["false"])

    # 3. Mirrors the GUARD_MEASURED incident: a flag reported True that was
    #    actually still False.
    reported_guard_measured_macos = False  # what the code actually held
    ledger.claim("GUARD_MEASURED['macos'] is True")
    ledger.verify_equals(
        "GUARD_MEASURED['macos'] is True",
        "GUARD_MEASURED['macos']",
        actual=reported_guard_measured_macos,
        expected=True,
    )

    # 4. Mirrors "a harness that reported PASS on its own TypeError" - the
    #    checked callable raises, and the ledger must not let that become a
    #    silent pass.
    ledger.claim("the harness's own computation succeeds")

    def _the_harnesss_own_broken_computation():
        return 1 + "oops"  # type: ignore[operator]  # deliberately raises TypeError

    ledger.verify_callable(
        "the harness's own computation succeeds",
        "1 + 'oops'",
        _the_harnesss_own_broken_computation,
    )

    # 5. A claim that NOBODY ever verified at all - the ledger must flag
    #    this distinctly from a failed one, not silently treat it as fine.
    ledger.claim("the overlay rendered on every platform")

    print(ledger.summary())

    caught_false_claim = any(
        c.text == "the probe script exited cleanly (rc=0)"
        for c in ledger.failed_claims()
    )
    caught_measured_mismatch = any(
        c.text == "GUARD_MEASURED['macos'] is True" for c in ledger.failed_claims()
    )
    caught_exception = any(
        c.text == "the harness's own computation succeeds"
        for c in ledger.failed_claims()
    )
    caught_unverified = any(
        c.text == "the overlay rendered on every platform"
        for c in ledger.unverified_claims()
    )
    all_verified_correctly_false = not ledger.all_verified()

    ok = (
        caught_false_claim
        and caught_measured_mismatch
        and caught_exception
        and caught_unverified
        and all_verified_correctly_false
    )
    print(f"\ncaught the false 'exited cleanly' claim:       {caught_false_claim}")
    print(f"caught the GUARD_MEASURED mismatch:             {caught_measured_mismatch}")
    print(f"caught the TypeError instead of a silent pass:  {caught_exception}")
    print(f"flagged the never-verified claim:               {caught_unverified}")
    print(
        f"all_verified() correctly reports False:         {all_verified_correctly_false}"
    )
    print(f"\nVERDICT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
