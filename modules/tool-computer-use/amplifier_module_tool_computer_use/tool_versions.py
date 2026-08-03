"""Model <-> computer-use tool-type RESOLUTION POLICY.

The vendor data this module resolves *over* is not here - it lives in
`providers.py`, one row per dialect, next to the wire format that row implies
(this module used to hold that table itself, mixing Anthropic's versioned
`computer_YYYYMMDD` types with OpenAI's bare `computer` in one flat dict, with
no way to tell which vendor a row belonged to). What lives here is the policy:
*when* to resolve, *which* source wins, and *whether* an unresolvable pairing
is allowed to raise.

Why any of this exists: Anthropic's server-side computer-use tool type is
versioned per model generation. Declaring the wrong `type` for the model that
actually receives the request is rejected by the API with a 400 - on *every
single turn*, not as a transient failure. `ComputerTool` used to hardcode a
single default version (`computer_20251124`) regardless of which model was in
use, which 400s every request the moment a session's model differs from
whatever that hardcoded default happened to match - most commonly via
`provider-anthropic`'s own model-fallback (Haiku/Sonnet fallback chains), which
silently switches models mid-session with no signal to this module.

Evidence, not a guess
----------------------
`providers.py`'s per-dialect `models` tables are built from live verification
against the real APIs (2026-08), not inferred from model-naming conventions:

    model                          tool_version         result
    claude-sonnet-4-5-20250929     computer_20250124    ACCEPTED (native tool_use)
    claude-sonnet-4-5-20250929     computer_20251124    REJECTED (400)
    claude-sonnet-5                computer_20250124    REJECTED (400)
    claude-sonnet-5                computer_20251124    ACCEPTED (native tool_use)
    claude-opus-5                  computer_20250124    REJECTED (400)
    claude-opus-5                  computer_20251124    ACCEPTED (native tool_use)
    gpt-5.5                        computer             ACCEPTED (native computer_call)

Deliberately NOT exhaustive: a model absent from those tables is *unverified*,
not unsupported. `resolve_tool_version`/`require_static_pairing` never invent a
compatibility guess for it - see their docstrings. Extend a dialect's `models`
only from another verified 200/400 pair against the real API, never from
inference about naming conventions.

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

from .providers import beta_headers, dialect_for_tool_type, model_tool_types

logger = logging.getLogger(__name__)


class ToolVersionError(RuntimeError):
    """A computer-use tool_version/model pairing could not be safely resolved.

    Raised only by `require_static_pairing()` (mount time). Never raised by
    `resolve_tool_version()` (request time) - see module docstring.
    """


#: Header required to opt into each native tool type, assembled from every
#: dialect's own `beta_headers` (`providers.py`). Anthropic contributes all of
#: today's entries; OpenAI has no such concept and contributes none.
BETA_HEADER_FOR_VERSION: dict[str, str] = beta_headers()

#: Verified model -> required tool_version pairs, assembled from every
#: dialect's own `models` table (`providers.py`). Iteration order follows
#: `providers.DIALECTS`, which `required_for_model`'s prefix scan depends on.
KNOWN_MODEL_TOOL_VERSIONS: dict[str, str] = model_tool_types()

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


def beta_header_for(tool_version: str) -> str | None:
    """The beta header that opts `tool_version` into native tool use, or `None`
    when the vendor that owns it HAS NO SUCH CONCEPT.

    `None` and "some header string" are different answers, and this function
    used to be unable to tell them apart. It was a flat
    `BETA_HEADER_FOR_VERSION.get(tool_version, <anthropic's fallback>)`, so
    every type not in Anthropic's table - including OpenAI's bare `computer`,
    whose vendor has no beta-header mechanism at all - was handed Anthropic's
    `computer-use-2025-11-24`. An empty `beta_headers` on a `Dialect` said
    "this vendor has no such concept" and the lookup heard "unknown type".

    The distinction is recoverable from the table already in hand, with no new
    field: ask which dialect OWNS the type.

      * Owned by a dialect -> that dialect's own answer is authoritative,
        including the absence of one. `computer` -> `None`, because OpenAI has
        no beta header, not because we failed to find one.
      * Owned by nobody (`dialect_for_tool_type` fell through to
        `DEFAULT_DIALECT`) -> genuinely unknown, e.g. a brand new, explicitly
        configured Anthropic type this build predates. Keep the historical
        fallback: a *wrong-generation* Anthropic beta header still opts into
        computer-use, whereas no header at all silently degrades the tool to an
        ordinary function tool.

    Incumbent behaviour is unchanged, and that is a property of the data rather
    than a coincidence: every one of `ANTHROPIC.tool_types` has an entry in
    `ANTHROPIC.beta_headers`, so "owned but absent" cannot arise for it.
    `tests/test_provider_dialects.py` pins that as an invariant of the table -
    a dialect's `beta_headers` must be empty (no such concept) or total over
    its `tool_types` (no accidental holes) - so a future type added without a
    header fails the suite instead of silently resolving to `None`.
    """
    dialect = dialect_for_tool_type(tool_version)
    if tool_version in dialect.tool_types:
        return dialect.beta_headers.get(tool_version)
    return BETA_HEADER_FOR_VERSION[FALLBACK_TOOL_VERSION]
