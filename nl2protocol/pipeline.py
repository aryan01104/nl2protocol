import sys
import json
import os
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic

from .models import ProtocolSchema
from .config import ConfigLoader
from .spec_analysis import infer_source_containers
from .stage_7_post_validation import generate_python_script, simulate_script


LINE_WIDTH = 60  # Same as the ===== separator

from .for_cli import colors as C
from .for_cli.labware_confirm import cli_labware_loop

def _log(msg: str = ""):
    """Print progress/status to stderr (keeps stdout clean for data output)."""
    print(msg, file=sys.stderr)


def _wrap(text: str, indent: str = "  ", width: int = LINE_WIDTH) -> str:
    """Wrap text to fit within LINE_WIDTH, preserving the indent."""
    import textwrap
    return textwrap.fill(text, width=width, initial_indent=indent,
                         subsequent_indent=indent)


# _stage() removed — stage banners now flow from stage_started events
# emitted by stage_block through ConsoleReporter. See ADR-0017 for the
# Observer-pattern completion rationale.


# Phase 3g (Group B): user-facing stage labels for the live-mode indicator.
# Numbering reflects how a user perceives the work, not the internal pipeline
# stage numbers (which include sub-stages like 3.5). Pre-orchestrator
# confirmations collapse into stage 4 ("Confirming with you"); the deterministic
# tail (schema build → Python codegen → simulation) collapses into stage 7
# ("Building & simulating") since they all run together with no user touch.
# Help text for the input-validator's classification verdicts. Looked up
# by stage 1's StageAborted result when validation.classification is one
# of these keys; the wrapper emits each line as an `info` event before
# the stage_failed. Data-driven so the stage body stays focused on the
# work, not on the help-text branching.
CLASSIFICATION_HELP = {
    "QUESTION": [
        "This looks like a question, not a protocol instruction.",
        "Try rephrasing as an action: 'Transfer 100uL from A1 to B1'",
    ],
    "AMBIGUOUS": [
        "This instruction is too vague to generate a protocol.",
        "Try adding specific volumes, wells, and labware names.",
    ],
    "INVALID": [
        "This doesn't appear to be a liquid-handling protocol.",
        "The OT-2 can only pipette — it can't centrifuge, read absorbance, etc.",
    ],
}

# Help text for stage 2 (extraction) when the extractor returns None.
EXTRACTION_FAILURE_HELP = [
    "Could not extract a protocol from your instruction.",
    "This can happen if:",
    "  - The instruction is too vague (add volumes, wells, labware names)",
    "  - The API key is invalid or out of credits",
    "  - The instruction isn't about liquid handling",
    "Try: 'Transfer 100uL from source_plate A1 to dest_plate B1'",
]


def _format_reasoning_lines(reasoning: str, width: int = LINE_WIDTH) -> list:
    """Split spec.reasoning into wrapped info lines for the CLI summary.

    Pre:    `reasoning` is the LLM's freeform explanation, sometimes
            numbered ("1. ...\n2. ..."), sometimes containing dash-
            bullets ("- ..."). May be empty or whitespace-only.
    Post:   Returns a list of pre-wrapped lines ready to emit one-by-one
            as `info` events. Numbered items become one wrapped block
            indented to 4 cols; dash-bullets nest at 6 cols. Empty
            input → empty list (no events emitted upstream).
    Side effects: None.
    """
    import re as _re
    out: list = []
    text = (reasoning or "").strip()
    if not text:
        return out
    parts = _re.split(r'(?=\d+\.\s)', text)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        sub_parts = _re.split(r'(?=\s*-\s)', part, maxsplit=0)
        for i, sub in enumerate(sub_parts):
            sub = sub.strip()
            if not sub:
                continue
            if i == 0:
                out.append(_wrap(sub, indent="  ", width=width))
            else:
                out.append(_wrap(f"- {sub.lstrip('- ')}", indent="    ", width=width))
    return out

PIPELINE_STAGE_TOTAL = 7
PIPELINE_STAGES = [
    (1, "Validating input"),
    (2, "Extracting protocol"),
    (3, "Resolving labware"),
    (4, "Confirming with you"),
    (5, "Resolving gaps"),
    (6, "Checking hardware"),
    (7, "Building & simulating"),
]


@dataclass
class PipelineResult:
    """Result from a successful protocol generation pipeline run."""
    script: str
    simulation_log: str
    protocol_schema: ProtocolSchema
    runlog: list
    config: dict


