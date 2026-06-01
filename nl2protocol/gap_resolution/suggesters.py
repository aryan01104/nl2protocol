"""
Suggester implementations (ADR-0008 PR2).

Five deterministic suggesters lifted from existing pipeline logic, plus
LLMSpotSuggester (focused per-Gap LLM call) and IndependentReviewSuggester
(batched reviewer pass).

Per ADR-0008 the suggester precedence is fixed in registry order:
  1. Deterministic    correct-or-empty, no LLM
  2. LLM spot         per-Gap LLM with focused context
  3. Last-resort      (deferred — LLMFullRefineSuggester is opt-in only)

The orchestrator runs them in order; first non-None wins for a given Gap.

All suggestions attach `Provenance(source="inferred", ...)` (via the
returned Suggestion's `provenance_source` field) so the report's ▴ marker
+ tooltip surfaces every fill with its rationale.
"""

from __future__ import annotations

import re
from typing import Optional

from nl2protocol.gap_resolution.protocols import Suggester
from nl2protocol.gap_resolution.types import Gap, Suggestion


# ============================================================================
# Helpers — extract step index from a Gap.field_path
# ============================================================================

_STEP_INDEX_PATTERN = re.compile(r"steps\[(\d+)\]")


def _step_index(gap: Gap) -> Optional[int]:
    """Pull the 0-indexed step from a `steps[N].field` Gap.field_path."""
    if gap.step_order is not None:
        return gap.step_order - 1
    m = _STEP_INDEX_PATTERN.search(gap.field_path)
    return int(m.group(1)) if m else None


# ============================================================================
# 1. ConfigLookupSuggester — substance → source location via config
# ============================================================================

class ConfigLookupSuggester:
    """For a Gap on `step.source` where the step has a substance, search
    `config.labware.contents` for a well containing that substance.

    Matching logic: case-insensitive substring; first-match-wins over
    labware iteration order.

    Pre:    `gap.kind == "missing"`, gap.field_path ends in `.source`,
            spec has a substance on the relevant step,
            context["config"] populated.
    Post:   Returns Suggestion with the LocationRef value when a match is
            found, else None. Confidence 0.9 (config lookup is correct
            given the data, but the user might have meant a different
            container — slight room for human review).
    """

    def suggest(self, gap: Gap, spec, context: dict) -> Optional[Suggestion]:
        if not gap.field_path.endswith(".source"):
            return None
        if gap.kind != "missing":
            return None
        idx = _step_index(gap)
        if idx is None or idx >= len(spec.steps):
            return None
        step = spec.steps[idx]
        if step.substance is None:
            return None
        config = context.get("config", {})
        labware = config.get("labware", {})

        substance_val = step.substance.value
        substance_lower = substance_val.lower()
        for label, lw in labware.items():
            contents = lw.get("contents", {}) or {}
            for well, content_desc in contents.items():
                if isinstance(content_desc, str) and substance_lower in content_desc.lower():
                    # Mint a LocationRef value. Schema-correct fill:
                    #   * description = the bare config key (the suggester
                    #     fills this honestly — there's no user-wording to
                    #     preserve since the user never described this ref;
                    #     the orchestrator writes it in to satisfy the
                    #     LocationRef contract).
                    #   * resolved_label = the SAME config key. Setting this
                    #     directly is what keeps ConstraintChecker quiet
                    #     downstream (it reads `resolved_label or description`
                    #     to validate against config keys; we want the first
                    #     branch to win with a real key).
                    #   * provenance = describes the description/well decision
                    #     (low information here — the suggester invented both,
                    #     so source='inferred' with a brief reasoning).
                    #   * resolved_label_provenance = describes WHY this
                    #     particular config label was picked — the substance
                    #     match. This is what the IndependentReviewSuggester
                    #     reviews (per ADR-0009).
                    from nl2protocol.models.spec import LocationRef, Provenance
                    _synth_reason = (
                        f"Synthesized by ConfigLookupSuggester from the "
                        f"substance match below; no user wording existed "
                        f"for this LocationRef."
                    )
                    _synth_why_not = (
                        f"Instruction names the substance "
                        f"('{substance_val}') but does not state any "
                        f"location for it — entire ref is suggester-filled."
                    )
                    loc = LocationRef(
                        description=label,
                        well=well,
                        resolved_label=label,
                        description_provenance=Provenance(
                            source="inferred",
                            positive_reasoning=_synth_reason,
                            why_not_in_instruction=_synth_why_not,
                            confidence=0.9,
                        ),
                        wells_provenance=Provenance(
                            source="inferred",
                            positive_reasoning=_synth_reason,
                            why_not_in_instruction=_synth_why_not,
                            confidence=0.9,
                        ),
                        resolved_label_provenance=Provenance(
                            source="inferred",
                            positive_reasoning=(
                                f"Config labware '{label}' lists substance "
                                f"'{substance_val}' at well {well}."
                            ),
                            why_not_in_instruction=(
                                f"Instruction names the substance "
                                f"('{substance_val}') but does not state which "
                                f"labware/well it comes from."
                            ),
                            confidence=0.9,
                        ),
                    )
                    return Suggestion(
                        value=loc,
                        provenance_source="deterministic",
                        positive_reasoning=(
                            f"Config lookup: substance '{substance_val}' is "
                            f"listed in labware '{label}' at well {well}."
                        ),
                        why_not_in_instruction=(
                            f"The instruction names a substance ('{substance_val}') "
                            f"to transfer but does not state which labware/well it "
                            f"comes from."
                        ),
                        confidence=0.9,
                    )
        return None


