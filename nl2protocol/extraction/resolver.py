"""
resolver.py — Resolves user-language labware descriptions to config labels.

One LLM call maps every unique description that appears in the spec to a
config label (or null when no reasonable match exists). 
The resolver returns SUGGESTIONS — it does NOT mutate the spec.
The pipeline's labware-assignments confirmation flow is the sole writer of
`resolved_label` and `resolved_label_provenance` on LocationRef objects;
that lets the provenance honestly reflect whether the user accepted the
resolver's pick or overrode it (review_status="user_accepted_suggestion"
vs "user_edited").

Suggest → Confirm → Apply mirrors the orchestrator pattern: produce
suggestions, present them to the user, then commit with truthful audit
trail. The previous Apply → Confirm flow stamped a generic
`source="inferred"` Provenance with `review_status="original"` before
the user had a chance to override, and user overrides only updated
`resolved_label` (leaving the provenance saying the resolver picked it).
"""

import json
from dataclasses import dataclass
from typing import List, Optional, Tuple

from nl2protocol.constants import DEFAULT_MODEL
from nl2protocol.models.spec import LocationRef, ProtocolSpec


@dataclass(frozen=True)
class LabwareMatchSuggestion:
    """The resolver's tentative pick for one labware description.

    Carries the suggested label + the reasoning the resolver constructed
    for the pick, plus the candidate list the user can pick from in the
    confirmation UI. `suggested_label` is None when the resolver could
    not pick (LLM returned null, or returned a label that doesn't exist
    in config).

    `branch` records WHICH path produced this suggestion so the modal
    can suppress UI for the deterministic case (single candidate fit
    both capability and type filters — nothing to ask the user) and
    show the right shape for ambiguous / namespace-split / unresolvable
    cases. Values:
      - "deterministic": exactly one capability+type survivor; no LLM call.
      - "llm":           2+ survivors after filters; LLM picked among them.
      - "namespace_split": 0 survivors AND wells partition cleanly by
                          letter prefix into ≥2 subgroups each fitting
                          a distinct config labware (handled by the
                          separate `NamespaceSplitDetector` + modal).
      - "unresolvable":  0 survivors with no clean partition; user
                          must pick from full candidate list (or fix
                          the config).

    Used by `LabwareMatcher.suggest()`; consumed by the pipeline's
    labware-assignments confirmation flow which decides whether to
    stamp the suggestion's reasoning into a Provenance (user accepted)
    or construct override-reasoning (user picked a different label).
    """

    description: str
    suggested_label: Optional[str]
    positive_reasoning: Optional[str]
    why_not_in_instruction: Optional[str]
    confidence: float
    candidates: List[str]
    branch: str = "unresolvable"


# ============================================================================
# Capability + type filters (module-level so tests can exercise without LLM)
# ============================================================================

# Closed set of Opentrons load_name categories the type filter recognizes.
# Anything else returned by `_load_name_category` is treated as unknown and
# the candidate stays in the running (fail-open).
_KNOWN_LOAD_NAME_CATEGORIES = frozenset({
    "tiprack", "tuberack", "wellplate", "reservoir", "aluminumblock",
})


def _load_name_category(load_name: str) -> Optional[str]:
    """Extract the category token from an Opentrons-style load_name.

    Pre:    `load_name` is the string stored in `config["labware"][k]
            ["load_name"]`. Opentrons convention is
            `<vendor>_<count>_<type>_<details...>` (e.g.
            `opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap`),
            so the category lives at index 2 after splitting on `_`.

    Post:   Returns the third underscore-separated token iff that token
            is in the closed set
            {"tiprack","tuberack","wellplate","reservoir","aluminumblock"}.
            Returns `None` when `load_name` has fewer than 3 segments
            OR the third token is not in the closed set. Empty string
            input also returns `None`.

    Side effects: None. Pure.
    """
    if not load_name:
        return None
    parts = load_name.split("_")
    if len(parts) < 3:
        return None
    category = parts[2]
    if category in _KNOWN_LOAD_NAME_CATEGORIES:
        return category
    return None


