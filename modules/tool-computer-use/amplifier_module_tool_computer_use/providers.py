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

import json
import math
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .geometry import ImageSpace

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
    #:
    #: The second argument is the COORDINATE SPACE the payload's numbers are
    #: relative to: the size of the screenshot the model was actually shown
    #: (`None` when the tool has not resolved its display geometry yet). It is
    #: the one fact about the world outside the payload that reading a payload
    #: can legitimately need, and it is passed rather than closed over because
    #: it is per-session and changes mid-session (`select_monitor`).
    #:
    #: Both incumbent dialects ignore it - they emit absolute pixels in the
    #: screenshot's own space, so the payload is already self-describing. A
    #: dialect emitting normalized or relative coordinates cannot be read
    #: without it, and one that needs it and is handed `None` must say so
    #: loudly (`ValueError`), never guess a size.
    read_actions: Callable[
        [Mapping[str, Any], ImageSpace | None], Iterable[Call] | None
    ]

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


def _read_anthropic_action(
    payload: Mapping[str, Any], image_space: ImageSpace | None
) -> Iterable[Call]:
    """Anthropic sends exactly one action per tool call, with the action name
    under `action` and its parameters as siblings.

    `image_space` is accepted and NEVER READ: Anthropic emits absolute pixels
    in the screenshot's own space, so the payload is already self-describing
    and this dialect's translation is complete without knowing the image size.
    Proven mechanically - `tests/test_provider_dialects.py` hands both
    incumbent readers an object that raises on any attribute access.

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


def _read_openai_actions(
    payload: Mapping[str, Any], image_space: ImageSpace | None
) -> Iterable[Call] | None:
    """OpenAI batches one or more actions under a single `computer_call`'s
    `actions` list. `amplifier-module-provider-openai` carries that batch
    through as `arguments={"actions": [...]}` (see its
    `_extract_computer_actions`), so the presence of that list IS the wire
    signature - detection by actual shape, never by asking which provider is
    mounted.

    `image_space` is accepted and NEVER READ - same reason as
    `_read_anthropic_action`: `x`/`y` are absolute pixels in the screenshot's
    own space, so nothing outside the payload is needed to read it.
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


# ---------------------------------------------------------------------------
# Gemini: no wire `type` at all, verb-per-function, normalized 0..999 grid
# ---------------------------------------------------------------------------

#: Gemini emits coordinates on a fixed 0..999 INTEGER GRID over the screenshot,
#: never in image pixels. Proven decisively by `gemini-unknown5.json`: probing
#: for the far-right and bottom-right of a 1280x720 image returned x=999 and
#: y=999 - impossible in pixel space on a 720px-tall image.
_GEMINI_COORD_MAX = 999

#: ...and the grid divisor is 1000, not 999. `gemini-coordproof.json` is the
#: arithmetic proof: raw (311, 84) on a 1280x720 image of a 3840x2160 monitor
#: put the REAL cursor at (1194, 181). 311/1000*3840 = 1194.2 -> 1194 (match);
#: 311/999*3840 = 1195.4 -> 1195 (miss). A 0..999 grid over N pixels is N/1000
#: per cell, so 999 lands on the last pixel rather than one past the edge.
_GEMINI_COORD_SPAN = _GEMINI_COORD_MAX + 1

#: The verbs actually observed on the wire. Google's published action list
#: names several of these differently (`click_at` for what arrived as
#: `move_to`; `magnitude_in_pixels` for what arrived as `magnitude`), so this
#: table follows the traffic, and only the traffic - the same evidence-only
#: discipline the `models` tables are held to.
_GEMINI_SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})


def _declare_gemini(
    tool_type: str, *, width: int, height: int, enable_zoom: bool
) -> dict[str, Any]:
    """Gemini's tool declaration has NO `type` key at all.

    Live traffic: `{"computer_use": {"environment": "ENVIRONMENT_DESKTOP"}}`
    -> accepted; `{"type": "computer_use", ...}` -> 400 `Unknown name "type"
    at 'tools[0]'`. The vendor key IS the discriminator.

    `width`/`height`/`enable_zoom` are accepted and discarded, exactly as in
    `_declare_openai` and for the same reason: no caller should have to know
    which dialect wants what.
    """
    return {"computer_use": {"environment": "ENVIRONMENT_DESKTOP"}}


