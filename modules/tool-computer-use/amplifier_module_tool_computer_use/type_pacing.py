"""Type-text pacing - the fix for a measured masking defect adjacent to
`docs/designs/coexistence.md` \u00a75.2/\u00a75.5.

The defect, measured
--------------------
A live test on a real MacBook typed a 202-character string via the shipped
`MacOSBackend.type_text()` and it completed in **0.07 seconds**. The
coexistence guard fires per character and behaved correctly - but the whole
operation finished faster than any human can react (~250ms reaction floor).

The arithmetic: 202 characters in ~70ms is an inter-character gap of
~0.35ms, while `presence.GUARD_MS["macos"]` is 10ms - a gap **28x narrower**
than the guard band. Since the detector's masked fraction is `GUARD /
cadence` (\u00a75.2), a gap that much narrower than the guard means masking is
effectively total: the presence detector is structurally blind for the
*entire* duration of a full-speed burst, and can only ever catch a human who
was already detected *before* the operation started. This is the same class
of defect O5 fixed for the 250ms-guard/60ms-cadence case in revision 1 of
the design - reappearing at a different (much narrower) cadence that nobody
had paced against.

For contrast, the ship gate (`scripts/verify_coexistence.py`) runs at 60ms
cadence against a 5ms Linux guard - masked fraction 8.3%, measured 9.00%.
*That* relationship is what makes the guard meaningful; full-speed
`type_text` has no such relationship at all.

The fix
-------
Pace `type_text` so the inter-character gap exceeds the guard band - but
ONLY when a coexistence guard is actually active for this session. An
unattended machine with no presence source (no guard constructed at all -
Windows today, or coexistence explicitly disabled) has nothing to pace
against and must not be slowed down: pacing exists solely to keep the
detector meaningful, never as a general "type slower" policy. See
`ComputerTool._run`'s `type` action for where this is applied - one shared
call site all backends route through, not a per-backend patch.

Why 25ms, not more
------------------
Single-event masked fraction at 25ms is `10/25 = 40%` on macOS and
`5/25 = 20%` on Linux (`presence.GUARD_MS`) - WORSE per-event than the ship
gate's 8.3%. This is a deliberate trade, not an oversight, and must be
stated honestly rather than presented as if 25ms were as safe as the ship
gate's 60ms: a real human interaction is never one isolated event (a
trackpad touch or keystroke produces a burst of several, unlike the single
event the ship gate models as its worst case), so multi-event detection
converges toward something close to the ship gate's per-two-event figure
(~0.7%) even though any single event within that burst is individually more
likely to be missed than the ship gate's own worst case. 25ms also keeps a
202-character string at roughly 5 seconds (202 * 25ms ~= 5.05s) instead of
the ~24s a more conservative pacing (e.g. 120ms) would cost - the practical
ceiling on how much latency one `type_text` call can absorb before it stops
being usable.

Do not round this up "for extra safety" the way revision 1 of the guard
band itself did (\u00a75.3, corrected by O5) - a wider pacing constant makes
EVERY individual character's masking fraction worse, in exchange for a
safety property that depends on humans producing multi-event bursts, not on
any single event landing outside the guard band.

This constant is derived from `presence.GUARD_MS` and is NOT independent of
it: if a platform's guard band is ever re-measured (e.g.
`GUARD_MS["windows-wsl2"]` once O4 lands there per `presence.GUARD_MEASURED`),
this pacing constant must be revisited against the new number, not left as
a stale assumption keyed to today's Linux/macOS bands.
"""

from __future__ import annotations

#: Milliseconds per character when a coexistence guard is active and no
#: explicit `type_pacing_ms` override is configured. See the module
#: docstring for the masking-fraction arithmetic this number is based on,
#: and why it is not "rounded up for safety".
AUTO_PACING_MS = 25.0


def resolve_type_pacing_ms(
    configured: int | float | None, *, guard_active: bool
) -> float:
    """Resolve the effective per-character `type_text` pacing delay, in
    milliseconds, for one operation.

    `configured` is the raw `type_pacing_ms` config value: `None` means
    "auto" (the default); any number is an explicit override.

    `guard_active` is whether a `CoexistenceGuard` is actually threaded
    through THIS specific `type_text` call (i.e. `self._coexistence_guard
    is not None and self._backend_type_text_supports_guard` at the call
    site in `ComputerTool._run`) - not merely whether one could in
    principle exist for this platform.

    Resolution:
      - No guard active: always `0.0` (full speed, unchanged from before
        this feature existed) - there is nothing to pace against, and no
        cheap per-character path exists on every backend (a remote or
        Windows `type_text` round-trips a whole string in one call;
        chunking it to pace would multiply subprocess/wire cost for no
        safety benefit, since no guard means no detector to protect).
      - Guard active, `configured is None` (auto, the default):
        `AUTO_PACING_MS`.
      - Guard active, `configured` is an explicit value: that value,
        verbatim, in EITHER direction - larger (more conservative) or
        smaller/zero (faster, at the caller's explicit risk). `0` is a
        legitimate choice; the caller is responsible for logging a
        WARNING once when it fires (see `ComputerTool._run`) - this
        function stays a pure resolution with no logging side effect, so
        it is trivially unit-testable without a logging fixture.
    """
    if not guard_active:
        return 0.0
    if configured is None:
        return AUTO_PACING_MS
    return float(configured)