def _description_category_filter(description: str) -> Optional[set]:
    """Map a user-written labware description to the categories it allows.

    Pre:    `description` is the user's free-text labware reference,
            e.g. "tube rack", "Eppendorf tubes", "tipbox", "96-well
            plate", "reservoir", "rack". Match is case-insensitive on
            substring.

    Post:   Returns a `set` of allowed categories when the description
            carries a strong category cue:
              - contains "tube"/"tubes"/"eppendorf"/"falcon"/"1.5ml"/"2ml"
                → {"tuberack", "aluminumblock"}
              - contains "tip"/"tips"/"tipbox"/"tiprack"
                → {"tiprack"}
              - contains "plate"/"wellplate"/"microplate"/"96-well"/"384-well"
                → {"wellplate"}
              - contains "reservoir"/"trough"/"basin"
                → {"reservoir"}
            Returns `None` for ambiguous / no-cue descriptions (e.g.
            bare "rack", or anything none of the rules match). `None`
            means "do not filter by type" — let the LLM / capability
            filter alone decide.

    Side effects: None. Pure.
    """
    if not description:
        return None
    d = description.lower()
    # Tip cues first so "tip rack" doesn't fall into the bare-"rack" branch.
    if any(k in d for k in ("tipbox", "tiprack", "tip rack")):
        return {"tiprack"}
    # "tips" / "tip" — match as whole tokens (or with trailing s) so words
    # like "stripe" don't trigger.
    tokens = set(d.replace(",", " ").split())
    if "tip" in tokens or "tips" in tokens:
        return {"tiprack"}
    if any(k in d for k in ("reservoir", "trough", "basin")):
        return {"reservoir"}
    if any(k in d for k in ("wellplate", "microplate", "well plate",
                              "96-well", "384-well", "plate")):
        return {"wellplate"}
    if any(k in d for k in ("tube", "tubes", "eppendorf", "falcon",
                              "1.5ml", "2ml")):
        return {"tuberack", "aluminumblock"}
    return None


def _capability_filter(wells: set, config: dict) -> List[str]:
    """Drop config labware that can't physically hold every well referenced.

    Pre:    `wells` is the aggregated set of well IDs the user
            referenced for ONE description across the whole spec
            (e.g. {"A1","A2",...,"C7"}). `config` is the lab config
            dict (the same shape `LabwareMatcher.config` carries).

    Post:   Returns a list of config labware keys (preserving config
            dict insertion order) whose
            `get_well_info(load_name)["valid_wells"]` is a superset of
            `wells`. Special cases:
              - `wells` empty → return ALL labware keys (no constraint).
              - `get_well_info` raises `ValueError` (unknown load_name)
                → KEEP that candidate (fail-open, matches the
                convention in constraints.py:553-559 where unknown
                load_names skip the constraint).
              - `config` has no "labware" key OR an empty one → `[]`.

    Side effects: None. Calls `get_well_info` per candidate.
    """
    from nl2protocol.models.labware import get_well_info
    labware_map = (config or {}).get("labware", {})
    if not labware_map:
        return []
    survivors: List[str] = []
    for label, lw in labware_map.items():
        if not wells:
            survivors.append(label)
            continue
        load_name = lw.get("load_name", "")
        try:
            info = get_well_info(load_name)
        except ValueError:
            # Fail-open: unknown load_name keeps the candidate; constraints
            # checker re-flags downstream (mirrors constraints.py:553-559).
            survivors.append(label)
            continue
        valid = set(info.get("valid_wells", []))
        if wells.issubset(valid):
            survivors.append(label)
    return survivors