def _normalize_gemini_action(
    name: str, args: Mapping[str, Any], image_space: ImageSpace | None
) -> Call:
    """Translate ONE Gemini `functionCall` into this tool's `(action, params)`
    vocabulary, in MODEL pixel space.

    Raises `ValueError` - surfaced to the model as an ordinary tool error - for
    every verb this tool has no honest equivalent for, and for a normalized
    coordinate that cannot be placed because the image it was measured against
    is unknown. Nothing is guessed at.
    """

    def xy() -> list[float]:
        x, y = args.get("x"), args.get("y")
        if x is None or y is None:
            raise ValueError(f"gemini action {name!r} is missing x/y")
        if image_space is None:
            raise ValueError(
                f"gemini action {name!r} carries normalized 0-{_GEMINI_COORD_MAX} "
                "coordinates, which cannot be placed without the size of the "
                "image the model was shown; display geometry is not resolved"
            )
        # Deliberately FLOAT. Rounding to whole model pixels here loses the
        # fraction `Display.to_screen` needs: for the proof capture, y=84 is
        # 60.48 model px, which scales to 181 screen px (the measured truth),
        # while a pre-rounded 60 scales to 180. One pixel - but it is the
        # difference between reproducing the capture and approximating it.
        return [
            float(x) * image_space.width / _GEMINI_COORD_SPAN,
            float(y) * image_space.height / _GEMINI_COORD_SPAN,
        ]

    if name in ("move_to", "hover_at"):
        return "mouse_move", {"coordinate": xy()}
    if name == "scroll_document":
        direction = str(args.get("direction") or "").strip().lower()
        if direction not in _GEMINI_SCROLL_DIRECTIONS:
            raise ValueError(
                f"gemini 'scroll_document' has unsupported direction {direction!r}"
            )
        # The direction absorbs cleanly. The magnitude does not, and this is a
        # real hole rather than a missing afternoon's work: `gemini-unknown6`
        # captured `magnitude: 999` against a 720px-tall image, i.e. a
        # NORMALIZED DISTANCE, while this tool's `scroll_amount` - and
        # `Backend.scroll`'s `amount` beneath it - is a WHEEL NOTCH COUNT.
        #
        # Note what this is NOT: it is not the coordinate problem again.
        # `image_space` is right here and it does not help. Converting a
        # distance-on-screen into notches needs pixels-per-notch, which is a
        # property of the TARGET (X11 issues N discrete button-4/5 events;
        # macOS posts N `kCGScrollEventUnitLine` lines; the pixels either
        # produces depend on the focused application's line height and the
        # user's scroll settings) - not a property of the vendor. There is no
        # value `Dialect` could hold that would be correct, because the fact
        # does not belong to the vendor at all. Inventing one here would be
        # exactly the inference the `models` tables forbid, so this fails loud.
        raise ValueError(
            "gemini 'scroll_document' magnitude is a normalized 0-999 distance "
            f"(got {args.get('magnitude')!r}), but this tool's scroll_amount is a "
            "wheel-notch count; pixels-per-notch is a property of the target "
            "backend, not of the vendor, and no capture pins one, so it is not "
            "guessed at. Capture one against a real target to close this."
        )
    if name == "open_web_browser":
        raise ValueError(
            "gemini 'open_web_browser' has no desktop equivalent in this tool's "
            "action vocabulary; Gemini's computer_use is browser-bound and asks "
            "for a browser session even under ENVIRONMENT_DESKTOP"
        )
    raise ValueError(
        f"unsupported Gemini computer-use action {name!r}; captured verbs are "
        "move_to, hover_at, scroll_document, open_web_browser"
    )