# ============================================================================
# 2. CarryoverSuggester — wait_for_temperature inherits from prior set_temp
# ============================================================================

class CarryoverSuggester:
    """For a `wait_for_temperature` Gap with null `temperature`, inherit the
    value from the most recent prior `set_temperature` step.

    Per ADR-0006 this is a forced-by-action-semantics inference: "wait until
    the module reaches its target" can only refer to the most recent
    set_temperature target. Confidence 0.95 (semantically forced, not a guess).

    Pre:    gap.field_path ends in `.temperature`, the step's action is
            `wait_for_temperature`, current value is None.
    Post:   Returns Suggestion with the inherited celsius if a prior
            set_temperature exists, else None.
    """

    def suggest(self, gap: Gap, spec, context: dict) -> Optional[Suggestion]:
        if not gap.field_path.endswith(".temperature"):
            return None
        idx = _step_index(gap)
        if idx is None or idx >= len(spec.steps):
            return None
        step = spec.steps[idx]
        if step.action != "wait_for_temperature":
            return None
        if step.temperature is not None:
            return None  # not a gap; defensive

        # Walk backward for the most recent set_temperature.
        for prior in reversed(spec.steps[:idx]):
            if prior.action == "set_temperature" and prior.temperature is not None:
                celsius = prior.temperature.value
                from nl2protocol.models.spec import Provenance, ProvenancedTemperature
                temp = ProvenancedTemperature(
                    value=celsius,
                    provenance=Provenance(
                        source="inferred",
                        positive_reasoning=(
                            f"wait_for_temperature inherits from the most "
                            f"recent set_temperature in the protocol; that "
                            f"value is {celsius}°C."
                        ),
                        why_not_in_instruction=(
                            "The instruction said to wait for the temperature "
                            "to stabilize but did not re-state the target "
                            "temperature."
                        ),
                        confidence=0.95,
                    ),
                )
                return Suggestion(
                    value=temp,
                    provenance_source="deterministic",
                    positive_reasoning=(
                        f"wait_for_temperature inherits from the most recent "
                        f"set_temperature in the protocol; that value is {celsius}°C."
                    ),
                    why_not_in_instruction=(
                        "The instruction said to wait for the temperature to "
                        "stabilize but did not re-state the target temperature."
                    ),
                    confidence=0.95,
                )
        return None


# ============================================================================
# 3. WellCapacitySuggester — initial_contents.volume_ul defaults to capacity
# ============================================================================

# Conservative well-capacity defaults by load_name pattern. Lifted from the
# original _build_initial_volume_queue logic. Volumes in microliters.
_LABWARE_CAPACITY_HINTS = [
    # (substring matched against load_name, default volume in uL)
    ("96_wellplate", 300.0),
    ("384_wellplate", 80.0),
    ("12_reservoir", 15000.0),
    ("1_reservoir", 200000.0),
    ("eppendorf_1.5ml", 1500.0),
    ("eppendorf_2ml", 2000.0),
    ("falcon_15ml", 15000.0),
    ("falcon_50ml", 50000.0),
    ("tuberack", 1500.0),  # generic tube rack default
]


