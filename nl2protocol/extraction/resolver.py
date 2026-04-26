"""
resolver.py — Resolves user-language labware descriptions to config labels.

One LLM call maps all descriptions to config labels using domain knowledge
and step context. Returns tentative assignments for user confirmation.
"""

import json
from typing import List

from nl2protocol.models.spec import ProtocolSpec, LocationRef


class LabwareResolver:
    """Resolves user-language labware descriptions to config labels.

    One LLM call maps all descriptions to config labels using domain knowledge
    and step context. Returns tentative assignments for user confirmation.
    Descriptions with no reasonable config match get null — caught by the
    unresolved LocationRef check in the pipeline.
    """

    def __init__(self, config: dict, client=None, model_name: str = "claude-sonnet-4-20250514"):
        self.config = config
        self.labware_labels = list(config.get("labware", {}).keys())
        self.client = client
        self.model_name = model_name

    def resolve(self, spec: ProtocolSpec) -> ProtocolSpec:
        """Walk all LocationRefs in spec and fill resolved_label via LLM."""
        spec = spec.model_copy(deep=True)

        all_refs = self._collect_refs(spec)
        unique_descs = {ref.description for ref in all_refs}

        # Include prefilled_labware descriptions
        for pf in spec.prefilled_labware:
            unique_descs.add(pf.labware)

        if not unique_descs:
            return spec

        resolved = self._llm_resolve(unique_descs, spec)

        # Apply resolutions
        for ref in all_refs:
            if ref.description in resolved:
                ref.resolved_label = resolved[ref.description]

        for wc in spec.initial_contents:
            if wc.labware in resolved:
                wc.labware = resolved[wc.labware]

        for pf in spec.prefilled_labware:
            if pf.labware in resolved:
                pf.labware = resolved[pf.labware]

        return spec

    def _collect_refs(self, spec: ProtocolSpec) -> List[LocationRef]:
        """Gather all LocationRef objects from spec steps."""
        refs = []
        for step in spec.steps:
            if step.source:
                refs.append(step.source)
            if step.destination:
                refs.append(step.destination)
        return refs

    def _llm_resolve(self, descriptions: set, spec: ProtocolSpec) -> dict:
        """Single LLM call to resolve all labware descriptions to config labels."""
        if not self.client:
            return {}

        # Build step context for each description
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

        # Build config summary
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

            # Only keep non-null assignments that reference valid config labels
            return {
                desc: label
                for desc, label in assignments.items()
                if label is not None and label in self.config.get("labware", {})
            }
        except Exception:
            return {}
