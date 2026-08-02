"""Reusable proof tools for `amplifier-bundle-computer-use`.

Four ship-gate-grade building blocks, extracted from patterns that were
hand-rolled repeatedly across a long investigation session (see each
module's own docstring for the specific incident it closes):

* `proof_injector`   - cross-platform synthetic-human event-stream injector
* `screen_differ`    - screenshot-diff quantifier (pixel + colour counts)
* `remote_probe`     - deploy/run/collect/teardown harness over SSH
* `evidence_ledger`  - claim -> verification -> actual output -> verdict

This package has no dependency on `amplifier_core` and no dependency on
`modules/tool-computer-use` - it is deliberately standalone so it can be
imported by ship-gate scripts (`scripts/*.py`), ad hoc probes, and its own
test suite without dragging in the bundle's runtime mount path.

Import convention (matching this repo's `scripts/*.py` and `tests/*.py`,
which have no installed package to rely on):

    import sys
    from pathlib import Path

    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT))
    from tools.evidence_ledger import EvidenceLedger
"""

from __future__ import annotations