def _type_filter(description: str, candidates: List[str],
                 config: dict) -> List[str]:
    """Drop candidates whose load_name category contradicts the description.

    Pre:    `description` is the user wording for one labware
            reference. `candidates` is a list of config labware keys
            (typically the output of `_capability_filter`). `config`
            carries each candidate's `load_name`.

    Post:   Returns the candidates filtered by the rules in
            `_description_category_filter`:
              - If filter is `None` (no category cue): return
                `candidates` unchanged.
              - Else: keep candidate iff
                `_load_name_category(config["labware"][c]["load_name"])
                in allowed_categories`. A candidate whose
                `_load_name_category` returns `None` (unknown shape)
                is DROPPED when a filter is active — we trust the
                category cue over an unparseable load_name.
            Preserves input order. Returns `[]` if everything is
            filtered out.

    Side effects: None. Pure.
    """
    allowed = _description_category_filter(description)
    if allowed is None:
        return list(candidates)
    labware_map = (config or {}).get("labware", {})
    survivors: List[str] = []
    for label in candidates:
        load_name = labware_map.get(label, {}).get("load_name", "")
        category = _load_name_category(load_name)
        if category in allowed:
            survivors.append(label)
    return survivors


def _has_namespace_split(description: str, wells: set,
                         config: dict) -> Optional[dict]:
    """Test whether referenced wells partition cleanly by letter prefix
    into ≥2 subgroups, each fitting a distinct config labware.

    Pre:    `description` is the user wording (used for type filter on
            each subgroup). `wells` is the aggregated well set that
            `_capability_filter` already proved fits NO single labware.
            `config` is the lab config dict.

    Post:   Returns a dict
              {
                "partition":      {prefix_letter: sorted_list_of_wells},
                "candidate_pairs": [(prefix_letter, fitting_label), ...],
              }
            iff:
              - every well in `wells` matches the regex `^([A-Z])(\\d+)$`,
              - grouping by the leading letter yields ≥2 groups,
              - EVERY group's well subset fits at least one config
                labware passing both capability AND type filter.
            Returns `None` otherwise. Strict by design: if some
            subgroup can't be hosted (e.g., C7 fits nothing in
            config), the detector stays silent and downstream constraint
            checking surfaces the real issue ("no labware fits C7")
            rather than proposing a mapping the user will then have
            to fight.

    Side effects: None. Calls `_capability_filter` + `_type_filter` per
                  subgroup.
    """
    import re as _re
    grouped: dict = {}
    for w in wells:
        m = _re.match(r"^([A-Z])(\d+)$", w)
        if not m:
            return None
        grouped.setdefault(m.group(1), []).append(w)
    if len(grouped) < 2:
        return None
    candidate_pairs: list = []
    for prefix in sorted(grouped.keys()):
        sub_wells = set(grouped[prefix])
        survivors = _type_filter(description,
                                  _capability_filter(sub_wells, config),
                                  config)
        if not survivors:
            return None
        candidate_pairs.append((prefix, survivors[0]))
    return {
        "partition": {p: sorted(grouped[p]) for p in sorted(grouped.keys())},
        "candidate_pairs": candidate_pairs,
    }