def _normalize_gemini_batch(
    name: str, args: Mapping[str, Any], image_space: ImageSpace | None
) -> Iterator[Call]:
    """One action per call, yielded LAZILY - and the laziness is load-bearing.

    `ComputerTool.execute` calls `read_call` OUTSIDE the `try:` that wraps
    iteration. So a `ValueError` raised while *reading* escapes `execute()`
    entirely instead of becoming a clean tool error, while the identical
    `ValueError` raised while *iterating* is caught and returned.

    Neither incumbent dialect exposes that: Anthropic's reader never raises (it
    passes the action name through untouched and lets `execute` own the
    "unknown action" message), and OpenAI's is a generator for an unrelated
    reason (preserving partial batch execution). Gemini rejects per VERB at
    read time and sends exactly one action per call, so the obvious eager
    `return [_normalize_gemini_action(...)]` is what a third implementor
    writes - and it silently moves every Gemini verb error outside the caller's
    error handling.

    `Dialect.read_actions`'s type distinguishes neither "iterable that raises
    on construction" from "iterable that raises on iteration"; nothing enforces
    the difference. STILL TRUE after the coordinate-space fix - see
    `tests/test_gemini_dialect.py`, which pins the laziness directly.
    """
    yield _normalize_gemini_action(name, args, image_space)


def _read_gemini_actions(
    payload: Mapping[str, Any], image_space: ImageSpace | None
) -> Iterable[Call] | None:
    """Gemini's response envelope is `functionCall`/`functionResponse`, not
    `tool_use` (Anthropic) or `computer_call` (OpenAI), and the VERB IS THE
    FUNCTION NAME - `{"name": "move_to", "args": {"x": 311, "y": 84}}` - not a
    field inside the payload the way `action`/`type` are for the other two.

    Detection is by shape (a string `name` beside a mapping `args`), matching
    how `_read_openai_actions` sniffs its batch - never by asking which
    provider is mounted.

    ASSUMPTION, stated because it is not verified: no `provider-gemini` module
    exists in this tree, so the exact dict an Amplifier provider would hand to
    `ComputerTool.execute` is not pinned by anything. This reads the
    `functionCall` body as captured in `gemini-response-1.json` /
    `gemini-coordproof.json`. If a real provider module flattens or renames
    those keys, this predicate is what changes.
    """
    name = payload.get("name")
    args = payload.get("args")
    if not isinstance(name, str) or not name or not isinstance(args, Mapping):
        return None
    # Detection eager, normalization lazy - see `_normalize_gemini_batch` for
    # why the second half of that is not a style choice.
    return _normalize_gemini_batch(name, args, image_space)


GEMINI = Dialect(
    name="gemini",
    # NOT a wire `type` - Gemini has none (see `_declare_gemini`). This is an
    # internal key so `dialect_for_tool_type` / `tool_versions` keep working,
    # and it is what `ComputerTool.native_tool_type` states to the hook; it
    # never reaches the wire. The field's own docstring says "type strings this
    # dialect owns on the wire", and for this dialect that is a lie of
    # convenience - recorded here rather than papered over.
    tool_types=("computer_use",),
    declare=_declare_gemini,
    read_actions=_read_gemini_actions,
    # UNVERIFIED. No capture pins whether a `functionResponse` must carry an
    # image; the one 400 we recorded ("requires the URL of the web page") was
    # about `open_web_browser`, not about screenshots. `False` is the
    # no-extra-work default, chosen because asserting `True` would be
    # inference. Flip it from a real 200/400 pair, never from the docs.
    result_must_carry_screenshot=False,
    models={
        # Undated generation prefix, per the Anthropic precedent above. Seen in
        # `gemini-response-1.json`'s `modelVersion`:
        # `gemini-2.5-computer-use-preview-10-2025`.
        "gemini-2.5-computer-use": "computer_use",
    },
    # Gemini has no beta-header opt-in for this tool, and saying so is now a
    # real answer rather than an absence indistinguishable from ignorance:
    # `tool_versions.beta_header_for` asks which dialect OWNS the type, so
    # `beta_header_for("computer_use")` returns `None`, not Anthropic's header.
    beta_headers={},
)


