"""
resolver.py — Resolves user-language labware descriptions to config labels.

One LLM call maps every unique description that appears in the spec to a
config label (or null when no reasonable match exists). The resolver
returns SUGGESTIONS — it does NOT mutate the spec. The pipeline's
labware-assignments confirmation flow is the sole writer of
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

from nl2protocol.models.spec import LocationRef, ProtocolSpec


@dataclass(frozen=True)
class LabwareSuggestion:
    """The resolver's tentative pick for one labware description.

    Carries the suggested label + the reasoning the resolver constructed
    for the pick, plus the candidate list the user can pick from in the
    confirmation UI. `suggested_label` is None when the resolver could
    not pick (LLM returned null, or returned a label that doesn't exist
    in config).

    Used by `LabwareResolver.suggest()`; consumed by the pipeline's
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


class LabwareResolver:
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

    def __init__(self, config: dict, client=None, model_name: str = "claude-sonnet-4-20250514"):
        self.config = config
        self.labware_labels = list(config.get("labware", {}).keys())
        self.client = client
        self.model_name = model_name

    def suggest(self, spec: ProtocolSpec) -> dict:
        """Build a `{description: LabwareSuggestion}` dict for every
        unique labware description that appears in the spec.

        Pre:    `spec` is a ProtocolSpec post-extraction. The spec is
                NOT mutated by this call. `self.client` may be None for
                test fakes; in that case the LLM resolution short-circuits
                to an empty `{}` and every description's
                `LabwareSuggestion.suggested_label` is None.

        Post:   Returns a dict keyed on each unique description (from
                step source/destination refs, initial_contents, and
                prefilled_labware). Each value is a `LabwareSuggestion`:
                  * `suggested_label`: the LLM's pick, OR None when
                    unresolvable.
                  * `positive_reasoning` / `why_not_in_instruction`:
                    populated iff `suggested_label is not None`. Both
                    are None for unresolvable descriptions (the user's
                    pick in the confirm step will generate fresh
                    reasoning).
                  * `confidence`: 0.85 for successful picks (matches the
                    legacy hardcoded value), 0.0 for unresolvable.
                  * `candidates`: the full list of valid config labels,
                    so the confirmation UI can populate dropdowns.

        Side effects: One Sonnet call when `self.client` is set and the
                spec carries at least one description. Otherwise no I/O.
        """
        unique_descs = self._collect_unique_descriptions(spec)
        if not unique_descs:
            return {}

        resolved = self._llm_resolve(unique_descs, spec)

        suggestions = {}
        for desc in unique_descs:
            entry = resolved.get(desc)
            if entry is not None:
                label, llm_reasoning = entry
                suggestions[desc] = LabwareSuggestion(
                    description=desc,
                    suggested_label=label,
                    positive_reasoning=self._positive_reasoning(desc, label, llm_reasoning),
                    why_not_in_instruction=self._why_not_in_instruction(desc, label),
                    confidence=0.85,
                    candidates=list(self.labware_labels),
                )
            else:
                suggestions[desc] = LabwareSuggestion(
                    description=desc,
                    suggested_label=None,
                    positive_reasoning=None,
                    why_not_in_instruction=None,
                    confidence=0.0,
                    candidates=list(self.labware_labels),
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
                "'{description}' → '{label}' (load_name '...'). <body>"
                where <body> is the LLM's reasoning when supplied, or
                an honest "reasoning was not surfaced — review and
                confirm or override" fallback when it isn't. The
                description→label mapping prefix is preserved in
                both branches so downstream consumers (modal display
                + stamped Provenance) keep structural context. The
                fallback intentionally avoids the previous template's
                false claim of having reasoned "based on context."

        Side effects: None.
        """
        load_name = self.config.get("labware", {}).get(label, {}).get("load_name", "")
        load_hint = f" (load_name '{load_name}')" if load_name else ""
        if reasoning and reasoning.strip():
            return f"'{description}' \u2192 '{label}'{load_hint}. {reasoning.strip()}"
        return (
            f"'{description}' \u2192 '{label}'{load_hint}. "
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
            return value, None
        if isinstance(value, dict):
            label = value.get("label")
            reasoning = value.get("reasoning")
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
            "- Use null label if NO config label is a reasonable match — do not force a match.\n"
            "- Each config label can be assigned to multiple descriptions if appropriate "
            "(e.g., source and destination on the same plate).\n"
        )

        try:
            from nl2protocol.spinner import Spinner
            with Spinner("Resolving labware references..."):
                response = self.client.messages.create(
                    model=self.model_name,
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}]
                )

            result_text = response.content[0].text
            if "```json" in result_text:
                result_text = result_text.split("```json")[1].split("```")[0]
            elif "```" in result_text:
                result_text = result_text.split("```")[1].split("```")[0]

            result = json.loads(result_text.strip())
            assignments = result.get("assignments", {})

            resolved: dict = {}
            valid_labels = self.config.get("labware", {})
            for desc, value in assignments.items():
                label, reasoning = self._parse_assignment(value)
                if label is not None and label in valid_labels:
                    resolved[desc] = (label, reasoning)
            return resolved
        except Exception:
            return {}