# public artifact of the file, specifically suggest func.
class LabwareMatcher:
    """Produces labware suggestions for user-language descriptions.

    One LLM call maps every unique description in the spec to a config
    label (or null when no reasonable match exists). Returns a dict
    `{description: LabwareSuggestion}` covering EVERY unique description
    the spec carries; entries whose `suggested_label is None` are the
    ones the LLM couldn't resolve and that the user will need to pick
    in the confirmation UI.

    Does NOT mutate the spec. The pipeline's labware-assignments
    confirmation flow is the sole writer of `resolved_label` +
    `resolved_label_provenance`.
    """

    def __init__(self, config: dict, client=None, model_name: str = DEFAULT_MODEL):
        self.config = config
        self.labware_labels = list(config.get("labware", {}).keys())
        self.client = client
        self.model_name = model_name

    def suggest(self, spec: ProtocolSpec) -> dict:
        """Build a `{description: LabwareMatchSuggestion}` dict for every
        unique labware description that appears in the spec.

        Three-branch dispatch per description:
          - deterministic (capability + type filters leave exactly one
            survivor): no LLM call, suggested_label set with
            confidence 0.95.
          - llm (≥2 survivors): one batched LLM call resolves the
            ambiguity, choosing among the narrowed survivors. The LLM
            cannot pick outside the survivor list (its return is
            re-validated against it).
          - namespace_split / unresolvable (0 survivors): no LLM call;
            suggested_label is None, branch tag distinguishes the
            two cases so the UI / detector can decide.

        Pre:    `spec` is a ProtocolSpec post-extraction. Not mutated.
                `self.client` may be None for test fakes; in that case
                the llm-branch falls back to "no suggestion" but the
                deterministic / namespace_split / unresolvable
                branches still work (they never touch the LLM).

        Post:   Returns a dict keyed on each unique description (from
                step source/destination refs, initial_contents, and
                prefilled_labware) — same key set as
                `_collect_descriptions_with_wells(spec)`. Each value
                carries the resolver's pick + provenance + branch tag.

        Side effects: At most one Sonnet call (for the llm-branch
                descriptions), batched. Otherwise no I/O.
        """
        per_desc = self._collect_descriptions_with_wells(spec)
        if not per_desc:
            return {}

        suggestions: dict = {}
        # Collect llm-branch survivors so we can issue a single batched
        # LLM call after all deterministic / namespace / unresolvable
        # branches are recorded.
        llm_branch: dict = {}  # description -> survivors

        for desc, payload in per_desc.items():
            wells = payload["wells"]
            branch, survivors = self._resolve_one(desc, wells)
            if branch == "deterministic":
                label = survivors[0]
                suggestions[desc] = LabwareMatchSuggestion(
                    description=desc,
                    suggested_label=label,
                    positive_reasoning=self._positive_reasoning(
                        desc, label,
                        f"Single capability + type filter survivor for "
                        f"'{desc}' across the wells the spec references.",
                    ),
                    why_not_in_instruction=self._why_not_in_instruction(
                        desc, label),
                    confidence=0.95,
                    candidates=survivors,
                    branch="deterministic",
                )
            elif branch == "llm":
                llm_branch[desc] = survivors
            else:
                # namespace_split / unresolvable — modal / detector decides.
                # Even with no capability survivors, narrow the candidate
                # dropdown semantically: a "tube rack" description should
                # never offer a tiprack option, even when no labware
                # physically fits the wells (the user picks a labware and
                # the shape mismatch is then flagged at assignment-apply
                # time via _build_user_action_provenance).
                semantic_candidates = _type_filter(
                    desc, list(self.labware_labels), self.config,
                )
                suggestions[desc] = LabwareMatchSuggestion(
                    description=desc,
                    suggested_label=None,
                    positive_reasoning=None,
                    why_not_in_instruction=None,
                    confidence=0.0,
                    candidates=(semantic_candidates
                                if semantic_candidates
                                else list(self.labware_labels)),
                    branch=branch,
                )

        if llm_branch:
            resolved = self._llm_resolve_narrowed(llm_branch, per_desc)
            for desc, survivors in llm_branch.items():
                entry = resolved.get(desc)
                if entry is not None:
                    label, llm_reasoning = entry
                    if label in survivors:
                        suggestions[desc] = LabwareMatchSuggestion(
                            description=desc,
                            suggested_label=label,
                            positive_reasoning=self._positive_reasoning(
                                desc, label, llm_reasoning),
                            why_not_in_instruction=self._why_not_in_instruction(
                                desc, label),
                            confidence=0.85,
                            candidates=survivors,
                            branch="llm",
                        )
                        continue
                # LLM returned nothing valid → leave as unresolved with the
                # narrowed survivor list so the user picks from physically
                # valid candidates.
                suggestions[desc] = LabwareMatchSuggestion(
                    description=desc,
                    suggested_label=None,
                    positive_reasoning=None,
                    why_not_in_instruction=None,
                    confidence=0.0,
                    candidates=survivors,
                    branch="llm",
                )
        return suggestions

    def _collect_unique_descriptions(self, spec: ProtocolSpec) -> List[str]:
        """Gather every unique labware description from a spec — step
        source/destination refs, initial_contents rows, and
        prefilled_labware rows. Preserves first-seen order so the
        returned dict from `suggest` has a stable iteration order for
        downstream display + test assertions.
        """
        seen = set()
        ordered = []
        for step in spec.steps:
            for ref in (step.source, step.destination):
                if ref is None or ref.description in seen:
                    continue
                seen.add(ref.description)
                ordered.append(ref.description)
        for wc in spec.initial_contents:
            if wc.labware not in seen:
                seen.add(wc.labware)
                ordered.append(wc.labware)
        for pf in spec.prefilled_labware:
            if pf.labware not in seen:
                seen.add(pf.labware)
                ordered.append(pf.labware)
        return ordered

    def _collect_descriptions_with_wells(self, spec: ProtocolSpec) -> dict:
        """Aggregate per-description well sets + step contexts across the spec.

        Pre:    `spec` is a post-extraction ProtocolSpec. Not mutated.

        Post:   Returns a dict
                  {description: {"wells": set[str],
                                 "contexts": list[str]}}
                covering every unique description found in
                `spec.steps[*].source/destination`,
                `spec.initial_contents[*].labware`, and
                `spec.prefilled_labware[*].labware`. First-seen order
                preserved (so `list(result.keys())` matches
                `_collect_unique_descriptions(spec)`).

                For each description:
                  - `wells` is the union of every well referenced —
                    from `ref.well` (singleton), `ref.wells` (explicit
                    list), `ref.well_range` (parsed via
                    `expand_well_range` from schema_builder), and the
                    `wc.well` field on initial_contents rows.
                    `prefilled_labware` rows contribute no wells
                    (they're labware-level metadata).
                  - `contexts` is a list of short strings describing
                    each step the description appears in (e.g.
                    "Step 2: transfer source", "Step 5: mix
                    destination"). Order matches step.order ascending,
                    initial_contents appended last as
                    "initial contents".

        Side effects: None. Pure aggregation. Uses `expand_well_range`
                      from `nl2protocol.extraction.schema_builder`.
        """
        from nl2protocol.extraction.schema_builder import expand_well_range
        result: dict = {}

        def _ensure(desc):
            if desc not in result:
                result[desc] = {"wells": set(), "contexts": []}
            return result[desc]

        def _wells_of(ref) -> set:
            ws: set = set()
            if ref.well:
                ws.add(ref.well)
            if ref.wells:
                ws.update(ref.wells)
            if ref.well_range:
                ws.update(expand_well_range(ref.well_range))
            return ws

        for step in spec.steps:
            for ref, role in [(step.source, "source"),
                              (step.destination, "destination")]:
                if ref is None:
                    continue
                entry = _ensure(ref.description)
                entry["wells"].update(_wells_of(ref))
                entry["contexts"].append(f"Step {step.order}: {step.action} {role}")

        for wc in spec.initial_contents:
            entry = _ensure(wc.labware)
            if wc.well:
                entry["wells"].add(wc.well)
            entry["contexts"].append("initial contents")

        for pf in spec.prefilled_labware:
            entry = _ensure(pf.labware)
            entry["contexts"].append("prefilled labware")

        return result

    def _resolve_one(self, description: str, wells: set) -> tuple:
        """Decide the resolution branch for ONE description.

        Pre:    `description` is the user wording. `wells` is the
                aggregated well set for that description (from
                `_collect_descriptions_with_wells`). `self.config` is
                already loaded.

        Post:   Returns `(branch, survivors)` where `branch` is one of
                {"deterministic","llm","namespace_split","unresolvable"}
                and `survivors` is a list of config labware keys:
                  - len==1 AND branch=="deterministic"  → unambiguous
                    match (single candidate survived capability + type
                    filters).
                  - len>=2 AND branch=="llm"            → LLM must
                    disambiguate among physically valid candidates.
                  - len==0 AND branch=="namespace_split" → 0 survivors
                    AND `_has_namespace_split` detected a clean
                    partition (caller routes to NamespaceSplitDetector).
                  - len==0 AND branch=="unresolvable"   → 0 survivors
                    AND no clean partition; caller surfaces full
                    candidate list for manual pick.

        Side effects: None. Calls `_capability_filter`, `_type_filter`,
                      `_has_namespace_split`. No LLM call.
        """
        survivors = _type_filter(description,
                                  _capability_filter(wells, self.config),
                                  self.config)
        if len(survivors) == 1:
            return ("deterministic", survivors)
        if len(survivors) >= 2:
            return ("llm", survivors)
        # 0 survivors
        if _has_namespace_split(description, wells, self.config) is not None:
            return ("namespace_split", [])
        return ("unresolvable", [])

    def _positive_reasoning(self, description: str, label: str,
                            reasoning: Optional[str] = None) -> str:
        """The 'why is this the right pick?' sentence for a resolver
        suggestion. Stamped into the Provenance iff the user accepts
        the suggestion in the confirmation step.

        Pre:    `description` is the user-language labware reference;
                `label` is a valid config labware key the resolver
                picked; `reasoning`, when truthy after strip(), is the
                LLM's concrete justification (e.g. naming a specific
                load_name match or single-candidate domain fit).

        Post:   Returns a string of the form
                "'{description}' → '{label}'. <body>"
                where <body> is the LLM's reasoning when supplied, or
                an honest "reasoning was not surfaced — review and
                confirm or override" fallback when it isn't. The
                description→label mapping prefix is preserved in
                both branches so downstream consumers (modal display
                + stamped Provenance) keep structural context. The
                fallback intentionally avoids the previous template's
                false claim of having reasoned "based on context."

                The full Opentrons load_name remains in
                self.config["labware"][label] for audit purposes but
                is no longer surfaced in the modal text — at 50+ chars
                it read as noise to reviewers.

        Side effects: None.
        """
        if reasoning and reasoning.strip():
            return f"'{description}' \u2192 '{label}'. {reasoning.strip()}"
        return (
            f"'{description}' \u2192 '{label}'. "
            f"Reasoning was not surfaced by the resolver \u2014 "
            f"review the candidates and confirm or override."
        )

    def _why_not_in_instruction(self, description: str, label: str) -> str:
        """The 'why isn't this in the instruction?' sentence — names the
        description-vs-config-key gap so the reviewer can verify the
        translation is genuinely necessary."""
        return (
            f"The user wrote '{description}' rather than the config key "
            f"'{label}' literally — natural-language vs config-key naming "
            f"gap is expected and requires resolution."
        )

    @staticmethod
    def _parse_assignment(value) -> Tuple[Optional[str], Optional[str]]:
        """Normalize one LLM-returned assignment value into a
        (label, reasoning) pair.

        Pre:    `value` is whatever the LLM emitted inside
                `assignments[description]`. New shape: a dict
                {"label": ..., "reasoning": ...}. Legacy shape:
                a bare string label, or null.

        Post:   Returns (label, reasoning) where:
                  - new dict shape → (value["label"], value["reasoning"]).
                    Either field may be None.
                  - legacy string shape → (value, None). The fallback
                    reasoning is left for `_positive_reasoning` to
                    fill so callers can distinguish "LLM gave reasoning"
                    from "LLM did not."
                  - null or any other shape → (None, None).

        Side effects: None.
        """
        if isinstance(value, str):
            stripped = value.strip()
            return (stripped if stripped else None), None
        if isinstance(value, dict):
            # Defensive normalization: an LLM may return label / reasoning
            # as a non-string (list, int, dict). Without these guards the
            # downstream `label in valid_labels` check (unhashable types)
            # or `reasoning.strip()` (no .strip on int) crashes the
            # parse, dropping ALL descriptions to the empty fallback
            # (CodeRabbit P1).
            raw_label = value.get("label")
            label = (raw_label.strip()
                     if isinstance(raw_label, str) and raw_label.strip()
                     else None)
            raw_reasoning = value.get("reasoning")
            reasoning = (raw_reasoning.strip()
                         if isinstance(raw_reasoning, str) and raw_reasoning.strip()
                         else None)
            return label, reasoning
        return None, None

    def _llm_resolve(self, descriptions: List[str],
                     spec: ProtocolSpec) -> dict:
        """Single LLM call to resolve all labware descriptions to config labels.

        Pre:    `descriptions` is the list of unique labware descriptions
                to resolve. `spec` is the extracted spec; only its
                step-context data is read (action, role, wells, substance).
                Provenance objects are NOT used by the prompt.
        Post:   Returns `{description: (config_label, reasoning_or_None)}`
                for SUCCESSFUL picks only — entries where the LLM
                returned null OR returned a label that doesn't exist in
                `self.config["labware"]` are filtered out. Reasoning is
                the LLM's concrete justification when present, or None
                when the LLM returned legacy string-only shape. Caller
                fills in `None` for descriptions absent from this dict.
        """
        if not self.client:
            return {}

        desc_context = {}
        for step in spec.steps:
            for ref, role in [(step.source, "source"), (step.destination, "destination")]:
                if ref and ref.description in descriptions:
                    if ref.description not in desc_context:
                        desc_context[ref.description] = []
                    wells = ref.well or ref.wells or ref.well_range or "unspecified"
                    desc_context[ref.description].append(
                        f"Step {step.order}: {step.action}, role={role}, "
                        f"wells={wells}, substance={step.substance.value if step.substance else 'unspecified'}"
                    )

        config_summary = {}
        for label, lw in self.config.get("labware", {}).items():
            config_summary[label] = {
                "load_name": lw.get("load_name", "unknown"),
                "slot": lw.get("slot", "unknown"),
            }

        prompt = (
            "You are resolving labware references in a lab protocol.\n\n"
            "The user described labware using natural language. Match each description "
            "to a config label based on domain knowledge and context "
            "(what action, what wells, what substance, what kind of labware it is).\n\n"
            f"CONFIG LABWARE:\n{json.dumps(config_summary, indent=2)}\n\n"
            "LABWARE REFERENCES TO RESOLVE:\n"
        )
        for desc in descriptions:
            contexts = desc_context.get(desc, [])
            prompt += f'\n  "{desc}":\n'
            if contexts:
                for ctx in contexts:
                    prompt += f"    - {ctx}\n"
            else:
                prompt += "    - (referenced in initial contents)\n"

        prompt += (
            "\nFor each description, respond with JSON only:\n"
            '{\n'
            '  "assignments": {\n'
            '    "<description>": {\n'
            '      "label": "<config_label or null>",\n'
            '      "reasoning": "<one or two short sentences naming the SPECIFIC signal that drove the pick>"\n'
            '    }\n'
            '  }\n'
            '}\n\n'
            "Reasoning rules (these are load-bearing — a vague reason is worse than no reason):\n"
            "- Name the specific signal that decided the pick. Examples of GOOD reasoning:\n"
            '    "Only labware in config with load_name containing tuberack; tiprack_20 and tiprack_300 are tip racks per their load_names."\n'
            '    "Step uses wells A1-D4 (24-well grid) — reagent_rack is the only candidate with that capacity."\n'
            '    "User wrote \'microplate\'; wellplate_96 is the only 96-well container in config (load_name corning_96_wellplate)."\n'
            "- Examples of BAD reasoning (do NOT write these — they describe nothing):\n"
            '    "Based on description text and step context."\n'
            '    "It matches the description."\n'
            '    "Inferred from the wells used in the step."\n'
            "- Reference concrete config keys, load_names, well coordinates, or substance names when relevant.\n"
            "- For null labels, briefly state why no config label is a reasonable match.\n"
            "\n"
            "Matching rules:\n"
            "- Match based on domain knowledge: 'Eppendorf tubes' = tube rack, "
            "'trough' = reservoir, 'microplate' = wellplate, etc.\n"
            "- Use the step context to disambiguate: sources are typically racks/reservoirs, "
            "destinations are typically plates.\n"
            "- Each config label can be assigned to multiple descriptions if appropriate "
            "(e.g., source and destination on the same plate).\n"
        )

        return self._call_llm_and_parse(prompt, self.config.get("labware", {}))

    def _llm_resolve_narrowed(self, narrowed: dict, per_desc: dict) -> dict:
        """Variant of `_llm_resolve` that asks the LLM to pick AMONG a
        pre-filtered candidate list per description rather than from the
        full config.

        Pre:    `narrowed` is `{description: list[candidate_label]}` —
                each candidate has already passed capability + type
                filters, so they're all physically valid. List has ≥2
                entries (single-candidate cases bypass the LLM).
                `per_desc` is `{description: {wells, contexts}}` from
                `_collect_descriptions_with_wells` so the prompt can
                carry the same per-step context.

        Post:   Returns `{description: (label, reasoning)}` only for
                descriptions whose LLM pick is one of that description's
                narrowed candidates. Validation is strict: a label
                outside the narrowed list is dropped from the result —
                the caller falls back to "no suggestion, pick from
                survivors" for those descriptions.

        Side effects: One Sonnet call.
        """
        if not self.client or not narrowed:
            return {}
        prompt = (
            "You are picking ONE labware label per description from a "
            "pre-filtered list of candidates that already match the wells "
            "the user referenced AND the semantic labware category implied "
            "by their wording. Your job is to disambiguate among these "
            "physically valid candidates using the step context.\n\n"
            "LABWARE REFERENCES TO RESOLVE:\n"
        )
        for desc, candidates in narrowed.items():
            payload = per_desc.get(desc, {})
            contexts = payload.get("contexts", [])
            wells = sorted(payload.get("wells", set()))
            prompt += f'\n  "{desc}":\n'
            prompt += f"    candidates: {candidates}\n"
            if wells:
                prompt += f"    wells referenced: {wells}\n"
            for ctx in contexts:
                prompt += f"    - {ctx}\n"
        prompt += (
            "\nFor each description, respond with JSON only:\n"
            '{\n'
            '  "assignments": {\n'
            '    "<description>": {\n'
            '      "label": "<one of the listed candidates>",\n'
            '      "reasoning": "<one or two short sentences naming the SPECIFIC signal that drove the pick>"\n'
            '    }\n'
            '  }\n'
            '}\n\n'
            "Rules:\n"
            "- The label MUST be one of the listed candidates for that "
            "description. Any other string will be rejected.\n"
            "- Reasoning must name the specific signal (load_name match, "
            "well capacity, substance, step role) that picked this "
            "candidate over the others.\n"
        )
        return self._call_llm_and_parse(
            prompt, self.config.get("labware", {}),
        )

    def _call_llm_and_parse(self, prompt: str, valid_labels: dict) -> dict:
        """Shared LLM-call + JSON-parse + label-validate plumbing for
        both `_llm_resolve` and `_llm_resolve_narrowed`.

        Replaces the previous bare `except: return {}` with typed
        handling so we can distinguish API errors, JSON parse failures,
        and schema mismatches in logs (silent drops were the root cause
        of "modal shows no suggestions for everything" diagnosis pain).
        """
        from nl2protocol.for_cli.spinner import Spinner
        try:
            with Spinner("Resolving labware references..."):
                response = self.client.messages.create(
                    model=self.model_name,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                )
            result_text = response.content[0].text
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0]
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0]
            try:
                result = json.loads(result_text.strip())
            except json.JSONDecodeError as e:
                print(f"  [resolver] LLM returned non-JSON; skipping all "
                      f"descriptions. Error: {e}")
                return {}
            assignments = result.get("assignments", {})
        except Exception as e:
            # Network / SDK / unexpected — log and degrade. Same effect
            # as the old bare except, but the message names the cause.
            print(f"  [resolver] LLM call failed; skipping all "
                  f"descriptions. Error: {type(e).__name__}: {e}")
            return {}
        resolved: dict = {}
        for desc, value in assignments.items():
            label, reasoning = self._parse_assignment(value)
            if label is not None and label in valid_labels:
                resolved[desc] = (label, reasoning)
        return resolved