# ---------------------------------------------------------------------------
# Qwen (Alibaba DashScope): OUT-OF-SAMPLE EXTENSIBILITY PROBE - NOT SHIPPED
#
# DELIBERATELY ABSENT FROM `DIALECTS`. This record is a measuring instrument for
# the claim in `docs/multi-provider-design.md` Sec 18 ("adding provider
# N+1 is bounded work"), not a provider this bundle supports. Qwen was dropped
# in Phase 0 BEFORE this module existed (`tests/fixtures/captures/qwen-verdict.md`),
# so its shape informed nothing here - which is exactly what makes it a valid
# out-of-sample test and what Sec 18 says the Gemini measurement lacked.
#
# Keeping it out of `DIALECTS` keeps real dispatch, `model_tool_types()` and
# `beta_headers()` bit-identical. `tests/test_qwen_out_of_sample.py` monkeypatches
# it in, exactly as the existing synthetic-dialect tests already do.
#
# RESULT OF THE PROBE - which of `Dialect`'s seven fields can hold Qwen's facts:
#
#   name                          FITS
#   tool_types                    STRAINS - Qwen has no wire tool type in any
#                                 sense; unlike Gemini (whose vendor key
#                                 `computer_use` at least discriminates a real
#                                 `tools[]` entry) Qwen puts NOTHING in `tools[]`.
#                                 An entry here is a pure internal fiction.
#   declare                       CANNOT - see `_declare_qwen`.
#   read_actions                  FITS AS A FUNCTION, UNREACHABLE AS A PATH -
#                                 see `_read_qwen_actions`.
#   result_must_carry_screenshot  N/A - there is no `tool_result` block to carry
#                                 anything; the screenshot goes back as an
#                                 ordinary user-turn image.
#   models                        VACUOUS - maps model -> tool type, and there is
#                                 no tool type. Also cannot hold the per-model
#                                 `smart_resize` numbers the coordinate mapping
#                                 needs (`Mapping[str, str]`).
#   beta_headers                  FITS (empty - no such concept), vacuously.
# ---------------------------------------------------------------------------

#: Alibaba's GUI-Plus doc extracts the action with exactly this regex over
#: `message.content`. Transcribed from the doc, not designed here.
_QWEN_TOOL_CALL_RE = re.compile(r"<tool_call>(.*?)</tool_call>", re.DOTALL)

#: Qwen-VL's `smart_resize` rounds both image dimensions to a multiple of this
#: before the vision encoder sees them, and the model emits coordinates in THAT
#: space - not in the source image's space and not in the 0-1000 space its own
#: system prompt claims. UNVERIFIED against a live call (region-locked, no key);
#: transcribed from the published preprocessing helper. Present only to show
#: what the coordinate mapping WOULD need, never to claim it is right.
_QWEN_IMAGE_FACTOR = 28

#: Also UNVERIFIED, and worse: these are per-MODEL-DEPLOYMENT numbers, not
#: per-vendor ones. They are module constants here only because `Dialect.models`
#: is a `Mapping[str, str]` and has nowhere to put a number. See the probe
#: result table above.
_QWEN_MIN_PIXELS = 3136
_QWEN_MAX_PIXELS = 12_845_056


def _qwen_smart_resize(width: int, height: int) -> tuple[int, int]:
    """The resized `(width, height)` the vision encoder actually saw.

    UNVERIFIED - see `_QWEN_IMAGE_FACTOR`. Included so the coordinate half of
    the probe is a real function rather than a claim about one.
    """
    h_bar = max(
        _QWEN_IMAGE_FACTOR, round(height / _QWEN_IMAGE_FACTOR) * _QWEN_IMAGE_FACTOR
    )
    w_bar = max(
        _QWEN_IMAGE_FACTOR, round(width / _QWEN_IMAGE_FACTOR) * _QWEN_IMAGE_FACTOR
    )
    if h_bar * w_bar > _QWEN_MAX_PIXELS:
        beta = math.sqrt((height * width) / _QWEN_MAX_PIXELS)
        h_bar = math.floor(height / beta / _QWEN_IMAGE_FACTOR) * _QWEN_IMAGE_FACTOR
        w_bar = math.floor(width / beta / _QWEN_IMAGE_FACTOR) * _QWEN_IMAGE_FACTOR
    elif h_bar * w_bar < _QWEN_MIN_PIXELS:
        beta = math.sqrt(_QWEN_MIN_PIXELS / (height * width))
        h_bar = math.ceil(height * beta / _QWEN_IMAGE_FACTOR) * _QWEN_IMAGE_FACTOR
        w_bar = math.ceil(width * beta / _QWEN_IMAGE_FACTOR) * _QWEN_IMAGE_FACTOR
    return w_bar, h_bar


class QwenNotDeclarableError(NotImplementedError):
    """`Dialect.declare` has no codomain that can express Qwen's declaration.

    A distinct type so the probe test asserts THIS, and can never accidentally
    pass on some unrelated `NotImplementedError`.
    """


