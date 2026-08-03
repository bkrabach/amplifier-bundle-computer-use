"""The provider wire dialects this tool speaks.

Two vendors ship a native `computer` tool, and they disagree about almost
everything on the wire. Both were driven against the same real desktop, through
this tool, before this module existed - the differences below are transcribed
from that traffic (`tests/fixtures/captures/`), not designed ahead of it:

===================  ====================================  =========================
                     Anthropic                             OpenAI
===================  ====================================  =========================
declaration          ``computer_20251124`` + REQUIRED       bare ``{"type":
                     ``display_width_px`` /                 "computer"}`` - every
                     ``display_height_px``                  config field 400s
action shape         ``{"action": "scroll", "coordinate":   ``{"type": "move",
                     [x, y], "scroll_direction": "down",    "keys": null, "x": 426,
                     "scroll_amount": 3}``                  "y": 87}``
cardinality          one action per call                    N actions per
                                                            ``call_id``, ONE result
modifiers            ride in ``text`` - the same field      dedicated ``keys``
                     ``type`` uses, overloaded              field, explicitly null
result               text-only is fine                      MUST carry a screenshot
model -> tool type   ``claude-opus-5`` ->                    ``gpt-5.5`` ->
                     ``computer_20251124``                  ``computer``
===================  ====================================  =========================

Why a table of records and not a framework
-------------------------------------------
N is two. A `Protocol` with two implementors, a registry with registration
hooks, or a plugin-discovery mechanism would all be scaffolding erected around
a dict lookup. What is actually needed is: *given a tool type, how do I
declare it; given a tool-call payload, how do I read it.* That is a dispatch
table. `Dialect` is a frozen record holding the four facts that genuinely
differ, and `DIALECTS` is the table. Adding a vendor is one more record and one
more row - no new machinery, and nothing to edit in `__init__.py` or
`tool_versions.py`.

What deliberately did NOT become per-dialect
---------------------------------------------
Everything downstream of `read_actions()`. Both dialects normalize into this
tool's own `(action, params)` vocabulary and then run through the *same*
`ComputerTool._run()`, the same `ACTIONS` membership check, the same
`read_only`/`MUTATING` gate, and the same halt/error handling. Those were never
provider-specific; only the wire form on either side of them is. A second,
parallel action dispatcher per vendor is exactly what this table exists to
prevent.

Where the *rest* of the provider story still lives, and why it is not here
--------------------------------------------------------------------------
`hook-computer-use` probes whether the mounted provider will actually carry a
native tool type to the wire. That knowledge is NOT in this module and cannot
be: the probes drive amplifier *provider module* internals
(`_derive_native_tool_betas`, `_convert_tools_from_request`) - plumbing names,
not vendor wire format - and `hook-computer-use` declares no dependency on this
package (see its `pyproject.toml`: ``dependencies = []``), so it cannot import
this table. Those are two different axes that happen to correlate 1:1 while
there are exactly two vendors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Anthropic's `computer` action names carrying the tool's own vocabulary.
#: OpenAI's `computer_call` action `type` -> this tool's `action` name, for the
#: click shapes whose target depends on `button` (every other type maps 1:1
#: inside `_read_openai_actions`).
_OPENAI_BUTTON_TO_ACTION = {
    "left": "left_click",
    "right": "right_click",
    "middle": "middle_click",
    "wheel": "middle_click",
    "back": "left_click",
    "forward": "left_click",
}

#: One normalized request: this tool's own action name plus the params `_run`
#: expects. Deliberately a plain tuple in a plain iterable, NOT a named
#: `ActionBatch` type - see `read_call`.
Call = tuple[str, dict[str, Any]]


def _normalize_openai_action(raw: Mapping[str, Any]) -> Call:
    """Translate ONE OpenAI `computer_call` action into this tool's `(action,
    params)` vocabulary.

    Raises `ValueError` for a `type` this tool has no equivalent action for, or
    a well-known type missing a field it requires - surfaced to the model as an
    ordinary tool error, never silently dropped or guessed at.
    """
    action_type = str(raw.get("type") or "").strip()

    def xy() -> list[float]:
        x, y = raw.get("x"), raw.get("y")
        if x is None or y is None:
            raise ValueError(f"openai action {action_type!r} is missing x/y")
        return [float(x), float(y)]

    if action_type == "screenshot":
        return "screenshot", {}
    if action_type == "wait":
        return "wait", {}
    if action_type == "move":
        return "mouse_move", {"coordinate": xy()}
    if action_type == "click":
        button = str(raw.get("button") or "left")
        return _OPENAI_BUTTON_TO_ACTION.get(button, "left_click"), {"coordinate": xy()}
    if action_type == "double_click":
        return "double_click", {"coordinate": xy()}
    if action_type == "drag":
        path = raw.get("path") or []
        if len(path) < 2:
            raise ValueError("openai 'drag' action requires a path of >= 2 points")
        start, end = path[0], path[-1]
        return "left_click_drag", {
            "start_coordinate": [float(start["x"]), float(start["y"])],
            "coordinate": [float(end["x"]), float(end["y"])],
        }
    if action_type == "scroll":
        dx = int(raw.get("scroll_x") or 0)
        dy = int(raw.get("scroll_y") or 0)
        if dy != 0:
            direction, amount = ("down" if dy > 0 else "up"), abs(dy)
        elif dx != 0:
            direction, amount = ("right" if dx > 0 else "left"), abs(dx)
        else:
            direction, amount = "down", 0
        params: dict[str, Any] = {
            "scroll_direction": direction,
            "scroll_amount": amount,
        }
        if raw.get("x") is not None and raw.get("y") is not None:
            params["coordinate"] = xy()
        return "scroll", params
    if action_type == "keypress":
        # OpenAI's dedicated `keys` field; Anthropic overloads `text` for the
        # same job, which is why both land on this tool's `text` param here.
        keys = raw.get("keys") or []
        return "key", {"text": "+".join(str(k) for k in keys)}
    if action_type == "type":
        return "type", {"text": str(raw.get("text") or "")}
    raise ValueError(f"unsupported OpenAI computer-use action type {action_type!r}")


@dataclass(frozen=True)
class Dialect:
    """One vendor's native computer-tool wire form.

    Four fields carry the four things that genuinely differ. Everything else
    about executing an action is shared and lives in `ComputerTool`.
    """

    #: Human-readable, for log lines only. Never used to select anything -
    #: selection is always by wire `type` string or by payload shape.
    name: str

    #: Native tool `type` strings this dialect owns on the wire.
    tool_types: tuple[str, ...]

    #: Build the native tool declaration. Anthropic REQUIRES the display size;
    #: OpenAI rejects it (and every other field) with a 400.
    declare: Callable[..., dict[str, Any]]

    #: Read a tool-call payload into this tool's own `(action, params)`
    #: vocabulary, or return `None` if the payload is not written in this
    #: dialect. May raise `ValueError` while iterating - see `read_call`.
    read_actions: Callable[[Mapping[str, Any]], Iterable[Call] | None]

    #: OpenAI's `computer_call_output` is invalid without an image, so a batch
    #: that produced none gets one more screenshot appended. Anthropic is happy
    #: with a text-only result.
    result_must_carry_screenshot: bool

    #: Verified model (or undated generation prefix) -> required tool type.
    #: Evidence only - extend from a real 200/400 pair, never from inference
    #: about naming conventions. See `tool_versions` for how it is applied.
    models: Mapping[str, str] = field(default_factory=dict)

    #: Wire type -> the beta header that opts into it. Empty for vendors with
    #: no such concept (OpenAI).
    beta_headers: Mapping[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Anthropic: dated `computer_YYYYMMDD` types, one action per call
# ---------------------------------------------------------------------------


def _declare_anthropic(
    tool_type: str, *, width: int, height: int, enable_zoom: bool
) -> dict[str, Any]:
    """Anthropic's server-side tool definition, sized to the current display.

    `display_width_px`/`display_height_px` are REQUIRED and must never drift
    out of sync with what `computer.screenshot` actually captures - that is why
    the caller passes live values rather than anything cached at construction
    (see `ComputerTool.native_tool_spec`).
    """
    spec: dict[str, Any] = {
        "type": tool_type,
        "name": "computer",
        "display_width_px": width,
        "display_height_px": height,
    }
    # Dated types sort lexically, so this is a real "at least this generation"
    # test within Anthropic's own scheme. It used to sit in `native_tool_spec`
    # where it doubled as an accidental provider branch: `"computer"` (OpenAI)
    # sorts BELOW `"computer_20251124"`, so the comparison silently did
    # double duty as "and not OpenAI". Here it means only what it says.
    if enable_zoom and tool_type >= "computer_20251124":
        spec["enable_zoom"] = True
    return spec


def _read_anthropic_action(payload: Mapping[str, Any]) -> Iterable[Call]:
    """Anthropic sends exactly one action per tool call, with the action name
    under `action` and its parameters as siblings.

    The catch-all: never returns `None`, so it must be last in `DIALECTS`. An
    unrecognised or missing `action` is deliberately passed through as-is -
    `ComputerTool.execute` owns the "unknown action" error message, and both
    dialects share it.
    """
    return [(str(payload.get("action") or "").strip(), dict(payload))]


ANTHROPIC = Dialect(
    name="anthropic",
    tool_types=("computer_20251124", "computer_20250124", "computer_20241022"),
    declare=_declare_anthropic,
    read_actions=_read_anthropic_action,
    result_must_carry_screenshot=False,
    models={
        # Keyed on the UNDATED generation prefix, not the dated id the evidence
        # was captured against (`claude-sonnet-4-5-20250929`): `required_for_model`
        # matches `model.startswith(known)`, so a dated key can only ever match
        # the exact dated id - the plain alias `claude-sonnet-4-5`, which is what
        # a provider commonly reports, fell through to the fallback version and
        # 400'd every request. The undated prefix covers both forms.
        "claude-sonnet-4-5": "computer_20250124",
        "claude-sonnet-5": "computer_20251124",
        "claude-opus-5": "computer_20251124",
    },
    beta_headers={
        "computer_20251124": "computer-use-2025-11-24",
        "computer_20250124": "computer-use-2025-01-24",
        "computer_20241022": "computer-use-2024-10-22",
    },
)


# ---------------------------------------------------------------------------
# OpenAI: bare `computer` type, batched actions per call
# ---------------------------------------------------------------------------


def _declare_openai(
    tool_type: str, *, width: int, height: int, enable_zoom: bool
) -> dict[str, Any]:
    """OpenAI's Responses API `computer` tool takes NOTHING but its type.

    Live traffic: `{"type": "computer"}` -> 200; `display_width_px`,
    `display_height_px`, `display_width`, `environment` (any of them, alone)
    -> 400 "Unknown parameter". `width`/`height`/`enable_zoom` are accepted and
    discarded here on purpose: the caller does not need to know which dialect
    wants what, and discarding by construction is what keeps a future field
    from leaking onto a wire that rejects it.
    """
    return {"type": tool_type}


def _normalize_openai_batch(raw_actions: list[Any]) -> Iterator[Call]:
    """Lazily normalize a batch, one entry at a time.

    Lazy on purpose: `ComputerTool.execute` runs each action as it is pulled,
    so a bad entry halfway through a batch fails AFTER the good actions before
    it have run - which is what the pre-seam per-item loop did. Normalizing the
    whole batch up front would silently make batches atomic, a real behaviour
    change dressed up as a refactor.
    """
    for entry in raw_actions:
        if not isinstance(entry, dict):
            raise ValueError(f"unsupported action entry {entry!r}")
        yield _normalize_openai_action(entry)


def _read_openai_actions(payload: Mapping[str, Any]) -> Iterable[Call] | None:
    """OpenAI batches one or more actions under a single `computer_call`'s
    `actions` list. `amplifier-module-provider-openai` carries that batch
    through as `arguments={"actions": [...]}` (see its
    `_extract_computer_actions`), so the presence of that list IS the wire
    signature - detection by actual shape, never by asking which provider is
    mounted.
    """
    raw = payload.get("actions")
    if not isinstance(raw, list):
        return None
    return _normalize_openai_batch(raw)


OPENAI = Dialect(
    name="openai",
    tool_types=("computer",),
    declare=_declare_openai,
    read_actions=_read_openai_actions,
    # A `computer_call_output` with no image is not a valid response to a
    # `computer_call` - OpenAI's own `_extract_computer_screenshot_data_url`
    # raises without one.
    result_must_carry_screenshot=True,
    models={
        # Verified live end-to-end through this bundle against gpt-5.5
        # (2026-08-03): a real `computer_call` batch against a real remote
        # desktop, returning a `computer_call_output` the model then read and
        # reasoned over.
        "gpt-5.5": "computer",
    },
    beta_headers={},  # OpenAI has no beta-header opt-in for this tool.
)


#: The table. Order is NOT load-bearing for dispatch - `read_call` tries the
#: catch-all explicitly last rather than relying on where it sits here, so this
#: order is free to be whatever reads best. It IS observable in one place:
#: `tool_versions.KNOWN_MODEL_TOOL_VERSIONS` is assembled in this order and
#: `required_for_model` scans prefixes in insertion order, so keep the order
#: stable unless you have checked that scan.
DIALECTS: tuple[Dialect, ...] = (ANTHROPIC, OPENAI)

#: Used when a tool type is not in any dialect's `tool_types` - e.g. a brand
#: new, explicitly configured Anthropic type this build predates. Anthropic's
#: is the versioned, forward-dated family, so it is the safe assumption for an
#: unrecognised `computer_*` string; guessing OpenAI's bare form for one would
#: strip the display size Anthropic requires.
DEFAULT_DIALECT = ANTHROPIC


def dialect_for_tool_type(tool_type: str) -> Dialect:
    """Which dialect owns this native tool `type` on the wire."""
    for dialect in DIALECTS:
        if tool_type in dialect.tool_types:
            return dialect
    return DEFAULT_DIALECT


def read_call(payload: Mapping[str, Any]) -> tuple[Dialect, Iterable[Call]]:
    """Which dialect this tool-call payload is written in, and the actions it
    asks for - in order.

    Keyed on payload SHAPE, not on the tool type currently declared: a session
    can have its `tool_version` corrected mid-flight (see
    `tool_versions.resolve_tool_version`), and the payload already in hand is
    the only trustworthy statement of what was actually sent.

    The returned iterable may be lazy and may raise `ValueError` mid-iteration
    (see `_normalize_openai_batch`). Callers must iterate inside their error
    handling, not before it.

    On `ActionBatch`: there isn't one, deliberately. Both dialects return an
    iterable of `(action, params)`; Anthropic's simply has length one. The only
    thing a batch genuinely needs beyond "a list, sometimes of length one" is
    `result_must_carry_screenshot`, which is a property of the *dialect*, not
    of any particular batch. A named batch type would carry one bool and a
    list, and every caller would immediately unpack it.
    """
    for dialect in DIALECTS:
        if dialect is DEFAULT_DIALECT:
            # Tried explicitly last, below - the catch-all claims everything,
            # so where it happens to sit in `DIALECTS` must not decide
            # dispatch. `DIALECTS` order is then free to serve the other thing
            # it feeds (`tool_versions.KNOWN_MODEL_TOOL_VERSIONS`) without
            # silently changing which dialect reads a payload.
            continue
        actions = dialect.read_actions(payload)
        if actions is not None:
            return dialect, actions
    actions = DEFAULT_DIALECT.read_actions(payload)
    if actions is None:  # pragma: no cover - the catch-all never declines
        raise AssertionError(
            f"{DEFAULT_DIALECT.name} is DEFAULT_DIALECT but declined a payload; "
            "the default dialect must claim everything no other dialect does"
        )
    return DEFAULT_DIALECT, actions


def model_tool_types() -> dict[str, str]:
    """Every verified model -> tool type pairing, across all dialects."""
    return {model: t for d in DIALECTS for model, t in d.models.items()}


def beta_headers() -> dict[str, str]:
    """Every native tool type -> beta header, across all dialects."""
    return {t: header for d in DIALECTS for t, header in d.beta_headers.items()}
