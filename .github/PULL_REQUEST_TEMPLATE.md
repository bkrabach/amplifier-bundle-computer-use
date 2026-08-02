## What does this change?

<!-- Summarize the change and why it's needed. Link any related issue(s). -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactor / internal change (no behavior change)
- [ ] Other:

## Testing

- [ ] `.venv/bin/python -m pytest tests/ -q` passes locally
- [ ] `uvx ruff format --check .` and `uvx ruff check .` pass locally
- [ ] If this touches the coexistence guard, presence detector, or an input backend:
      `scripts/verify_coexistence.py` was run locally against a real `Xvfb`/display and
      the result is pasted below.

## Evidence checklist

This project's evidence standard (see `CONTRIBUTING.md`) requires that platform-behavior
claims be backed by a real run, not inference. Check the boxes that apply — leave unchecked
boxes unchecked rather than assuming them true.

- [ ] **This PR makes a claim about platform behavior** (timing, permission dialogs, input
      delivery, screenshot capture, etc.). If checked, paste the real run's output below —
      not a description of what you expect it to do.
- [ ] **This PR flips a `GUARD_MEASURED` flag to `True`** (in
      `modules/tool-computer-use/amplifier_module_tool_computer_use/presence.py`). If
      checked, paste the hardware run that justifies the flip — the platform it was run
      on, the command, and the raw result.
- [ ] Neither of the above applies to this PR.

<!-- Paste real run output / evidence here, if either box above is checked. -->

```
paste here
```

## Anything reviewers should focus on

<!-- Tricky parts, deliberate trade-offs, things you're unsure about. -->