def _declare_qwen(
    tool_type: str, *, width: int, height: int, enable_zoom: bool
) -> dict[str, Any]:
    """THE FIRST BREAK. There is no honest return value.

    `declare`'s contract is "build the native tool declaration", and every
    consumer puts the returned dict in the request's `tools[]` array:
    `ComputerTool.native_tool_spec` -> the orchestrator's `_build_tool_spec` ->
    `ToolSpec` -> the provider's tool list. That destination is baked into the
    field, not into any one dialect.

    Qwen puts NOTHING in `tools[]`. DashScope documents `type` as "Currently,
    only function is supported" and the API reference contains zero
    computer-use tool types. Its declaration is a `computer_use` JSON schema
    PASTED INTO THE SYSTEM PROMPT AS TEXT (Alibaba's own GUI-Plus doc). The
    schema is a dict, so the SHAPE is expressible - the SLOT is not. There is
    no field on `Dialect` for "text that belongs in the system prompt" and no
    consumer that would carry one there.

    The three candidate return values and why each is forbidden:

      * `{}` - emits an empty, malformed entry in `tools[]`. Silent degradation.
      * the `computer_use` schema dict - puts a computer-use schema in the one
        slot DashScope documents as function-only. Either a 400 or a SILENT
        DROP; `qwen-verdict.md` explicitly records which of the two as
        unverified, so choosing this is choosing a coin-flip between "fails
        loudly" and "fails invisibly".
      * `None` - violates the declared `dict[str, Any]` return type, and no
        consumer has a "this dialect declares nothing" branch.

    So it raises. A raising `declare` is not a stub - it is the measurement:
    the seam can hold this vendor's NAME but not its DECLARATION.
    """
    raise QwenNotDeclarableError(
        "Qwen has no native tool declaration: its computer_use schema is pasted "
        "into the SYSTEM PROMPT as text, and `Dialect.declare` can only return a "
        "dict destined for the request's `tools[]` array. Expressing this needs a "
        "new Dialect field AND a new consumer that reaches the system prompt - "
        "neither exists (see providers.py, Qwen probe)."
    )


def _normalize_qwen_action(
    raw: Mapping[str, Any], image_space: ImageSpace | None
) -> Call:
    """Translate ONE regex-extracted Qwen `<tool_call>` body into this tool's
    `(action, params)` vocabulary, in MODEL pixel space.

    The action vocabulary itself absorbs cleanly - Qwen's pasted schema is
    modelled on Anthropic's, so `left_click`/`type`/`key` arrive under `action`
    with a `coordinate` pair, which is very nearly `_read_anthropic_action`'s
    shape.

    The COORDINATES are the interesting part, and this is the one axis where
    the seam's existing vocabulary genuinely earns its keep: Qwen's numbers are
    in the `smart_resize`d image's space, so they cannot be placed without
    knowing the size of the image the model was shown - which is exactly what
    `image_space` is and why `Dialect.read_actions` takes it. Handed `None`,
    this raises rather than guessing, per `_normalize_gemini_action`'s
    precedent.
    """
    args = raw.get("arguments")
    body: Mapping[str, Any] = args if isinstance(args, Mapping) else raw
    action = str(body.get("action") or "").strip()
    params: dict[str, Any] = dict(body)

    coord = params.get("coordinate")
    if isinstance(coord, (list, tuple)) and len(coord) == 2:
        if image_space is None:
            raise ValueError(
                f"qwen action {action!r} carries coordinates in the model's "
                "internal smart_resize image space, which cannot be placed "
                "without the size of the image the model was shown; display "
                "geometry is not resolved"
            )
        resized_w, resized_h = _qwen_smart_resize(image_space.width, image_space.height)
        params["coordinate"] = [
            float(coord[0]) * image_space.width / resized_w,
            float(coord[1]) * image_space.height / resized_h,
        ]
    return action, params


def _normalize_qwen_batch(
    bodies: list[Mapping[str, Any]], image_space: ImageSpace | None
) -> Iterator[Call]:
    """Lazy, for `_normalize_gemini_batch`'s reason: `ComputerTool.execute`
    calls `read_call` OUTSIDE its `try:`, so a `ValueError` raised at read time
    escapes the caller's error handling while the identical one raised during
    iteration becomes a clean tool error."""
    for body in bodies:
        yield _normalize_qwen_action(body, image_space)