def _capacity_for_labware(load_name: str) -> float:
    for substr, capacity in _LABWARE_CAPACITY_HINTS:
        if substr in load_name.lower():
            return capacity
    return 1500.0  # safe fallback


class WellCapacitySuggester:
    """For an initial_contents Gap (path `initial_contents[N].volume_ul`),
    propose the labware's well capacity as the default volume.

    Lifted from `_build_initial_volume_queue`'s default-volume logic.

    Pre:    gap.field_path matches `initial_contents[N].volume_ul`,
            spec.initial_contents[N] populated, context["config"] has
            the labware listing.
    Post:   Returns Suggestion with the capacity value. Confidence 0.7
            (a real default, but the user may have meant a different
            volume — kept below the 0.85 auto-accept threshold so this
            always surfaces to the user).
    """

    _PATH_PATTERN = re.compile(r"initial_contents\[(\d+)\]\.volume_ul")

    def suggest(self, gap: Gap, spec, context: dict) -> Optional[Suggestion]:
        m = self._PATH_PATTERN.match(gap.field_path)
        if not m:
            return None
        idx = int(m.group(1))
        if idx >= len(spec.initial_contents):
            return None
        ic = spec.initial_contents[idx]
        config = context.get("config", {})

        # Map the user's labware description to a real config entry so the
        # load_name (and therefore the capacity lookup) is accurate. Three-
        # path resolution lets this suggester run in pipeline contexts that
        # supply the resolver's suggestions AND in test/CLI contexts that
        # don't.
        resolved_label, load_name = self._resolve_to_config(
            ic.labware, config, context.get("labware_suggestions"),
        )
        capacity = _capacity_for_labware(load_name)

        if resolved_label and load_name:
            reasoning = (
                f"'{ic.labware}' resolved to config '{resolved_label}' "
                f"(load_name '{load_name}'); a well in this container holds "
                f"~{capacity:.0f} \u00b5L. Using as default \u2014 edit if "
                f"your actual fill volume is lower."
            )
        else:
            reasoning = (
                f"Container '{ic.labware}' has no resolved config match; "
                f"using conservative ~{capacity:.0f} \u00b5L default. Edit "
                f"if your actual fill volume differs."
            )

        return Suggestion(
            value=capacity,
            provenance_source="deterministic",
            positive_reasoning=reasoning,
            why_not_in_instruction=(
                f"The instruction says well {ic.well} contains "
                f"'{ic.substance}' but does not state the volume."
            ),
            confidence=0.7,
        )

    @staticmethod
    def _resolve_to_config(description: str, config: dict,
                          labware_suggestions: Optional[dict]
                          ) -> "tuple[Optional[str], str]":
        """Map a user-language labware description to (config_label, load_name).

        Pre:    `description` is the user's wording (e.g. 'tube rack').
                `config` is the lab config dict — the source of truth for
                load_names; `config["labware"]` may be absent or empty.
                `labware_suggestions`, when supplied, is the dict returned
                by `LabwareResolver.suggest()` keyed on description; each
                value exposes a `.suggested_label` attribute (or None).

        Post:   Returns `(label, load_name)` where:
                  - `label` is the config key chosen for `description`, or
                    None when no path resolves.
                  - `load_name` is `config["labware"][label]["load_name"]`,
                    or '' when no path resolves OR the resolved label has
                    no load_name entry.
                Three lookup paths tried in order, first match wins:
                  1. `labware_suggestions[description].suggested_label`
                     (the proper path when the labware resolver has run
                     earlier in the pipeline).
                  2. `description` is already a literal key in
                     `config["labware"]` (the legacy/test path).
                  3. neither matches → returns `(None, '')`.

        Side effects: None.
        """
        label: Optional[str] = None
        labware_map = config.get("labware", {})
        if labware_suggestions:
            sug = labware_suggestions.get(description)
            suggested = (getattr(sug, "suggested_label", None)
                         if sug is not None else None)
            # CodeRabbit P1: the suggested_label must actually exist in
            # the active config. A stale label (resolver suggested a
            # value not present anymore) would otherwise be propagated
            # through to load_name lookup → empty load_name →
            # conservative-fallback capacity, with no chance to try
            # the legacy direct-config-key path below.
            if isinstance(suggested, str) and suggested in labware_map:
                label = suggested
        if label is None and description in labware_map:
            label = description
        if label is None:
            return None, ""
        load_name = labware_map.get(label, {}).get("load_name", "")
        return label, load_name


