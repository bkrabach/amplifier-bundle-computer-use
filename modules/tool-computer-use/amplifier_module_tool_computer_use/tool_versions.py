"""Model <-> Anthropic computer-use tool-type compatibility.

Anthropic's server-side computer-use tool type (`computer_20250124`,
`computer_20251124`, ...) is versioned per model generation. Declaring the wrong
`type` for the model that actually receives the request is rejected by the API
with a 400 - on *every single turn*, not as a transient failure. This module is
the fix for that defect: `ComputerTool` used to hardcode a single default
version (`computer_20251124`) regardless of which model was in use, which 400s
every request the moment a session's model differs from whatever that hardcoded
default happened to match - most commonly via `provider-anthropic`'s own
model-fallback (Haiku/Sonnet fallback chains), which silently switches models
mid-session with no signal to this module.

Evidence, not a guess
----------------------
The table below is built from a live verification against the real Anthropic
API (2026-08), not inferred from model-naming conventions:

    model                          tool_version         result
    claude-sonnet-4-5-20250929     computer_20250124    ACCEPTED (native tool_use)
    claude-sonnet-4-5-20250929     computer_20251124    REJECTED (400)
    claude-sonnet-5                computer_20250124    REJECTED (400)
    claude-sonnet-5                computer_20251124    ACCEPTED (native tool_use)
    claude-opus-5                  computer_20250124    REJECTED (400)
    claude-opus-5                  computer_20251124    ACCEPTED (native tool_use)

Deliberately NOT exhaustive: a model absent from this table is *unverified*,
not unsupported. `resolve_tool_version`/`require_static_pairing` never invent a
compatibility guess for it - see their docstrings. Extend this table only from
another verified 200/400 pair against the real API, never from inference about
naming conventions.

Two resolution points, two different failure postures
-------------------------------------------------------
* `require_static_pairing()` - called once, at `ComputerTool.__init__` (mount
  time), over whatever is *statically configured* (`tool_version`, `model`
  config hints). This is where an unresolvable pairing is allowed to fail
  loudly: mount is the earliest, cheapest point to catch a config mistake, and
  nothing has been sent to a live conversation yet.
* `resolve_tool_version()` - called on every `provider:request`, over the
  *actual* model about to receive the request (`ChatRequest.model`, when the
  hook-computer-use wrapper can see it). This never raises: an exception here
  would take down the request pipeline mid-session (exactly the class of bug
  D3 already fixed for `native_tool_spec`). Instead, a known model always wins
  and silently-corrects a stale config - because continuing to emit a
  known-wrong pairing is *exactly* the "silently emit a spec that will 400"
  outcome this whole module exists to prevent - while an unknown model just
  keeps whatever was already resolved (no flapping on a single unrecognised
  turn).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ToolVersionError(RuntimeError):
    """A computer-use tool_version/model pairing could not be safely resolved.

    Raised only by `require_static_pairing()` (mount time). Never raised by
    `resolve_tool_version()` (request time) - see module docstring.
    """


#: `anthropic-beta` header required to opt into each native tool type.
BETA_HEADER_FOR_VERSION: dict[str, str] = {
    "computer_20251124": "computer-use-2025-11-24",
    "computer_20250124": "computer-use-2025-01-24",
    "computer_20241022": "computer-use-2024-10-22",
}

#: Verified model -> required tool_version pairs. See module docstring.
KNOWN_MODEL_TOOL_VERSIONS: dict[str, str] = {
    # Keyed on the UNDATED generation prefix, not the dated id the evidence was
    # captured against (`claude-sonnet-4-5-20250929`). `required_for_model`
    # matches `model.startswith(known)`, so a dated key can only ever match the
    # exact dated id - the plain alias `claude-sonnet-4-5`, which is what a
    # provider commonly reports, fell through to FALLBACK_TOOL_VERSION
    # (`computer_20251124`) and 400'd every request. Found by the evaluation
    # harness on its first real run, against a DTU whose provider reported the
    # undated alias. The undated prefix covers both forms.
    "claude-sonnet-4-5": "computer_20250124",
    "claude-sonnet-5": "computer_20251124",
    "claude-opus-5": "computer_20251124",
    # OpenAI's Responses API native `computer` tool type is bare - no date,
    # no version suffix (unlike Anthropic's per-generation `computer_YYYYMMDD`
    # scheme above; see amplifier-module-provider-openai's own
    # `_convert_tools_from_request`, which accepts ONLY `{"type": "computer"}`
    # verbatim - any other field 400s "Unknown parameter"). Verified live
    # end-to-end through this bundle against gpt-5.5 (2026-08-03): a real
    # `computer_call` batch (`{"actions": [{"type": "screenshot"}]}`) against
    # a real remote desktop, correctly returned a `computer_call_output` the
    # model then read and reasoned over (reported the on-screen clock
    # changing between two screenshots taken 15s apart).
    "gpt-5.5": "computer",
}

#: Used only when neither an explicit `tool_version` config value nor any
#: resolvable model is available anywhere (mount-time static config carried
#: neither, and no live model has been observed yet). Matches this module's
#: original hardcoded default so a config that specifies neither keeps working
#: exactly as it did before this fix - the defect this module closes is a
#: *wrong* pairing being used silently, not the existence of a starting value.
FALLBACK_TOOL_VERSION = "computer_20251124"


def known_models() -> tuple[str, ...]:
    """Every model name/prefix this module can verify a pairing for."""
    return tuple(KNOWN_MODEL_TOOL_VERSIONS)


def required_for_model(model: str) -> str | None:
    """The verified tool_version for `model`, or `None` if unverified.

    Tries an exact match first, then a longest-known-prefix match. Model ids
    routinely carry a dated suffix (`claude-sonnet-4-5-20250929`) that a
    hand-maintained table cannot enumerate for every dated release of a
    generation sharing one tool-use contract - `claude-sonnet-5` and
    `claude-opus-5` above have *no* date suffix precisely because the verified
    evidence was captured against the undated alias. Prefix matching lets one
    table entry cover a whole generation without guessing at unverified
    entries; it never widens what counts as "verified".
    """
    if not model:
        return None
    if model in KNOWN_MODEL_TOOL_VERSIONS:
        return KNOWN_MODEL_TOOL_VERSIONS[model]
    for known, version in KNOWN_MODEL_TOOL_VERSIONS.items():
        if model.startswith(known):
            return version
    return None


def require_static_pairing(model_hint: str | None, configured: str | None) -> str:
    """Resolve `tool_version` from *static* mount-time config. May raise.

    Call once, at `ComputerTool.__init__`. `model_hint` is the optional
    `config.model` value a bundle author can set to declare which model this
    session's provider is configured for; `configured` is the optional
    `config.tool_version` override. Both are static, explicit, human-set
    values - exactly the kind of "explicit ask" this codebase's `target_monitor`
    precedent (see `__init__.py`) says must fail loud rather than silently
    guess when it's wrong.

    Raises `ToolVersionError` when:
      - `model_hint` is a KNOWN model and `configured` names a DIFFERENT,
        verified-incompatible tool_version (an explicit, resolvable conflict);
      - `model_hint` is given but UNVERIFIED, and no `configured` override
        exists to fall back on (nothing safe to emit at all).

    Never raises when nothing is configured (returns `FALLBACK_TOOL_VERSION`)
    or when `configured` is set with no model hint to check it against (trusts
    the explicit value - see `resolve_tool_version` for the request-time
    dynamic correction that also protects this case in production).
    """
    if model_hint:
        required = required_for_model(model_hint)
        if required is not None:
            if configured and configured != required:
                raise ToolVersionError(
                    f"configured tool_version={configured!r} is incompatible with "
                    f"model {model_hint!r}: the verified requirement is "
                    f"{required!r}. Set config.tool_version={required!r}, or drop "
                    "the override and let it auto-resolve from config.model."
                )
            return required
        if configured:
            logger.warning(
                "computer-use: model %r is not in the verified model->tool_version "
                "table (known: %s); trusting the explicitly configured "
                "tool_version=%r unverified",
                model_hint,
                ", ".join(known_models()),
                configured,
            )
            return configured
        raise ToolVersionError(
            f"model {model_hint!r} is not in the verified model->tool_version "
            f"table (known: {', '.join(known_models())}) and no tool_version is "
            "configured. Set tools.computer.config.tool_version explicitly to "
            "unblock (see Anthropic's current computer-use tool types), or "
            "correct config.model to a verified one."
        )
    if configured:
        return configured
    return FALLBACK_TOOL_VERSION


def resolve_tool_version(
    model: str | None,
    configured: str | None,
    *,
    previous: str | None = None,
) -> tuple[str, bool]:
    """Resolve `tool_version` for the model about to receive THIS request.

    Call on every `provider:request`, with `model` being `ChatRequest.model`
    (or whatever the caller has determined the live, about-to-be-used model
    is). Never raises - see module docstring for why.

    Returns `(tool_version, corrected)`; `corrected` is True iff the returned
    value differs from what was previously in effect (`previous`, falling back
    to `configured`), so callers can log a correction instead of silently
    swapping it.

    Resolution order:
    1. `model` is known -> ALWAYS return its verified requirement, even if it
       overrides an explicit `configured` value or the previously-resolved
       one. A stale value paired with a model that does not support it 400s on
       *every* request until fixed; auto-correcting to the verified pairing is
       what keeps this module from silently 400ing forever.
    2. `model` is unset or unverified, `configured` is set -> trust it. An
       unrecognised model cannot be verified, and refusing to operate on every
       model this table doesn't happen to enumerate yet would be worse than
       trusting a deliberate, explicit config value.
    3. `model` is unset or unverified, `configured` is unset, `previous` is
       set -> keep the last resolved value (session continuity: do not flap
       tool_version on a turn where the model happens to be unrecognised).
    4. Nothing known at all -> `FALLBACK_TOOL_VERSION`.
    """
    if model:
        required = required_for_model(model)
        if required is not None:
            baseline = previous or configured
            return required, (baseline is not None and baseline != required)
    if configured:
        return configured, False
    if previous:
        return previous, False
    return FALLBACK_TOOL_VERSION, False


def beta_header_for(tool_version: str) -> str:
    """The `anthropic-beta` header value for `tool_version`.

    Falls back to the header for `FALLBACK_TOOL_VERSION` for a `tool_version`
    string this module has never seen (e.g. a brand new, explicitly configured
    type) - a missing beta header degrades to "server treats it as an ordinary
    tool", not a crash.
    """
    return BETA_HEADER_FOR_VERSION.get(
        tool_version, BETA_HEADER_FOR_VERSION[FALLBACK_TOOL_VERSION]
    )
