"""Unit tests for `tools.evidence_ledger.EvidenceLedger` - no remote host, no
subprocess beyond genuinely trivial local binaries (`true`/`false`), no
desktop. Exercises the exact three failure modes the ledger exists to catch:
a claim with no verification, a verification that legitimately fails, and a
checked callable that raises instead of returning.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.evidence_ledger import EvidenceLedger, EvidenceLedgerError


def test_claim_is_idempotent_and_starts_unverified():
    ledger = EvidenceLedger()
    ledger.claim("the sky is blue")
    ledger.claim("the sky is blue")  # re-registering must not reset it
    assert len(ledger.claims()) == 1
    assert ledger.claims()[0].status == "unverified"
    assert ledger.unverified_claims() == ledger.claims()
    assert not ledger.all_verified()


def test_run_command_records_a_real_passing_subprocess():
    ledger = EvidenceLedger()
    ledger.claim("true exits 0")
    v = ledger.run_command("true exits 0", ["true"])
    assert v.verdict is True
    assert "returncode=0" in v.actual_output
    assert ledger.verified_claims()[0].text == "true exits 0"
    assert ledger.all_verified()


def test_run_command_catches_a_deliberately_false_claim():
    """The core demonstration: claim something false, verify with a REAL
    subprocess (`false` always exits 1), and the ledger must flag it -
    never silently pass."""
    ledger = EvidenceLedger()
    ledger.claim("the script exits 0")
    v = ledger.run_command("the script exits 0", ["false"])
    assert v.verdict is False
    assert "returncode=1" in v.actual_output
    assert ledger.failed_claims()[0].text == "the script exits 0"
    assert not ledger.all_verified()


def test_run_command_stdout_expectation_mismatch_fails():
    ledger = EvidenceLedger()
    ledger.claim("echoes hello")
    v = ledger.run_command(
        "echoes hello", ["echo", "goodbye"], expect_stdout_contains="hello"
    )
    assert v.verdict is False
    assert "MISMATCH" in v.actual_output


def test_verify_callable_catches_a_raised_exception_not_a_silent_pass():
    """Direct fix for incident 3: a harness that reported PASS on its own
    TypeError. Here, the checked callable raises - the ledger must record
    that as FAILED, with the exception visible, never as a pass."""
    ledger = EvidenceLedger()
    ledger.claim("addition works")

    def _boom():
        return 1 + "not a number"  # type: ignore[operator]

    v = ledger.verify_callable("addition works", "1 + 'not a number'", _boom)
    assert v.verdict is False
    assert "EXCEPTION" in v.actual_output
    assert "TypeError" in v.actual_output
    assert ledger.failed_claims()[0].text == "addition works"


def test_verify_callable_records_real_return_value_and_custom_expectation():
    ledger = EvidenceLedger()
    ledger.claim("backend reports available")
    v = ledger.verify_callable(
        "backend reports available", "probe().available", lambda: True
    )
    assert v.verdict is True
    assert v.actual_output == "True"

    ledger.claim("counts exactly three items")
    v2 = ledger.verify_callable(
        "counts exactly three items",
        "len([1, 2, 3])",
        lambda: len([1, 2, 3]),
        expect=lambda n: n == 3,
    )
    assert v2.verdict is True


def test_verify_equals_catches_a_measured_flag_that_disagrees_with_reality():
    """Mirrors the actual incident: a GUARD_MEASURED flag reported True
    that was still False. `verify_equals` compares the two directly."""
    ledger = EvidenceLedger()
    ledger.claim("GUARD_MEASURED['macos'] is True")
    reported_value = False  # what the code actually held
    v = ledger.verify_equals(
        "GUARD_MEASURED['macos'] is True",
        "GUARD_MEASURED['macos']",
        actual=reported_value,
        expected=True,
    )
    assert v.verdict is False
    assert not ledger.all_verified()


def test_unverified_claim_is_flagged_distinctly_from_a_failed_one():
    ledger = EvidenceLedger()
    ledger.claim("checked and passing")
    ledger.run_command("checked and passing", ["true"])
    ledger.claim("never actually checked")

    unverified = ledger.unverified_claims()
    assert [c.text for c in unverified] == ["never actually checked"]
    assert not ledger.all_verified()
    summary = ledger.summary()
    assert "NO VERIFICATION ATTACHED" in summary
    assert "[UNVERIFIED] never actually checked" in summary
    assert "[VERIFIED] checked and passing" in summary


def test_all_verified_true_only_when_every_claim_is_verified():
    ledger = EvidenceLedger()
    ledger.claim("a")
    ledger.run_command("a", ["true"])
    assert ledger.all_verified()

    ledger.claim("b")
    ledger.run_command("b", ["false"])
    assert not ledger.all_verified()


def test_all_verified_false_with_zero_claims():
    ledger = EvidenceLedger()
    assert ledger.all_verified() is False


def test_verifying_unregistered_claim_raises():
    ledger = EvidenceLedger()
    try:
        ledger.run_command("never claimed", ["true"])
    except EvidenceLedgerError:
        pass
    else:
        raise AssertionError("expected EvidenceLedgerError")


def test_run_command_exception_path_is_a_failed_verification_not_a_crash():
    ledger = EvidenceLedger()
    ledger.claim("a nonexistent binary can be run")
    v = ledger.run_command(
        "a nonexistent binary can be run", ["/no/such/binary/anywhere"]
    )
    assert v.verdict is False
    assert "EXCEPTION" in v.actual_output


def test_to_dict_and_save_round_trip(tmp_path):
    ledger = EvidenceLedger()
    ledger.claim("a")
    ledger.run_command("a", ["true"])
    data = ledger.to_dict()
    assert data["all_verified"] is True
    assert data["claims"][0]["status"] == "verified"

    out = tmp_path / "ledger.json"
    ledger.save(out)
    assert out.exists()
    import json

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved == data