# ============================================================================
# 4. RegexFromNoteSuggester — extract embedded values from pause/comment notes
# ============================================================================

class RegexFromNoteSuggester:
    """For a `pause` Gap missing duration, scan the step's `note` for a
    duration phrase (e.g. "incubate 5 minutes", "wait 30s") and propose it.

    Pattern existed in `schema_builder.py` for wait_for_temperature note
    parsing; this generalizes it to pause-with-note.

    Pre:    gap.field_path ends in `.duration`, the step's action is
            `pause`, current duration is None, step.note is non-empty.
    Post:   Returns Suggestion with parsed duration if regex matches,
            else None. Confidence 0.8.
    """

    _DURATION_PATTERN = re.compile(
        r"(\d+(?:\.\d+)?)\s*(seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
        re.IGNORECASE,
    )

    _UNIT_NORMALIZE = {
        "s": "seconds", "sec": "seconds", "secs": "seconds",
        "second": "seconds", "seconds": "seconds",
        "m": "minutes", "min": "minutes", "mins": "minutes",
        "minute": "minutes", "minutes": "minutes",
        "h": "hours", "hr": "hours", "hrs": "hours",
        "hour": "hours", "hours": "hours",
    }

    def suggest(self, gap: Gap, spec, context: dict) -> Optional[Suggestion]:
        if not gap.field_path.endswith(".duration"):
            return None
        idx = _step_index(gap)
        if idx is None or idx >= len(spec.steps):
            return None
        step = spec.steps[idx]
        if step.action != "pause":
            return None
        if not step.note:
            return None
        m = self._DURATION_PATTERN.search(step.note)
        if not m:
            return None
        value = float(m.group(1))
        raw_unit = m.group(2).lower()
        unit = self._UNIT_NORMALIZE.get(raw_unit, "seconds")
        from nl2protocol.models.spec import Provenance, ProvenancedDuration
        dur = ProvenancedDuration(
            value=value,
            unit=unit,
            provenance=Provenance(
                source="inferred",
                positive_reasoning=(
                    f"Step note contains a duration phrase '{m.group(0)}'; "
                    f"interpreted as {value} {unit}."
                ),
                why_not_in_instruction=(
                    "The duration was embedded in the step's free-text note "
                    "rather than placed in the structured duration field."
                ),
                confidence=0.8,
            ),
        )
        return Suggestion(
            value=dur,
            provenance_source="deterministic",
            positive_reasoning=(
                f"Step note contains a duration phrase '{m.group(0)}'; "
                f"interpreted as {value} {unit}."
            ),
            why_not_in_instruction=(
                "The duration was embedded in the step's free-text note "
                "rather than placed in the structured duration field."
            ),
            confidence=0.8,
        )


# ============================================================================
# 5. WellRangeClipSuggester — constraint violation: wells out of labware range
# ============================================================================

# Pattern matching the constraint checker's error string for out-of-range wells.
# Today's message format: "Wells ['A7', 'A8'] do not exist on 'sample_rack'
# (load_name) ... Valid well range: A1-D6."
_OOR_WELL_PATTERN = re.compile(
    r"Wells\s+\[([^\]]+)\]\s+do not exist on\s+'([^']+)'", re.DOTALL
)
_VALID_RANGE_PATTERN = re.compile(r"Valid well range:\s+([A-H])(\d+)-([A-H])(\d+)")


