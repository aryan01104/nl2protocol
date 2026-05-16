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
from typing import List, Optional

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
            label = resolved.get(desc)
            if label is not None:
                suggestions[desc] = LabwareSuggestion(
                    description=desc,
                    suggested_label=label,
                    positive_reasoning=self._positive_reasoning(desc, label),
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

    def _positive_reasoning(self, description: str, label: str) -> str:
        """The 'why is this the right pick?' sentence for a resolver
        suggestion. Stamped into the Provenance iff the user accepts
        the suggestion in the confirmation step.
        """
        load_name = self.config.get("labware", {}).get(label, {}).get("load_name", "")
        load_hint = f" (load_name '{load_name}')" if load_name else ""
        return (
            f"User-language description '{description}' resolved to config "
            f"labware '{label}'{load_hint} based on description text + "
            f"step usage context (well names, action role)."
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

    def _llm_resolve(self, descriptions: List[str], spec: ProtocolSpec) -> dict:
        """Single LLM call to resolve all labware descriptions to config labels.

        Pre:    `descriptions` is the list of unique labware descriptions
                to resolve. `spec` is the extracted spec; only its
                step-context data is read (action, role, wells, substance).
                Provenance objects are NOT used by the prompt.
        Post:   Returns `{description: config_label}` for SUCCESSFUL picks
                only — entries where the LLM returned null OR returned a
                label that doesn't exist in `self.config["labware"]` are
                filtered out. Caller fills in `None` for descriptions
                absent from this dict.
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
            '{"assignments": {"<description>": "<config_label or null>", ...}}\n\n'
            "Rules:\n"
            "- Match based on domain knowledge: 'Eppendorf tubes' = tube rack, "
            "'trough' = reservoir, 'microplate' = wellplate, etc.\n"
            "- Use the step context to disambiguate: sources are typically racks/reservoirs, "
            "destinations are typically plates.\n"
            "- Use null if NO config label is a reasonable match — do not force a match.\n"
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

            return {
                desc: label
                for desc, label in assignments.items()
                if label is not None and label in self.config.get("labware", {})
            }
        except Exception:
            return {}