def _read_qwen_actions(
    payload: Mapping[str, Any], image_space: ImageSpace | None
) -> Iterable[Call] | None:
    """THE SECOND BREAK - and this one is about the CALLER, not the callee.

    As a function this works: given the documented payload it extracts and
    normalizes correctly, and `tests/test_qwen_out_of_sample.py` proves it end
    to end through `read_call`.

    What does not work is that NOTHING EVER CALLS IT. `read_actions`' entire
    reachable surface is `read_call`, whose only caller is
    `ComputerTool.execute`, which the orchestrator invokes only for a parsed
    TOOL CALL: `amplifier-module-loop-streaming` does
    `tool_calls = provider.parse_tool_calls(response)` and, on an empty list,
    streams the response as final assistant text and never dispatches a tool
    (`amplifier_module_loop_streaming/__init__.py:1785-1787`). Qwen emits no
    tool call - its action is `<tool_call>...</tool_call>` INSIDE
    `message.content`, and `parse_tool_calls` returns `response.tool_calls`,
    which is empty.

    So the shim below (accepting `{"content": <str>}`) is a fiction with no
    producer. Something upstream would have to regex the assistant text and
    synthesize a tool call; that something is a provider module or the hook,
    both OUTSIDE `providers.py`, and one of them outside this repo entirely.

    Detection is by shape, per the incumbents: a `content` string containing at
    least one `<tool_call>` block.
    """
    content = payload.get("content")
    if not isinstance(content, str):
        return None
    bodies: list[Mapping[str, Any]] = []
    for blob in _QWEN_TOOL_CALL_RE.findall(content):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, Mapping):
            bodies.append(parsed)
    if not bodies:
        return None
    return _normalize_qwen_batch(bodies, image_space)


QWEN = Dialect(
    name="qwen",
    # PURE FICTION - see the probe result table above. Qwen contributes nothing
    # to `tools[]`, so there is no wire type to name and nothing this string
    # could ever be checked against. Gemini's equivalent lie at least
    # discriminates a real `tools[]` entry; this one discriminates nothing.
    tool_types=("qwen_computer_use",),
    declare=_declare_qwen,
    read_actions=_read_qwen_actions,
    # Not False-because-verified, False-because-inapplicable: Qwen returns the
    # screenshot as an ordinary user-turn image, not as a `tool_result` block,
    # so "must the result carry a screenshot" has no referent at all. The field
    # cannot express "this question does not apply to me".
    result_must_carry_screenshot=False,
    models={},
    beta_headers={},
)


#: The table. Order is NOT load-bearing for dispatch - `read_call` tries the
#: catch-all explicitly last rather than relying on where it sits here, so this
#: order is free to be whatever reads best. It IS observable in one place:
#: `tool_versions.KNOWN_MODEL_TOOL_VERSIONS` is assembled in this order and
#: `required_for_model` scans prefixes in insertion order, so keep the order
#: stable unless you have checked that scan.
DIALECTS: tuple[Dialect, ...] = (ANTHROPIC, OPENAI, GEMINI)

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


def read_call(
    payload: Mapping[str, Any], image_space: ImageSpace | None = None
) -> tuple[Dialect, Iterable[Call]]:
    """Which dialect this tool-call payload is written in, and the actions it
    asks for - in this tool's own `(action, params)` vocabulary, in MODEL pixel
    space, in order.

    `image_space` is the COORDINATE SPACE the payload's numbers live in - the
    size of the screenshot the model was actually shown. It is the one fact
    about the world outside the payload that reading a payload can legitimately
    require, and it is a parameter rather than something the table closes over
    because it is per-session and changes mid-session (`select_monitor`), so
    there is no correct value to bake in when `DIALECTS` is built at import
    time.

    Defaulted to `None` so a caller with no display context (every unit test
    that only exercises absolute-pixel dialects) needs no ceremony. That is not
    a silent degradation: a dialect that genuinely needs the size and is handed
    `None` raises `ValueError` naming the missing fact, which `execute()`
    already turns into an ordinary tool error. What it must never do is invent
    a size.

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
        actions = dialect.read_actions(payload, image_space)
        if actions is not None:
            return dialect, actions
    actions = DEFAULT_DIALECT.read_actions(payload, image_space)
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