class WellRangeClipSuggester:
    """For a constraint_violation Gap of the form "wells X,Y don't exist
    on labware Z (valid range A1-D6)", propose clipping to the valid range.

    NEW logic (no existing equivalent — today's pipeline blocks without
    suggestion). Confidence 0.7 (a fix, but the user might want a different
    labware instead — kept below auto-accept threshold).

    Pre:    gap.kind == "constraint_violation", gap.description contains
            the standard out-of-range message format from ConstraintChecker.
    Post:   Returns Suggestion with a sorted list of valid wells when the
            description parses, else None. The orchestrator hands this to
            the user; on accept, the spec's relevant LocationRef.wells
            field gets replaced.
    """

    def suggest(self, gap: Gap, spec, context: dict) -> Optional[Suggestion]:
        if gap.kind != "constraint_violation":
            return None
        m_oor = _OOR_WELL_PATTERN.search(gap.description)
        m_range = _VALID_RANGE_PATTERN.search(gap.description)
        if not m_oor or not m_range:
            return None
        # Parse offending wells (e.g. "'A7', 'A8'").
        offending = [w.strip().strip("'\"") for w in m_oor.group(1).split(",")]
        labware_label = m_oor.group(2)
        # Parse valid range (e.g. A1-D6).
        row_lo, col_lo_s, row_hi, col_hi_s = m_range.group(1, 2, 3, 4)
        col_lo, col_hi = int(col_lo_s), int(col_hi_s)
        # Enumerate the valid wells of the labware.
        valid = []
        for r in range(ord(row_lo), ord(row_hi) + 1):
            for c in range(col_lo, col_hi + 1):
                valid.append(f"{chr(r)}{c}")
        # Build the clipped well list: drop offending, keep originals
        # that are valid. We don't know the original list here without
        # reading the spec — for the suggestion shape, propose the valid
        # range as the new wells set. The orchestrator/handler can refine
        # if needed.
        return Suggestion(
            value=valid,
            provenance_source="deterministic",
            positive_reasoning=(
                f"Wells {offending} exceed the labware's valid range "
                f"({row_lo}{col_lo}-{row_hi}{col_hi}). Clipping to the "
                f"in-range wells preserves intent without exceeding capacity."
            ),
            why_not_in_instruction=(
                "The instruction specified wells beyond the labware's "
                "physical capacity; a fix is needed but the instruction "
                "doesn't say which way to compromise."
            ),
            confidence=0.7,
        )


# ============================================================================
# 6. LLMSpotSuggester — focused per-Gap LLM call
# ============================================================================
#
# Note: the former `LabwareSuggester` (token-overlap heuristic for ambiguous
# LocationRef Gaps) was deleted in the Phase 5 capability-matcher refactor.
# `LabwareMatcher` (extraction/resolver.py) now produces physically-pre-
# filtered candidates upstream, making the token-overlap fallback obsolete.