class ProtocolAgent:
    def __init__(self, api_key: str,
                 config_path: str = "lab_config.json",
                 confirmation_manager=None,
                 reporter=None,
                 confirmation_handler=None,
                 assignments_handler=None,
                 binary_confirm_handler=None,
                 initial_contents_handler=None,
                 namespace_split_handler=None):
        """
        Args:
            config_path: path to lab_config.json
            confirmation_manager: optional ConfirmationManager (see
                nl2protocol.confirmation). Defaults to InteractiveCM, which
                preserves the existing CLI behavior. Tests and evals can pass
                ScriptedCM(answers) or AutoConfirmCM() to drive the pipeline
                non-interactively.
            reporter: optional Reporter (see nl2protocol.reporting). Defaults
                to ConsoleReporter, which preserves the existing CLI banners.
                Tests pass CapturingReporter to inspect the structured event
                stream without I/O. Future HTML/TUI sinks plug in here.
            confirmation_handler: optional ConfirmationHandler for the
                gap-resolution orchestrator (see nl2protocol.gap_resolution).
                Defaults to None — `run_pipeline` then constructs a
                CLIConfirmationHandler that drives stdin via `cm`. Live mode
                (ADR-0013 Phase 3c) injects an HTMLConfirmationHandler that
                blocks the worker thread until the browser posts a response
                over the WebSocket.
            assignments_handler: optional handler with a
                `confirm(table) -> Optional[dict]` method for labware
                description→label confirmation. Defaults to None — the
                CLI loop in `_confirm_labware_assignments` runs as before.
                Live mode (ADR-0013 Phase 3d) injects an
                HTMLAssignmentsHandler that pops a center modal in the
                browser and blocks the worker thread until the user
                confirms or aborts.
            binary_confirm_handler: optional handler with a
                `confirm(title, items, yes_label, no_label) -> bool`
                method used for stand-alone Y/N prompts (inferred-source
                acknowledgement, hardware-error proceed). Defaults to
                None — pipeline falls back to the existing TTY-gated
                `cm.prompt`. Live mode (ADR-0013 Phase 3e) injects an
                HTMLBinaryConfirmHandler that pops a small popup in the
                browser.
            initial_contents_handler: optional handler with a
                `confirm(table) -> Optional[list]` method for batched
                initial-contents volume confirmation. Defaults to None —
                IC volume gaps then route through the orchestrator's
                per-Gap flow as before. Live mode (ADR-0013 Phase 3f)
                injects an HTMLInitialContentsHandler that pops a center
                modal with one row per IC entry, lets the user accept
                or edit each volume in a single batch.
        """
        from nl2protocol.for_cli.confirmation import InteractiveCM
        from nl2protocol.reporting import ConsoleReporter
        self.config_path = config_path
        self._api_key = api_key
        self.config_loader = ConfigLoader(api_key=api_key, config_path=config_path)
        self.cm = confirmation_manager or InteractiveCM()
        self.reporter = reporter or ConsoleReporter()
        self.confirmation_handler = confirmation_handler
        self.assignments_handler = assignments_handler
        self.binary_confirm_handler = binary_confirm_handler
        self.initial_contents_handler = initial_contents_handler
        # Phase 5 capability matcher: handler for "A1-A6 / B1 / C1-C7"-
        # style multi-rack descriptions. Fires before the labware-
        # assignments confirm so the partition is settled first.
        self.namespace_split_handler = namespace_split_handler

    def _run_stage(self, number: int, name: str, fn) -> bool:
        """Run a stage closure inside a stage_block and translate its
        StageResult into events.

        Pre:    `number` is 1..PIPELINE_STAGE_TOTAL; `name` is the
                short user-facing label (matches PIPELINE_STAGES[number-1]).
                `fn` is a zero-arg or one-arg callable returning either a
                `StageOk` or a `StageAborted`. If `fn` accepts one arg it
                receives the `stage_block` so it can call `.progress(msg)`
                for sub-action narration.
        Post:   Opens a stage_block; calls `fn`; translates its result:
                  - StageOk(summary)        → s.success(summary), return True
                  - StageAborted(reason,    → emit each help line as info,
                       help, suggestion)      then s.fail(reason), return False
                  - Uncaught exception      → s.error(str(e)), return False;
                                              exception is re-raised by
                                              stage_block's __exit__ — caller
                                              never sees the False in that case.
                Caller treats False as "return None from run_pipeline".
        """
        from .reporting import stage_block, StageOk, StageAborted
        import inspect
        with stage_block(self.reporter, number,
                         total=PIPELINE_STAGE_TOTAL, name=name) as s:
            # Support both zero-arg and one-arg stage functions. Stages
            # that need to emit progress mid-flight take `s`; pure
            # compute stages take nothing.
            sig = inspect.signature(fn)
            result = fn(s) if len(sig.parameters) == 1 else fn()
            if isinstance(result, StageAborted):
                for line in result.help:
                    s.info(line)
                if result.suggestion:
                    s.info(f"Suggestion: {result.suggestion}")
                s.fail(result.reason)
                return False
            if isinstance(result, StageOk):
                if result.summary:
                    s.success(result.summary)
                return True
            # Unknown return shape — coerce to success silently to avoid
            # blocking the pipeline on a stage we haven't migrated yet.
            return True

    def _confirm_initial_contents_via_handler(self, spec, suggesters,
                                                  context) -> Optional[bool]:
        """Browser-bridged variant of the orchestrator's per-Gap
        initial-contents volume flow. Builds one table row per IC entry,
        runs the existing suggester registry to pre-fill suggested
        volumes (mirrors the orchestrator's "first non-None suggestion
        wins" pattern), pops a single batch modal, applies confirmed
        volumes back onto spec.initial_contents.

        Pre:    `self.initial_contents_handler` is non-None (caller
                checks). `spec.initial_contents` is reachable. `suggesters`
                is the same registry the orchestrator would use.
                `context` carries the instruction + config the suggesters
                read.
        Post:   Returns True on confirm (volumes applied — spec mutated
                in place), False on abort. Returns True with no-op when
                there are no IC entries (nothing to confirm).
                On confirm, every IC row's `volume_ul` is set from the
                browser response (user-edited or accepted suggestion);
                rows the browser response doesn't cover are left untouched.

        Side effects:
          - mutates spec.initial_contents[*].volume_ul
          - blocks the calling thread until the browser submits
        """
        ic_list = list(getattr(spec, "initial_contents", []) or [])
        if not ic_list:
            return True
        # Build the table. For null/zero volumes, run suggesters via
        # the same first-non-None pattern the orchestrator uses. We
        # synthesize a Gap per row so suggesters get the shape they
        # expect; the synthetic gap doesn't go anywhere else.
        from nl2protocol.gap_resolution.targets import InitialVolume
        from nl2protocol.gap_resolution.types import Gap
        table = []
        for idx, ic in enumerate(ic_list):
            current = getattr(ic, "volume_ul", None)
            suggested = None
            # Capture the winning Suggestion so we can pass its reasoning
            # through to the modal. Stays None for user-stated rows
            # (current is non-null) — the UI suppresses the hint there
            # because the value IS the user's word.
            winning_sug = None
            if current is None:
                synthetic_gap = Gap(
                    id=f"ic{idx}",
                    step_order=None,
                    kind="missing",
                    current_value=None,
                    description=f"volume for {ic.labware} well {ic.well}",
                    severity="blocker",
                    metadata={},
                    targets=[InitialVolume(well_idx=idx)],
                )
                for s in suggesters:
                    try:
                        candidate = s.suggest(synthetic_gap, spec, context)
                    except Exception:
                        candidate = None
                    if candidate is not None:
                        winning_sug = candidate
                        try:
                            suggested = float(candidate.value)
                        except (TypeError, ValueError):
                            suggested = None
                        break
            # CARRY-B1: for user-stated rows (current is non-null), pull
            # the verbatim cite from volume_ul_provenance so the modal can
            # render a per-row audit trail like "from your instruction:
            # '50uL DNA'". Stays None when the LLM didn't populate the
            # provenance (legacy specs, or when the volume itself was null).
            user_cited_text = None
            if current is not None:
                vol_prov = getattr(ic, "volume_ul_provenance", None)
                if vol_prov is not None and getattr(vol_prov, "source", None) == "instruction":
                    cited = getattr(vol_prov, "cited_text", None)
                    if cited:
                        user_cited_text = (
                            " | ".join(cited) if isinstance(cited, list) else str(cited)
                        )
            table.append({
                "labware": getattr(ic, "labware", ""),
                "well": getattr(ic, "well", ""),
                "substance": getattr(ic, "substance", ""),
                "current_volume_ul": current,
                "suggested_volume_ul": suggested,
                # Surface the suggester's reasoning into the modal so
                # the user sees WHY a default was picked. Only populated
                # for suggester-filled rows; user-stated rows have these
                # null and the UI renders no per-row hint there.
                "provenance_source": (
                    winning_sug.provenance_source
                    if winning_sug is not None else None
                ),
                "provenance_reasoning": (
                    winning_sug.positive_reasoning
                    if winning_sug is not None else None
                ),
                # CARRY-B1: verbatim instruction substring for user-stated
                # volumes. Null for defaults (the provenance_reasoning above
                # covers those) and for legacy specs where the LLM didn't
                # populate volume_ul_provenance.
                "user_cited_text": user_cited_text,
            })
        confirmed = self.initial_contents_handler.confirm(table)
        if confirmed is None:
            return False
        # Apply by (labware, well) match — robust to ordering changes.
        by_key = {(r.get("labware"), r.get("well")): r.get("volume_ul")
                  for r in confirmed}
        from nl2protocol.models.spec import push_revision
        for ic in spec.initial_contents:
            key = (getattr(ic, "labware", None), getattr(ic, "well", None))
            if key in by_key and by_key[key] is not None:
                try:
                    new_vol = float(by_key[key])
                except (TypeError, ValueError):
                    continue
                push_revision(ic, volume_ul=new_vol)
        return True

    def _maybe_handle_namespace_split(self, spec) -> Optional[bool]:
        """Detect + resolve namespace-split gaps before the assignments
        confirm runs.

        Pre:    `spec` is post-extraction, pre-assignment. The handler
                may be None (CLI mode) — then this is a no-op and
                returns None.

        Post:   When the handler exists AND
                `NamespaceSplitDetector` emits ≥1 gap with
                `subkind="namespace_split"`:
                  - Builds the modal payload from the gap metadata
                    (one row per prefix group + per-row candidates).
                  - Calls `self.namespace_split_handler.confirm(...)`.
                  - On user abort (handler returns None): returns False
                    so the caller can save state and halt the run.
                  - On user confirm: rewrites every LocationRef whose
                    description matches the namespace-split's
                    `description` to a per-group description tagged
                    with the prefix (e.g. "tube rack [A]"). Subsequent
                    resolver passes treat each tagged description as a
                    separate labware so the assignments confirm
                    operates on per-rack rows.
                  - Returns True so the caller knows to re-run
                    suggest() against the rewritten spec.
                When no namespace_split gaps exist OR the handler is
                None, returns None (caller treats as "no change").

        Side effects: Mutates `spec.steps[*].source/destination.description`
                      in place for refs touched by an accepted split.
        """
        if self.namespace_split_handler is None:
            return None
        from .gap_resolution import NamespaceSplitDetector, apply_namespace_split
        gaps = NamespaceSplitDetector().detect(
            spec, {"config": self.config_loader.config})
        ns_gaps = [g for g in gaps
                   if (g.metadata or {}).get("subkind") == "namespace_split"]
        if not ns_gaps:
            return None
        # One modal per detected description (typically just one — most
        # protocols have a single ambiguous rack name). Each row carries
        # the prefix, the wells in that subgroup, the candidate labware,
        # and the resolver's first-fit suggestion.
        applied_any = False
        for gap in ns_gaps:
            meta = gap.metadata or {}
            description = meta.get("description", "")
            partition = meta.get("partition", {}) or {}
            pairs = dict(meta.get("candidate_pairs", []) or [])
            payload = []
            all_labels = list(self.config_loader.config.get("labware", {}).keys())
            for prefix in sorted(partition.keys()):
                payload.append({
                    "prefix": prefix,
                    "wells": list(partition[prefix]),
                    "candidates": all_labels,
                    "suggested_label": pairs.get(prefix),
                })
            confirmed = self.namespace_split_handler.confirm(payload)
            if confirmed is None:
                return False
            if not confirmed:
                continue
            apply_namespace_split(spec, description, confirmed)
            applied_any = True
        return applied_any if applied_any else None

    def _confirm_labware_assignments_via_handler(
        self, spec, labware_suggestions: dict,
        reviewer_objections: Optional[dict] = None,
    ) -> Optional[dict]:
        """Browser-bridged variant of `_confirm_labware_assignments`.

        Builds the description→suggested_label table from the resolver's
        suggestions (NOT from spec, which is no longer mutated by the
        resolver), then delegates to `self.assignments_handler.confirm`.

        Pre:    `spec.steps` carries LocationRefs whose `description`
                drives the rows. `labware_suggestions` is the dict
                returned by `LabwareResolver.suggest()`, keyed on
                description with values exposing `.suggested_label` +
                `.candidates`. `reviewer_objections` is optional —
                `{description: objection_text}` from a pre-modal
                reviewer pass (see `_review_labware_suggestions`).
                When present, each objection is prefixed onto the
                row's `positive_reasoning` with a "⚠ Reviewer
                flagged:" tag so it surfaces in the existing
                per-row hint slot in the modal. `self.assignments_handler`
                is non-None (caller checks).
        Post:   Returns dict {description: confirmed_label} or None
                (None means user aborted or browser timed out).
                Empty dict when there were no LocationRefs to confirm
                (handler short-circuits).
        """
        from .extraction.schema_builder import expand_well_range
        config_labels = list(
            self.config_loader.config.get("labware", {}).keys()
        )
        objections = reviewer_objections or {}

        # Fix A: aggregate wells per description across every ref that
        # shares it, so the shape-mismatch check on a row reflects the
        # union of wells the user's labware pick would need to host.
        wells_by_desc: dict = {}
        for step in spec.steps:
            for ref in (step.source, step.destination):
                if ref is None:
                    continue
                wells = wells_by_desc.setdefault(ref.description, set())
                if ref.well:
                    wells.add(ref.well)
                if ref.wells:
                    wells.update(ref.wells)
                if ref.well_range:
                    try:
                        wells.update(expand_well_range(ref.well_range))
                    except Exception:
                        pass

        seen = set()
        table = []
        for step in spec.steps:
            for ref in [step.source, step.destination]:
                if ref is None or ref.description in seen:
                    continue
                seen.add(ref.description)
                suggestion = labware_suggestions.get(ref.description)
                suggested = suggestion.suggested_label if suggestion else None
                candidates = (
                    suggestion.candidates if suggestion is not None
                    else config_labels
                )
                base_reasoning = (
                    suggestion.positive_reasoning
                    if suggestion is not None else None
                )
                # Fix A: stack warnings ABOVE the resolver's reasoning.
                # Shape mismatch (objective physical-fit fact) first,
                # then reviewer objection (LLM judgment), then the
                # resolver's own reasoning labeled "Original reasoning:".
                #
                # Shape mismatch is computed across EVERY candidate, not
                # just the resolver's `suggested` pick, because the modal
                # exists so the user can override the suggestion (and
                # the resolver often returns None when candidates are
                # ambiguous — exactly when the user picks unaided and
                # most needs the warning). Per the existing handover the
                # frontend can't recompute on dropdown change, so we
                # surface every candidate-vs-wells mismatch upfront,
                # grouped by shape signature to dedupe shared racks.
                shape_warnings = self._shape_mismatch_warnings(
                    candidates, wells_by_desc.get(ref.description),
                )
                objection = objections.get(ref.description)
                parts = list(shape_warnings)
                if objection:
                    parts.append(f"⚠ Reviewer flagged: {objection}")
                if base_reasoning:
                    label = "Original reasoning: " if parts else ""
                    parts.append(f"{label}{base_reasoning}")
                reasoning = "\n\n".join(parts) if parts else None
                table.append({
                    "description": ref.description,
                    "suggested_label": suggested,
                    "candidates": candidates,
                    # Carry the resolver's reasoning (optionally prefixed
                    # with shape-mismatch and/or reviewer-objection
                    # warnings) so the modal can surface it inline per
                    # row. None when the resolver had no suggestion AND
                    # nothing flagged.
                    "positive_reasoning": reasoning,
                })
        return self.assignments_handler.confirm(table)

    def _review_labware_suggestions(
        self, labware_suggestions: dict, instruction: str, extractor,
    ) -> dict:
        """Run the IndependentReviewSuggester against the labware
        suggestions BEFORE the assignments modal opens. Returns
        `{description: objection_text}` for every suggestion the
        reviewer disagreed with. Failures degrade silently to `{}`
        — a missed pre-modal review is no worse than today's
        behavior where the reviewer fires later inside the
        orchestrator loop.

        Pre:    `labware_suggestions` is the dict from
                `LabwareMatcher.suggest()`. `instruction` is the
                user's free-text protocol description. `extractor`
                supplies `client` + `model_name` for the LLM call
                (uses the same metered client as everything else).
        Post:   Returns dict {description: objection_text}. Empty
                when no suggestions to review, all agreed, the LLM
                call failed, or the client is None (test-mode).
        Side effects: At most one LLM call. The metered client tracks
                      it like any other.
        """
        if not labware_suggestions or extractor is None \
                or extractor.client is None:
            return {}
        from nl2protocol.gap_resolution.suggesters import (
            IndependentReviewSuggester,
        )
        reviewer = IndependentReviewSuggester(
            client=extractor.client, model_name=extractor.model_name,
        )
        return reviewer.review_suggestions(
            labware_suggestions, {"instruction": instruction or ""},
        )

    def _resolve_description_gaps_pre_pass(
        self, spec, ic_suggesters, prompt: str, extractor,
        reviewer_model: str,
    ) -> bool:
        """Run a scoped gap-resolution pass over source/destination
        descriptions BEFORE labware matching. Resolves missing-description
        and fabricated-citation gaps so labware picks land against
        finalized descriptions instead of strings the user later rewrites
        inside the main orchestrator loop (silent staleness, Fix E).

        Pre:    `spec` is the freshly-extracted ProtocolSpec. `ic_suggesters`
                is the suggester registry shared with the IC batch and
                main orchestrator (includes LLMSpotSuggester, which can
                propose new descriptions). `extractor` supplies the LLM
                client + model name. `reviewer_model` is the smaller model
                used by the IndependentReviewSuggester.
        Post:   Returns True iff the pass either found nothing to fix or
                resolved everything it found; False iff the user aborted.
                Mutates `spec` in place via the orchestrator's apply path.
        Side effects: At most one or two LLM calls per detected gap
                      (suggester + reviewer). Emits the usual storytelling
                      events under stage_3_gap_resolver.
        """
        from .gap_resolution import (
            Orchestrator, CLIConfirmationHandler, default_apply_resolution,
            MissingFieldsDetector, ProvenanceWarningDetector,
            IndependentReviewSuggester,
        )

        def _is_description_gap(gap) -> bool:
            return (
                gap.field_path.endswith(".description")
                or gap.field_path.endswith(".description_provenance")
            )

        orch = Orchestrator(
            detectors=[
                MissingFieldsDetector(),
                ProvenanceWarningDetector(),
            ],
            suggesters=ic_suggesters,
            reviewer=IndependentReviewSuggester(
                client=extractor.client,
                model_name=reviewer_model,
            ) if extractor is not None and extractor.client is not None else None,
            handler=(
                self.confirmation_handler
                or CLIConfirmationHandler(cm=self.cm, log=_log)
            ),
            apply_resolution=default_apply_resolution,
            reporter=self.reporter,
        )
        outcome = orch.run(
            spec,
            context={
                "instruction": prompt,
                "config": self.config_loader.config,
            },
            gap_filter=_is_description_gap,
        )
        if outcome.aborted:
            return False
        return True

    def _apply_labware_assignments(
        self, spec, labware_suggestions: dict, confirmed: dict,
    ) -> None:
        """Write user-confirmed labware mappings into the spec with
        truthful provenance.

        Pre:    `spec` is the post-extraction ProtocolSpec (not yet
                mutated by labware resolution). `labware_suggestions`
                is the resolver's `{description: LabwareSuggestion}`
                dict — drives the "user accepted vs overrode" decision.
                `confirmed` is `{description: label}` from the user
                (or, in headless mode, an auto-build of the resolver's
                non-null suggestions).

        Post:   For every LocationRef whose `description` appears in
                `confirmed` with a non-null label:
                  * `ref.resolved_label = confirmed[description]`
                  * `ref.resolved_label_provenance = Provenance(...)`
                    with `review_status="user_accepted_suggestion"` iff
                    the picked label matches the resolver's suggestion;
                    `review_status="user_edited"` otherwise.
                  * `initial_contents` and `prefilled_labware` rows
                    referring to the same user-language description get
                    their `labware` string rewritten to the confirmed
                    config label (so the downstream well-state tracker
                    and constraint checker see config-canonical names).

                Refs whose description isn't in `confirmed` (or whose
                picked label is null) are left untouched — `resolved_label`
                stays `None`, the orchestrator's `LabwareAmbiguityDetector`
                will surface them.

        Side effects: Mutates `spec` in place (writes resolved_label,
                resolved_label_provenance, and rewrites initial_contents
                / prefilled_labware labware strings).
        """
        if not confirmed:
            return
        from nl2protocol.models.spec import push_revision
        from nl2protocol.extraction.schema_builder import expand_well_range
        for step in spec.steps:
            for ref in (step.source, step.destination):
                if ref is None:
                    continue
                if ref.description not in confirmed:
                    continue
                label = confirmed[ref.description]
                if label is None:
                    continue
                suggestion = labware_suggestions.get(ref.description)
                # Per-ref wells for the shape-mismatch check in
                # _build_user_action_provenance. Same shape-aggregation
                # logic as constraints._wells_of_ref.
                ref_wells: set = set()
                if ref.well:
                    ref_wells.add(ref.well)
                if ref.wells:
                    ref_wells.update(ref.wells)
                if ref.well_range:
                    ref_wells.update(expand_well_range(ref.well_range))
                # One logical write per LocationRef: snapshot the pre-
                # assignment state, then update both resolved_label and
                # resolved_label_provenance on the head.
                push_revision(
                    ref,
                    resolved_label=label,
                    resolved_label_provenance=self._build_user_action_provenance(
                        description=ref.description,
                        label=label,
                        suggestion=suggestion,
                        ref_wells=ref_wells,
                    ),
                )
        # initial_contents and prefilled_labware store labware as plain
        # strings (no LocationRef, no provenance slot). Rewrite them to
        # the confirmed config label so downstream stages see
        # config-canonical names. Snapshot each row before remapping so
        # the user-facing description (pre-assignment) is preserved in
        # prior_revisions.
        for wc in spec.initial_contents:
            if wc.labware in confirmed and confirmed[wc.labware] is not None:
                push_revision(wc, labware=confirmed[wc.labware])
        for pf in spec.prefilled_labware:
            if pf.labware in confirmed and confirmed[pf.labware] is not None:
                push_revision(pf, labware=confirmed[pf.labware])

    def _build_user_action_provenance(
        self, description: str, label: str, suggestion=None,
        ref_wells: Optional[set] = None,
    ):
        """Build the Provenance stamped onto a confirmed labware
        mapping. `review_status` reflects user action: accepted the
        resolver's suggestion as-is vs overrode it (or picked when the
        resolver had no suggestion).

        Pre:    `description` is the user's labware wording. `label` is
                the config key the user confirmed. `suggestion` is the
                matching `LabwareSuggestion` from the resolver (or None
                if the resolver returned nothing for this description).
                `ref_wells` is the set of wells referenced by THIS
                LocationRef (not aggregated across the spec). Used to
                detect a shape mismatch between the user's pick and the
                wells they expect to touch.

        Post:   Returns a `Provenance` with:
                  * source="inferred" (labware mapping is never
                    literally in the instruction)
                  * review_status="user_accepted_suggestion" iff
                    suggestion is not None AND
                    suggestion.suggested_label == label;
                    "user_edited" otherwise
                  * positive_reasoning / why_not_in_instruction:
                    the resolver's own reasoning when the user accepted,
                    fresh override-reasoning when the user picked a
                    different label (or the resolver had no pick).
                    When `ref_wells` contains any well outside the
                    picked labware's `valid_wells`, a "[shape mismatch]"
                    note naming the offending wells + the labware's
                    valid range is APPENDED to positive_reasoning so
                    the warning surfaces wherever positive_reasoning
                    is rendered (modal + report).
                  * confidence: 0.85 on accept (matches the resolver's
                    self-assessment), 1.0 on user override (user is
                    authoritative). When a shape mismatch is detected,
                    confidence is clamped at min(existing, 0.5) — the
                    physical impossibility downgrades trust regardless
                    of user/resolver agreement.
        Side effects: None.
        """
        from nl2protocol.models.spec import Provenance

        accepted = (
            suggestion is not None
            and suggestion.suggested_label == label
            and suggestion.positive_reasoning is not None
        )
        if accepted:
            base_positive = suggestion.positive_reasoning
            base_why = suggestion.why_not_in_instruction
            base_conf = suggestion.confidence
            base_status = "user_accepted_suggestion"
        else:
            suggested = suggestion.suggested_label if suggestion is not None else None
            if suggested is not None:
                base_positive = (
                    f"User picked '{label}' for description '{description}' "
                    f"over the resolver's suggestion of '{suggested}'."
                )
            else:
                base_positive = (
                    f"User picked '{label}' for description '{description}'; "
                    f"resolver had no suggestion."
                )
            base_why = (
                f"Labware label '{label}' is a config key, not an "
                f"instruction phrase."
            )
            base_conf = 1.0
            base_status = "user_edited"

        # Shape-mismatch warning: if the user-picked labware can't
        # physically host every well referenced by this ref, append a
        # warning into positive_reasoning so the modal + report surface
        # it inline. Constraint check still emits its own WELL_INVALID
        # error downstream; this just makes the provenance honest about
        # the mismatch at the moment of decision.
        mismatch_note = self._shape_mismatch_note(label, ref_wells)
        if mismatch_note:
            base_positive = f"{base_positive} {mismatch_note}"
            base_conf = min(base_conf, 0.5)

        return Provenance(
            source="inferred",
            positive_reasoning=base_positive,
            why_not_in_instruction=base_why,
            confidence=base_conf,
            review_status=base_status,
        )

    def _shape_mismatch_warnings(
        self, candidates: Optional[list], ref_wells: Optional[set],
    ) -> list:
        """Build per-shape shape-mismatch warning strings for a row in the
        labware-assignments modal. Surfaces a warning for every candidate
        whose `valid_wells` cannot host the union of wells the row's
        description touches — NOT just the resolver's `suggested` pick —
        so the user gets a warning regardless of which dropdown option
        they choose.

        Pre:    `candidates` is the list of config labware keys offered as
                dropdown options for this row (from the resolver's
                `LabwareSuggestion.candidates`). `ref_wells` is the union
                of wells every ref sharing this description references
                across the spec.
        Post:   Returns a list of `"⚠ Shape mismatch: ..."` strings, one
                per unique (offending-wells, valid-rows, valid-columns)
                signature. Candidates with the same signature are named
                together in one warning so configs whose racks share a
                load_name produce one line instead of N duplicates.
                Returns an empty list when there are no candidates, no
                wells, or every candidate can host every well.
        Side effects: None. Imports `get_well_info` lazily.
        """
        if not candidates or not ref_wells:
            return []
        from nl2protocol.models.labware import get_well_info
        labware_cfg = self.config_loader.config.get("labware", {})
        sig_to_labels: dict = {}
        for cand in candidates:
            lw = labware_cfg.get(cand, {})
            load_name = lw.get("load_name", "")
            if not load_name:
                continue
            try:
                info = get_well_info(load_name)
            except (ValueError, ImportError):
                continue
            valid = set(info.get("valid_wells", []))
            if not valid:
                continue
            offending = tuple(sorted(w for w in ref_wells if w not in valid))
            if not offending:
                continue
            sig = (offending,
                   info.get("row_range", "?"),
                   info.get("col_range", "?"))
            sig_to_labels.setdefault(sig, []).append(cand)
        warnings = []
        for (offending, row_range, col_range), labels in sig_to_labels.items():
            shown = list(offending[:5])
            ellipsis = (
                "" if len(offending) <= 5
                else f" (+{len(offending) - 5} more)"
            )
            labels_str = ", ".join(labels)
            warnings.append(
                f"⚠ Shape mismatch: Wells {shown}{ellipsis} do not exist "
                f"on {labels_str} (valid rows {row_range}, columns "
                f"{col_range})."
            )
        return warnings

    def _shape_mismatch_facts(self, label: str,
                                ref_wells: Optional[set]) -> Optional[str]:
        """Build the just-the-facts shape-mismatch sentence when
        `ref_wells` contains any well outside `label`'s `valid_wells`.

        Pre:    `label` is a config labware key. `ref_wells` is the set
                of wells the ref touches (or None / empty for "no wells
                to check").
        Post:   Returns a single sentence "Wells [...] do not exist on
                '<label>' (valid rows X, columns Y)." with no marker
                prefix and no post-decision context. Callers add their
                own framing (provenance vs modal hint). Returns None
                when there's nothing to warn about (no wells, unknown
                load_name, or wells fully fit). Unknown load_names →
                None (fail-open, matches the constraint checker's
                convention).
        Side effects: None.
        """
        if not ref_wells:
            return None
        lw = self.config_loader.config.get("labware", {}).get(label, {})
        load_name = lw.get("load_name", "")
        if not load_name:
            return None
        try:
            from nl2protocol.models.labware import get_well_info
            info = get_well_info(load_name)
        except (ValueError, ImportError):
            return None
        valid = set(info.get("valid_wells", []))
        if not valid:
            return None
        offending = sorted(w for w in ref_wells if w not in valid)
        if not offending:
            return None
        row_range = info.get("row_range", "?")
        col_range = info.get("col_range", "?")
        # Cap the listed wells to 5 so the modal text stays scannable.
        shown = offending[:5]
        ellipsis = "" if len(offending) <= 5 else f" (+{len(offending) - 5} more)"
        return (
            f"Wells {shown}{ellipsis} do not exist on "
            f"'{label}' (valid rows {row_range}, columns {col_range})."
        )

    def _shape_mismatch_note(self, label: str,
                              ref_wells: Optional[set]) -> Optional[str]:
        """Provenance-side wrapper around `_shape_mismatch_facts`. Used
        AFTER the user has picked, so the tail captures that context.

        Returns "[shape mismatch] <facts> Constraint check will flag
        this downstream; the user picked this labware anyway." or None.
        """
        facts = self._shape_mismatch_facts(label, ref_wells)
        if facts is None:
            return None
        return (
            f"[shape mismatch] {facts} Constraint check will flag this "
            f"downstream; the user picked this labware anyway."
        )

    def run_pipeline(self, prompt: str, csv_path: str = None,
                     full_confirmation: bool = False, confirmation_threshold: float = 0.7,
                     verbose: bool = False) -> Optional[PipelineResult]:
        """Run the protocol generation pipeline.

        New architecture (v2):
          Stage 0: Input validation
          Stage 1: LLM reasons through instruction → ProtocolSpec (one LLM call)
          Stage 2: Hallucination guard + sufficiency check + gap filling
          Stage 3: User confirms the spec
          Stage 4: Deterministic: spec + config → ProtocolSchema (no LLM)
          Stage 5: Deterministic: ProtocolSchema → Python script (no LLM)
          Stage 6: Opentrons simulation
          Stage 7: Intent verification (LLM, optional safety net)

        Only Stage 1 uses an LLM for generation. Stages 4-5 are deterministic,
        so user-specified volumes can never be corrupted.

        Falls back to the old LLM-based generation if the deterministic path fails.
        """
        from datetime import datetime
        from .extraction import SemanticExtractor
        from .extraction.schema_builder import spec_to_schema
        from .reporting import StageEvent

        # Emit raw instruction event for downstream reporters (HTMLReporter etc.)
        self.reporter.emit(StageEvent(
            kind="raw_instruction",
            data={"instruction": prompt},
            stage_name="input",
        ))

        # DEV ONLY — remove before public release.
        # Intermediate state log: accumulates spec snapshots throughout the
        # pipeline, written to disk on completion or failure for inspection.
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)

        state_log = {
            "timestamp": datetime.now().isoformat(),
            "input": {"instruction": prompt},
        }

        def _save_state_log(failed_at: str = None):
            if failed_at:
                state_log["failed_at"] = failed_at
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(output_dir, f"pipeline_state_{ts}.json")
            with open(path, 'w') as f:
                json.dump(state_log, f, indent=2, default=str)
            _log(f"  {C.dim(f'State log: {path}')}")

        try:
            from .reporting import StageOk, StageAborted

            # Stage 1 — the body is the algorithm: load config, classify,
            # decide. Presentation (events, help text, success/error
            # rendering) lives in _run_stage. Branching on the classifier
            # verdict reads from CLASSIFICATION_HELP at module top.
            def stage_1(s):
                s.progress("loading config")
                try:
                    self.config_loader.load_config()
                except Exception as e:
                    _save_state_log("stage_1_config")
                    return StageAborted(reason=f"Config validation failed: {e}")
                state_log["input"]["config"] = self.config_loader.config

                s.progress("classifying instruction (Haiku)")
                from .validation.input_validator import InputValidator
                try:
                    validation = InputValidator(api_key=self._api_key).classify(prompt)
                except Exception as e:
                    return StageAborted(reason=str(e))

                if not validation.is_valid_protocol:
                    return StageAborted(
                        reason="input not recognized as protocol instruction",
                        help=CLASSIFICATION_HELP.get(validation.classification, []),
                        suggestion=validation.suggestion,
                    )
                return StageOk(summary="Input validated as protocol instruction.")

            if not self._run_stage(1, "Validating input", stage_1):
                return None

            # Holders for stage outputs that later stages read. Closures
            # below assign via `nonlocal`; the wrapper doesn't know about
            # these (it only translates StageResults).
            spec = None
            extractor = None

            def stage_2(s):
                nonlocal spec, extractor
                s.progress("Sonnet extracting protocol from instruction")
                extractor = SemanticExtractor(
                    client=self.config_loader.client,
                    model_name=self.config_loader.model_name,
                )
                extracted = extractor.extract(prompt)
                if extracted is None:
                    _save_state_log("stage_2_extraction")
                    return StageAborted(
                        reason="could not extract a protocol",
                        help=EXTRACTION_FAILURE_HELP,
                    )
                spec = extracted
                self.reporter.emit(StageEvent(
                    kind="extracted_spec", data={"spec": spec},
                    stage_name="stage_2_extraction",
                ))
                state_log["stage_2_extraction"] = spec.model_dump()
                s.info(f"Protocol type: {spec.protocol_type or 'ad-hoc'}")
                s.info(f"Steps: {len(spec.steps)}")
                if spec.reasoning:
                    s.info("Reasoning:")
                    for line in _format_reasoning_lines(spec.reasoning):
                        s.info(line)
                if spec.initial_contents:
                    s.info(f"Initial state: {len(spec.initial_contents)} wells/tubes have contents")
                return StageOk()

            if not self._run_stage(2, "Extracting protocol", stage_2):
                return None

            # State that flows between stages 3-7. Each closure declares
            # `nonlocal` for whatever it writes; reads are automatic.
            ic_suggesters = None
            reviewer_model = "claude-haiku-4-5"
            labware_suggestions = None
            labware_reviewer_objections = None
            confirmed = None
            protocol_schema = None
            script = None
            simulation_log = ""
            runlog: list = []
            step_summaries: list = []

            def stage_3(s):
                nonlocal ic_suggesters, labware_suggestions, labware_reviewer_objections
                s.progress("matching descriptions to config labware")
                from .gap_resolution import (
                    ConfigLookupSuggester, CarryoverSuggester,
                    WellCapacitySuggester, RegexFromNoteSuggester,
                    WellRangeClipSuggester, LLMSpotSuggester,
                )
                ic_suggesters = [
                    ConfigLookupSuggester(), CarryoverSuggester(),
                    WellCapacitySuggester(), RegexFromNoteSuggester(),
                    WellRangeClipSuggester(),
                    LLMSpotSuggester(client=extractor.client,
                                      model_name=extractor.model_name),
                ]

                s.progress("auditing labware descriptions")
                if not self._resolve_description_gaps_pre_pass(
                    spec, ic_suggesters, prompt, extractor, reviewer_model,
                ):
                    _save_state_log("stage_3_description_pre_pass")
                    return StageAborted(reason="description-gap pre-pass aborted")

                from .extraction import LabwareMatcher
                matcher = LabwareMatcher(
                    config=self.config_loader.config,
                    client=extractor.client,
                    model_name=extractor.model_name,
                )
                labware_suggestions = matcher.suggest(spec)
                labware_reviewer_objections = self._review_labware_suggestions(
                    labware_suggestions, prompt, extractor,
                )

                ns_split_applied = self._maybe_handle_namespace_split(spec)
                if ns_split_applied is False:
                    _save_state_log("stage_2_5_namespace_split")
                    return StageAborted(reason="namespace-split confirmation aborted")
                if ns_split_applied:
                    labware_suggestions = matcher.suggest(spec)
                    labware_reviewer_objections = self._review_labware_suggestions(
                        labware_suggestions, prompt, extractor,
                    )
                return StageOk()

            if not self._run_stage(3, "Resolving labware", stage_3):
                return None

            def stage_4(s):
                nonlocal confirmed
                # 1. IC batch confirm (live mode only — CLI keeps per-Gap flow).
                if self.initial_contents_handler is not None and spec.initial_contents:
                    s.progress("step 1 of 3 — initial contents")
                    ic_ok = self._confirm_initial_contents_via_handler(
                        spec, ic_suggesters,
                        {
                            "instruction": prompt,
                            "config": self.config_loader.config,
                            "labware_suggestions": labware_suggestions,
                        },
                    )
                    if not ic_ok:
                        _save_state_log("stage_2_5_initial_contents")
                        return StageAborted(reason="initial-contents confirmation aborted")

                # 2. Source-container inference + Y/n ack.
                if self.binary_confirm_handler is not None or sys.stdin.isatty():
                    s.progress("step 2 of 3 — source containers")
                source_only = infer_source_containers(spec)
                if source_only:
                    items = [
                        f"{lw} well {w}{f' ({sub})' if sub else ''}"
                        for lw, w, sub in source_only
                    ]
                    s.info("Inferred source containers (pre-filled by you before running):")
                    for line in items:
                        s.info(f"  - {line}")
                    user_aborted = False
                    if self.binary_confirm_handler is not None:
                        ok = self.binary_confirm_handler.confirm(
                            title="Confirm inferred source containers",
                            items=items,
                            yes_label="Yes, these are correct",
                            no_label="No, abort",
                        )
                        user_aborted = not ok
                    elif sys.stdin.isatty():
                        response = self.cm.prompt("Is this correct? [Y/n]: ").lower()
                        user_aborted = response in ('n', 'no')
                    if user_aborted:
                        state_log["stage_2_5_sources"] = [
                            {"labware": lw, "well": w, "substance": sub}
                            for lw, w, sub in source_only
                        ]
                        _save_state_log("stage_2_5_sources")
                        return StageAborted(
                            reason="source-container confirmation aborted",
                            help=["Adjust your instruction to clarify source containers."],
                        )
                    from .extraction import WellContents
                    for labware, well, substance in source_only:
                        already = any(ic.labware == labware and ic.well == well
                                      for ic in spec.initial_contents)
                        if not already:
                            spec.initial_contents.append(
                                WellContents(labware=labware, well=well,
                                             substance=substance or "reagent")
                            )

                # 3. Labware-assignments confirm.
                if self.assignments_handler is not None or sys.stdin.isatty():
                    s.progress("step 3 of 3 — labware assignments")
                if self.assignments_handler is not None:
                    confirmed = self._confirm_labware_assignments_via_handler(
                        spec, labware_suggestions,
                        reviewer_objections=labware_reviewer_objections,
                    )
                elif sys.stdin.isatty():
                    confirmed = cli_labware_loop(
                        spec, labware_suggestions,
                        self.config_loader.config, self.cm,
                    )
                if confirmed is None and (self.assignments_handler is not None
                                            or sys.stdin.isatty()):
                    _save_state_log("stage_2_5_assignments")
                    return StageAborted(reason="labware-assignments confirmation aborted")
                if confirmed is None:
                    confirmed = {
                        desc: sug.suggested_label
                        for desc, sug in labware_suggestions.items()
                        if sug.suggested_label is not None
                    }
                self._apply_labware_assignments(spec, labware_suggestions, confirmed)

                self.reporter.emit(StageEvent(
                    kind="labware_resolution_done",
                    data={
                        "resolutions": {
                            ref.description: ref.resolved_label
                            for step in spec.steps
                            for ref in [step.source, step.destination]
                            if ref and ref.resolved_label
                        },
                    },
                    stage_name="stage_3_labware_resolver",
                ))
                return StageOk()

            if not self._run_stage(4, "Confirming with you", stage_4):
                return None

            def stage_5(s):
                nonlocal spec
                from .gap_resolution import (
                    Orchestrator, CLIConfirmationHandler, default_apply_resolution,
                    MissingFieldsDetector, ProvenanceWarningDetector,
                    InitialContentsVolumeDetector, InitialContentsWellDetector,
                    ConstraintViolationDetector, LabwareAmbiguityDetector,
                    IndependentReviewSuggester,
                )
                orch = Orchestrator(
                    detectors=[
                        MissingFieldsDetector(),
                        ProvenanceWarningDetector(),
                        InitialContentsVolumeDetector(),
                        InitialContentsWellDetector(),
                        ConstraintViolationDetector(),
                        LabwareAmbiguityDetector(),
                    ],
                    suggesters=ic_suggesters,
                    reviewer=IndependentReviewSuggester(
                        client=extractor.client,
                        model_name=reviewer_model,
                    ),
                    handler=self.confirmation_handler or CLIConfirmationHandler(cm=self.cm, log=_log),
                    apply_resolution=default_apply_resolution,
                    reporter=self.reporter,
                )
                outcome = orch.run(spec, context={
                    "instruction": prompt,
                    "config": self.config_loader.config,
                })
                state_log["stage_3_gap_resolver"] = {
                    "converged": outcome.converged,
                    "aborted": outcome.aborted,
                    "iterations": [
                        {
                            "iteration": it.iteration,
                            "gap_count": len(it.records),
                            "auto_accepted": sum(1 for r in it.records if r.auto_accepted),
                        }
                        for it in outcome.iterations
                    ],
                }
                if outcome.aborted:
                    _save_state_log("stage_3_gap_resolver")
                    return StageAborted(reason="gap resolution aborted")
                if not outcome.converged:
                    _save_state_log("stage_3_gap_resolver")
                    return StageAborted(
                        reason="gap resolution hit iteration cap without converging",
                    )
                spec = outcome.spec
                self.reporter.emit(StageEvent(
                    kind="resolved_spec",
                    data={"spec": spec},
                    stage_name="stage_3_gap_resolver",
                ))
                unresolved_refs = []
                for step in spec.steps:
                    for ref in [step.source, step.destination]:
                        if ref and not ref.resolved_label and ref.description not in unresolved_refs:
                            unresolved_refs.append(ref.description)
                if unresolved_refs:
                    _save_state_log("stage_3_unresolved_labware")
                    return StageAborted(
                        reason=f"no config match for: {unresolved_refs}",
                        help=["Add appropriate labware to your config.json for these, then re-run."],
                    )
                return StageOk(summary="Gap resolution converged.")

            if not self._run_stage(5, "Resolving gaps", stage_5):
                return None

            def stage_6(s):
                from .validation.constraints import PhysicalConstraintsChecker
                checker = PhysicalConstraintsChecker(self.config_loader.config)
                constraint_result = checker.assert_physical_constraints(spec)
                state_log["stage_4_constraints"] = {
                    "errors": [str(v) for v in constraint_result.errors],
                    "warnings": [str(w) for w in constraint_result.warnings],
                }
                self.reporter.emit(StageEvent(
                    kind="constraint_check_done",
                    data={
                        "violation_count": len(constraint_result.errors),
                        "warnings": [str(w) for w in constraint_result.warnings],
                        "passed_checks": [
                            {
                                "step": p.step,
                                "check_type": p.violation_type.value,
                                "detail_label": p.detail_label,
                                "what": p.what,
                            }
                            for p in constraint_result.passes
                        ],
                    },
                    stage_name="stage_4_constraints",
                ))
                for w in constraint_result.warnings:
                    s.warning(str(w))
                if not constraint_result.has_errors:
                    return StageOk(summary="All constraints satisfied.")

                error_items = [str(v) for v in constraint_result.errors]
                s.info(f"HARDWARE CONFLICTS DETECTED ({len(error_items)}):")
                s.info("-" * 56)
                for v in error_items:
                    s.info(v)
                proceed = False
                if self.binary_confirm_handler is not None:
                    s.info("These conflicts mean the protocol may not execute correctly.")
                    proceed = self.binary_confirm_handler.confirm(
                        title="Hardware conflicts detected",
                        items=error_items,
                        yes_label="Proceed anyway",
                        no_label="Abort",
                    )
                elif sys.stdin.isatty():
                    s.info("These conflicts mean the protocol may not execute correctly.")
                    response = self.cm.prompt("Proceed anyway? [y/N]: ").lower()
                    proceed = response in ('y', 'yes')
                else:
                    s.info("Cannot proceed with hardware conflicts in non-interactive mode.")
                if not proceed:
                    _save_state_log("stage_4_constraints")
                    return StageAborted(
                        reason="aborted due to hardware conflicts",
                        help=["Fix your config or instruction and retry."],
                    )
                return StageOk(summary="Proceeding with constraint adjustments.")

            if not self._run_stage(6, "Checking hardware", stage_6):
                return None

            def stage_7(s):
                nonlocal protocol_schema, script, simulation_log, runlog, step_summaries
                s.progress("building protocol schema")
                state_log["stage_5_spec"] = spec.model_dump()
                try:
                    from .extraction import CompleteProtocolSpec
                    complete_spec = CompleteProtocolSpec.model_validate(spec.model_dump())
                    self.reporter.emit(StageEvent(
                        kind="completed_spec",
                        data={"spec": complete_spec},
                        stage_name="stage_5_spec",
                    ))
                    protocol_schema, _, step_summaries = spec_to_schema(
                        complete_spec, self.config_loader.config)
                except Exception as e:
                    err = str(e)
                    _save_state_log("stage_5_schema")
                    help_lines = ["Schema conversion failed."]
                    if "not found" in err.lower():
                        help_lines.append(f"A labware or module reference could not be resolved: {err}")
                        help_lines.append("Check that your config labels match the specification above.")
                    else:
                        help_lines.append(f"Detail: {err}")
                    help_lines.append(
                        "This is a deterministic step — the specification likely has an inconsistency.")
                    return StageAborted(reason="schema build failed", help=help_lines)

                s.progress("generating Python script")
                try:
                    script, step_line_map = generate_python_script(
                        protocol_schema, step_summaries=step_summaries,
                    )
                    self.reporter.emit(StageEvent(
                        kind="generated_script",
                        data={"script": script, "step_line_map": step_line_map},
                        stage_name="stage_6_script",
                    ))
                except ValueError as e:
                    _save_state_log("stage_6_script")
                    return StageAborted(
                        reason="script generation failed",
                        help=[
                            f"Detail: {e}",
                            "Try: simplify your instruction or check config slot assignments.",
                        ],
                    )

                debug_script = os.path.join(
                    output_dir,
                    f"debug_script_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py",
                )
                with open(debug_script, 'w') as f:
                    f.write(script)
                s.info(f"Debug: script saved to {debug_script}")

                s.progress("Opentrons simulator running")
                success, sim_log, run_log = simulate_script(script)
                simulation_log = sim_log
                runlog = run_log
                if not success:
                    if "errorType=" in simulation_log:
                        import re as _re
                        error_match = _re.search(r"errorType='([^']+)'", simulation_log)
                        error_detail_match = _re.search(
                            r"errorInfo=\{[^}]*'detail':\s*'([^']+)'", simulation_log)
                        error_type = error_match.group(1) if error_match else "Unknown"
                        error_detail = (
                            error_detail_match.group(1) if error_detail_match
                            else simulation_log[:300]
                        )
                        failure_lines = [
                            f"Simulation failed: {error_type}",
                            f"Detail: {error_detail[:200]}",
                        ]
                    else:
                        failure_lines = [f"Simulation failed: {simulation_log[:500]}"]
                    failure_lines.extend([
                        "The generated Python script did not pass the Opentrons simulator.",
                        f"Check {debug_script} to see the generated code.",
                    ])
                    _save_state_log("stage_7_simulation")
                    return StageAborted(reason="simulation failed", help=failure_lines)
                return StageOk(summary="Simulation passed.")

            if not self._run_stage(7, "Building & simulating", stage_7):
                return None

            # Pipeline complete. (Stage 8 — LLM intent verification — was removed
            # in ADR-0004; the deterministic constraint checker (Stage 4) and
            # Opentrons simulator (Stage 7) cover the load-bearing checks reliably,
            # while the intent verifier had a ~95% false-positive rate that eroded
            # user trust. See docs/adr/0004-remove-intent-verifier.md.)
            _save_state_log()
            # Flush buffered events for any reporter that batches (HTMLReporter etc.).
            # ConsoleReporter (default) is a no-op here.
            self.reporter.finalize()
            return PipelineResult(
                script=script,
                simulation_log=simulation_log,
                protocol_schema=protocol_schema,
                runlog=runlog,
                config=self.config_loader.config
            )
        except Exception as exc:
            # Uncaught exception escaped every per-stage failure save.
            # Dump the accumulated state_log (which already carries
            # whatever stage snapshots succeeded before the crash) plus
            # traceback so the failure is auditable. Re-raise so the
            # server's wrapper still sees the original exception.
            import traceback as _tb
            state_log["exception_type"] = type(exc).__name__
            state_log["exception_message"] = str(exc)
            state_log["traceback"] = "".join(_tb.format_exception(
                type(exc), exc, exc.__traceback__,
            ))
            try:
                _save_state_log("uncaught_exception")
            except Exception:
                pass
            raise


if __name__ == "__main__":
    import sys
    from nl2protocol.for_cli.cli import main

    # If no arguments provided, show help
    if len(sys.argv) == 1:
        sys.argv.append('--help')

    sys.exit(main())