class LLMSpotSuggester:
    """For Gaps no deterministic suggester resolved, ask the LLM with
    focused context (just the relevant instruction snippet, config slice,
    and neighboring steps — NOT the whole spec).

    Per ADR-0008 this is the principled replacement for today's wholesale
    `refine`: bounded output (~300 tokens), can't regress correct fields,
    can't trigger token-truncation parse errors.

    The LLM is asked to either:
      (a) Find a verbatim instruction phrase that grounds the value
          (upgrade Gap to source="instruction"), OR
      (b) Propose a value with reasoning (upgrade to source="inferred").

    Returns the better answer, with structured positive + negative reasoning.

    Pre:    `client` is an Anthropic SDK client; `model_name` is a valid
            model id. The LLM call is real on production runs; tests
            inject a stub.
    Post:   Returns Suggestion with the LLM's value + reasoning, or None
            if the call/parse fails (failures degrade gracefully — the
            user will see the Gap unsuggested).
    """

    _SPOT_PROMPT_TEMPLATE = """You are filling ONE missing value in a protocol spec.

INSTRUCTION (relevant fragment):
{instruction_snippet}

CONFIG (relevant slice):
{config_slice}

NEIGHBORING STEPS:
{neighbors}

THE GAP:
- field: {field_path}
- step: {step_order}
- description: {description}
- current value: {current_value}

Your task: produce ONE JSON object with this exact shape:
{{
  "value": <the proposed value, in the same shape the spec field expects>,
  "positive_reasoning": "<one sentence: why this value is right>",
  "why_not_in_instruction": "<one sentence: name the specific element the instruction lacks; do not write 'not specified' generically — state what you would have expected to find>",
  "confidence": <0.0-1.0>
}}

Output ONLY the JSON object, no preamble.
"""

    def __init__(self, client, model_name: str):
        self._client = client
        self._model_name = model_name

    def suggest(self, gap: Gap, spec, context: dict) -> Optional[Suggestion]:
        prompt = self._build_prompt(gap, spec, context)
        try:
            response = self._client.messages.create(
                model=self._model_name,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            data = self._parse_response(text)
        except Exception:
            return None
        if data is None:
            return None
        return Suggestion(
            value=data["value"],
            provenance_source="inferred",
            positive_reasoning=data["positive_reasoning"],
            why_not_in_instruction=data.get("why_not_in_instruction"),
            confidence=float(data.get("confidence", 0.6)),
        )

    def _build_prompt(self, gap: Gap, spec, context: dict) -> str:
        instruction = context.get("instruction", "")
        config = context.get("config", {})
        # Pass the full instruction. A 600-char window dropped late lines
        # like "Mix each well gently 3 times at 100uL", so the suggester
        # invented values that were actually in the instruction. Typical
        # specs are <2KB; trim only if absurdly long.
        instruction_snippet = instruction[:4000]
        # Focused config slice: just the labware section, abbreviated.
        import json as _json
        config_slice = _json.dumps(
            {"labware": config.get("labware", {})}, indent=2
        )[:800]
        # Neighboring steps: 1 before + 1 after the gap's step.
        neighbors = self._neighbors_text(gap, spec)
        return self._SPOT_PROMPT_TEMPLATE.format(
            instruction_snippet=instruction_snippet,
            config_slice=config_slice,
            neighbors=neighbors,
            field_path=gap.field_path,
            step_order=gap.step_order or "(spec-level)",
            description=gap.description,
            current_value=repr(gap.current_value),
        )

    def _neighbors_text(self, gap: Gap, spec) -> str:
        if gap.step_order is None:
            return "(spec-level gap, no step neighbors)"
        idx = gap.step_order - 1
        lo = max(0, idx - 1)
        hi = min(len(spec.steps), idx + 2)
        return "\n".join(
            f"  step {s.order} ({s.action})" for s in spec.steps[lo:hi]
        )

    @staticmethod
    def _parse_response(text: str) -> Optional[dict]:
        """Extract the JSON object from the LLM response. Tolerates surrounding whitespace
        but not preamble — strict per the prompt."""
        import json as _json
        try:
            return _json.loads(text)
        except _json.JSONDecodeError:
            # Try to find a JSON object in the text (in case of preamble).
            import re as _re
            m = _re.search(r"\{[\s\S]*\}", text)
            if m:
                try:
                    return _json.loads(m.group(0))
                except _json.JSONDecodeError:
                    return None
            return None


# ============================================================================
# 7. IndependentReviewSuggester — batched reviewer pass
# ============================================================================

class IndependentReviewSuggester:
    """Batches every source="inferred"/"domain_default" provenance into one
    reviewer LLM call. Reviewer tests positive_reasoning AND
    why_not_in_instruction as two independently-falsifiable claims.

    Returns one ReviewResult per provenance, indexed by field_path.

    Per ADR-0008:
      - Use a different model from the extractor when possible (Haiku
        reviewing Sonnet) to mitigate same-model agreement bias.
      - Skeptical-auditor framing in prompt.
      - Per-iteration call (not per-pipeline), keeps reviewer in sync
        with what's been resolved so far.

    Note: this is structurally a "Suggester" (takes spec, produces
    structured output) but is special-cased in the orchestrator — it
    runs ONCE per iteration over ALL inferred values, not per-Gap.
    Therefore it doesn't strictly implement the Suggester protocol; it
    has its own `review(spec, context) -> Dict[str, ReviewResult]`
    method.
    """

    _REVIEW_PROMPT_TEMPLATE = """You are a SKEPTICAL AUDITOR reviewing another model's claims about a protocol spec.

For each claim below, evaluate TWO things INDEPENDENTLY:
  1. Is the POSITIVE reasoning sound? (Domain-knowledge check: does this value match standard lab practice / the action's semantics?)
  2. Is the NEGATIVE reasoning correct? (Text-grounding check: does the instruction REALLY not supply this value? Re-read the instruction; the original model may have missed something.)

INSTRUCTION:
{instruction}

CLAIMS TO REVIEW:
{claims_json}

For EACH claim, output a JSON object with exact shape:
{{
  "field_path": "<the claim's field_path>",
  "confirms_positive": <true/false>,
  "confirms_negative": <true/false>,
  "objection": "<one sentence specific objection — required when either confirms is false; omit or use null otherwise>"
}}

Output a JSON ARRAY of these objects, one per claim, in the same order. No preamble.
"""

    def __init__(self, client, model_name: str):
        self._client = client
        self._model_name = model_name

    def review(self, spec, context: dict) -> dict:
        """Walk the spec, batch every inferred/domain_default provenance
        into one reviewer call, return field_path -> ReviewResult.
        """
        claims = self._collect_claims(spec)
        if not claims:
            return {}
        return self._review_claims(claims, context)

    def review_suggestions(self, labware_suggestions: dict,
                            context: dict) -> dict:
        """Review labware-resolver suggestions BEFORE they reach the
        assignments modal so the modal can surface objections inline.

        Pre:    `labware_suggestions` is the dict from
                `LabwareMatcher.suggest()` — keyed on description,
                values are `LabwareMatchSuggestion` with .suggested_label,
                .positive_reasoning, .why_not_in_instruction,
                .confidence. `context` carries at least
                `{"instruction": <user instruction text>}` so the
                reviewer can text-ground claims.

        Post:   Returns `{description: objection_text}` for every
                suggestion the reviewer disagreed with. Descriptions
                the reviewer agreed with — or that had no
                suggested_label / no positive_reasoning to review —
                are omitted. Empty dict on LLM error / parse failure
                (degrade silently — caller continues with un-reviewed
                suggestions, no worse than today).
        Side effects: One Sonnet/Haiku call when there's at least one
                      reviewable suggestion. Otherwise no I/O.
        """
        claims = []
        for desc, sug in (labware_suggestions or {}).items():
            if sug is None or sug.suggested_label is None:
                continue
            if not sug.positive_reasoning:
                continue
            # field_path uses a synthetic "labware.{description}" key —
            # not a real spec path. The reviewer just round-trips it
            # back so we can re-key by description.
            claims.append({
                "field_path": f"labware.{desc}",
                "value": sug.suggested_label,
                "positive_reasoning": sug.positive_reasoning,
                "why_not_in_instruction": sug.why_not_in_instruction or "",
                "source": "inferred",
                "confidence": sug.confidence,
            })
        if not claims:
            return {}
        verdicts = self._review_claims(claims, context)
        objections: dict = {}
        for fp, result in verdicts.items():
            if not fp.startswith("labware."):
                continue
            desc = fp[len("labware."):]
            agreed = result.confirms_positive and result.confirms_negative
            if not agreed and result.objection:
                objections[desc] = result.objection
        return objections

    def _review_claims(self, claims: list, context: dict) -> dict:
        """Internal: send a pre-built claim list through the reviewer
        LLM and parse the response. Shared by `review` (spec-walk
        claims) and `review_suggestions` (resolver-suggestion claims).
        """
        prompt = self._build_prompt(claims, context)
        try:
            response = self._client.messages.create(
                model=self._model_name,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            verdicts = self._parse_response(text)
        except Exception:
            return {}
        if verdicts is None:
            return {}
        from nl2protocol.gap_resolution.types import ReviewResult
        results = {}
        for v in verdicts:
            try:
                fp = v["field_path"]
                cp = bool(v["confirms_positive"])
                cn = bool(v["confirms_negative"])
                objection = v.get("objection") or None
                # Defensive: if both confirmed, force objection to None.
                if cp and cn:
                    objection = None
                # If either disconfirmed and objection missing, synthesize.
                if not (cp and cn) and not objection:
                    objection = "(reviewer disagreed but provided no objection text)"
                results[fp] = ReviewResult(
                    field_path=fp,
                    confirms_positive=cp,
                    confirms_negative=cn,
                    objection=objection,
                )
            except (KeyError, ValueError):
                continue
        return results

    @staticmethod
    def _collect_claims(spec) -> list:
        """Walk every provenanced atomic value AND every LocationRef's
        resolved_label_provenance; emit a claim for each non-instruction
        provenance.

        Per ADR-0008 PR3a step 3: labware-resolution claims (which config
        labware the resolver picked for each user-language description)
        flow through the same reviewer machinery as inferred spec values.
        Both surfaces share the Provenance schema and the two-claim
        verification protocol; one batched reviewer call covers both."""
        claims = []
        for step_idx, step in enumerate(spec.steps):
            # Atomic-value claims: volume / duration / temperature / substance.
            for fname in ("volume", "duration", "temperature", "substance"):
                field_obj = getattr(step, fname, None)
                if field_obj is None:
                    continue
                prov = getattr(field_obj, "provenance", None)
                if prov is None or prov.source == "instruction":
                    continue
                claims.append({
                    "field_path": f"steps[{step_idx}].{fname}",
                    "value": repr(field_obj.value),
                    "positive_reasoning": prov.positive_reasoning or "",
                    "why_not_in_instruction": prov.why_not_in_instruction or "",
                    "source": prov.source,
                    "confidence": prov.confidence,
                })

            # LocationRef-field claims (source / destination). Each ref has
            # two provenance slots; emit one claim per non-instruction slot
            # whose verdict the orchestrator routes back to the matching
            # slot. Both slots claim the SAME field_path because today's
            # _apply_reviewer_verdicts propagates a single verdict to both —
            # so reviewing either reviews the field as a whole. Skip when
            # both slots are instruction-sourced (nothing to verify).
            for role in ("source", "destination"):
                ref = getattr(step, role, None)
                if ref is None:
                    continue
                # Prefer the slot whose source is NOT 'instruction' (that's
                # the one with reasoning the reviewer needs to verify).
                prov = None
                for prov_attr in ("description_provenance", "wells_provenance"):
                    candidate = getattr(ref, prov_attr, None)
                    if candidate and candidate.source != "instruction":
                        prov = candidate
                        break
                if prov is None:
                    continue
                value_repr = (
                    f"{ref.description} "
                    f"{ref.well or ref.wells or ref.well_range or ''}"
                ).strip()
                claims.append({
                    "field_path": f"steps[{step_idx}].{role}",
                    "value": value_repr,
                    "positive_reasoning": prov.positive_reasoning or "",
                    "why_not_in_instruction": prov.why_not_in_instruction or "",
                    "source": prov.source,
                    "confidence": prov.confidence,
                })

            # Labware-resolution claims: resolved_label_provenance on each
            # LocationRef. Field path uses the .resolved_label suffix so the
            # stamp helper routes verdicts to resolved_label_provenance, not
            # the LocationRef's primary provenance.
            for role in ("source", "destination"):
                ref = getattr(step, role, None)
                if ref is None:
                    continue
                rprov = getattr(ref, "resolved_label_provenance", None)
                if rprov is None or rprov.source == "instruction":
                    continue
                claims.append({
                    "field_path": f"steps[{step_idx}].{role}.resolved_label",
                    "value": ref.resolved_label or "",
                    "positive_reasoning": rprov.positive_reasoning or "",
                    "why_not_in_instruction": rprov.why_not_in_instruction or "",
                    "source": rprov.source,
                    "confidence": rprov.confidence,
                })
        return claims

    def _build_prompt(self, claims: list, context: dict) -> str:
        import json as _json
        return self._REVIEW_PROMPT_TEMPLATE.format(
            instruction=context.get("instruction", "")[:1500],
            claims_json=_json.dumps(claims, indent=2),
        )

    @staticmethod
    def _parse_response(text: str) -> Optional[list]:
        import json as _json
        try:
            data = _json.loads(text)
            return data if isinstance(data, list) else None
        except _json.JSONDecodeError:
            import re as _re
            m = _re.search(r"\[[\s\S]*\]", text)
            if m:
                try:
                    data = _json.loads(m.group(0))
                    return data if isinstance(data, list) else None
                except _json.JSONDecodeError:
                    return None
            return None
