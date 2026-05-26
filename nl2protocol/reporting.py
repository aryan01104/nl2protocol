"""
reporting.py — Pluggable stage-event reporting for the pipeline.

The CLI runs `ProtocolAgent.run_pipeline` with a `ConsoleReporter` (default)
that prints stage banners and progress to stdout. Tests construct the agent
with a `CapturingReporter` to inspect the event stream without I/O. Future
HTML / TUI / web outputs implement the same `Reporter` Protocol and consume
the same structured events.

The reporter does NOT replace every `_log` call in the pipeline — only the
events that carry load-bearing data (stage transitions, the extracted spec,
the completed spec, the generated script, warnings, errors). Progress noise
("Reasoning through protocol...", "Validating config...") stays as `_log`
because it's not data downstream consumers need.

Why a structured event stream rather than free-text log lines: HTML / TUI
output sinks need typed access to spec, script, provenance — not a string
they have to parse. Structured events make the same data renderable in
multiple formats from one source.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Protocol, Tuple


# ============================================================================
# Event taxonomy
# ============================================================================

# Output kinds an event may carry. Keep this list closed — adding a new kind
# means every Reporter implementation must decide how to render it.
EventKind = Literal[
    "stage_start",          # data = {"stage": "Stage 2/7", "label": "Reasoning through ..."}
    "stage_complete",       # data = {"stage": "Stage 2/7", "summary": "..."}
    "stage_failed",         # data = {"stage": "Stage 2/7", "reason": "..."}
    "raw_instruction",      # data = {"instruction": "..."}
    # Phase 3b collapse: extracted/resolved/completed events all populate
    # the SAME "Protocol Steps" column. The temporal progression is
    # encoded per-cell via `prior_revisions` rather than per-column.
    "extracted_spec",       # data = {"spec": ProtocolSpec}        — initial column population
    "resolved_spec",        # data = {"spec": ProtocolSpec}        — re-render with revision chains
    "completed_spec",       # data = {"spec": CompleteProtocolSpec} — re-render with inline ✓ + warnings
    "generated_script",     # data = {"script": "..."}
    "warning",              # data = {"message": "...", "context": "..."}
    "error",                # data = {"message": "...", "stage": "..."}
    "info",                 # data = {"message": "..."}  — short status notes
    # ADR-0011 Phase 1 — orchestrator + pipeline storytelling events.
    # The existing HTMLReporter ignores these; Phase 2 renders them. They
    # are emitted by the orchestrator (gap_iteration_*, gap_detected,
    # gap_resolved) and the pipeline (labware_resolution_done,
    # constraint_check_done) at the points where the user-visible spec
    # transforms.
    "gap_iteration_start",  # data = {"iteration": int, "gap_count": int}
    "gap_iteration_end",    # data = {"iteration": int, "resolved_count": int, "remaining": int}
    "gap_detected",         # data = {"gap_id": str, "gap_kind": str, "field_path": str, "step_order": Optional[int], "description": str}
    "gap_resolved",         # data = {"gap_id": str, "resolution_kind": str, "value_repr": str, "auto_accepted": bool, "field_path": str, "step_order": Optional[int]}
    "labware_resolution_done",  # data = {"resolutions": {description: resolved_label, ...}}
    "constraint_check_done",    # data = {"violation_count": int, "warnings": [str, ...]}
]


@dataclass
class StageEvent:
    """Single structured event emitted at a meaningful pipeline checkpoint.

    `kind` says what happened; `data` is a kind-specific dict (typed loosely
    so consumers can grow without schema migration). `timestamp` and
    `stage_name` give consumers enough to render chronological / per-stage
    views.
    """

    kind: EventKind
    data: dict
    stage_name: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


# ============================================================================
# Reporter interface
# ============================================================================

class Reporter(Protocol):
    """Sink for pipeline stage events.

    `emit` is called by `ProtocolAgent.run_pipeline` at every meaningful
    checkpoint. `finalize` is called once at the end of a successful or
    failed run; sinks that buffer events (HTMLReporter, etc.) write their
    output here.
    """

    def emit(self, event: StageEvent) -> None:
        """Record a single stage event."""
        ...

    def finalize(self) -> None:
        """Called once at end-of-run. Sinks that buffer should write here."""
        ...


# ============================================================================
# ConsoleReporter — preserves current CLI behavior
# ============================================================================

class ConsoleReporter:
    """Default reporter — silent on all events.

    Rationale: the CLI already prints stage banners, progress messages, spec
    summaries etc. via the existing `_log` / `_stage` helpers in pipeline.py.
    ConsoleReporter exists as the default `Reporter` so the type is satisfied;
    it does NOT print anything itself, because doing so would duplicate the
    CLI output. The CLI flow stays exactly as today.

    Future text-renderers (e.g., a SilentReporter for tests, a JSONReporter
    for log aggregation) can implement Reporter without colliding with the
    CLI output. The HTMLReporter (Phase 2) buffers events via
    CapturingReporter and renders one HTML file at finalize().
    """

    def emit(self, event: StageEvent) -> None:
        pass  # silent — see class docstring

    def finalize(self) -> None:
        pass


# ============================================================================
# CapturingReporter — for tests + base for buffering reporters (HTMLReporter)
# ============================================================================

class CapturingReporter:
    """Collects all events to a list. Useful for tests + as the base for
    sinks that need to buffer the entire run before rendering (e.g.,
    HTMLReporter, which renders one HTML file at finalize()).

    Pre:    Constructed with no args.
    Post:   `events` is a list of every `StageEvent` passed to `emit`, in
            order. `finalize()` is a no-op for the base class — subclasses
            override to render at end-of-run.
    """

    def __init__(self):
        self.events: List[StageEvent] = []

    def emit(self, event: StageEvent) -> None:
        self.events.append(event)

    def finalize(self) -> None:
        pass

    # Convenience accessors for tests
    def events_of_kind(self, kind: EventKind) -> List[StageEvent]:
        return [e for e in self.events if e.kind == kind]

    def first_of_kind(self, kind: EventKind) -> Optional[StageEvent]:
        events = self.events_of_kind(kind)
        return events[0] if events else None


# ============================================================================
# HTMLReporter — renders a self-contained report.html at finalize()
# ============================================================================

# Color CSS classes for each Provenance.source value. Kept in sync with the
# template's CSS variables so the visualization is consistent.
_PROV_CLASS = {
    "instruction":    "prov-instruction",
    "config":         "prov-config",
    "domain_default": "prov-domain_default",
    "inferred":       "prov-inferred",
}

# Per-citation palette size. 8 distinguishable hues defined in the template's
# CSS as .palette-0 .. .palette-7. Each unique cited_text deterministically
# hashes to one slot; both the cite span in the instruction column and the
# atomic value span in the spec column carry the same .palette-N class so
# readers can connect them by hue without any arrow.
_PALETTE_SIZE = 8


# ============================================================================
# Constraint warning routing — partition by step number for inline anchoring
# ============================================================================

_WARN_STEP_RE = re.compile(r"^\s*\[WARN\]\s+Step\s+(\d+)\s*:", re.IGNORECASE)


def _split_warnings_by_step(
    warnings: List[str],
) -> Tuple[Dict[int, List[str]], List[str]]:
    """Partition constraint warnings into per-step buckets + a global bucket.

    Pre:    `warnings` is a list of constraint-checker warning strings,
            each typically formatted as "[WARN] Step N: ...". N is the
            1-based step.order for step-specific concerns, or 0 for
            protocol-level concerns (e.g. "Protocol uses temperature
            control but no labware is on the temperature module").

    Post:   Returns (per_step, global_warnings) where:
              - per_step[step_order] is the list of warnings whose
                leading "[WARN] Step N:" parses to step_order >= 1.
                Dict iteration order matches first-seen step_order.
              - global_warnings is the list of warnings whose leading
                step number is 0 OR whose prefix doesn't match the
                regex (defensive — unrecognized formats land in the
                global bucket rather than getting dropped).
            Each input warning lands in exactly one bucket; total
            count is preserved. Order within each bucket is preserved.

    Side effects: None.

    Why this lives in reporting.py and not constraints.py: the source
    of constraint warnings (constraints.py) emits formatted strings.
    The decision about WHERE to surface them in the UI is presentation,
    so the parser lives next to the renderer that consumes it.
    """
    per_step: Dict[int, List[str]] = {}
    global_warnings: List[str] = []
    for w in warnings:
        m = _WARN_STEP_RE.match(w)
        if m:
            step_n = int(m.group(1))
            if step_n >= 1:
                per_step.setdefault(step_n, []).append(w)
                continue
        global_warnings.append(w)
    return per_step, global_warnings


def _passes_to_inline_checks(
    passed_checks: List[dict],
) -> Dict[int, Dict[str, str]]:
    """Group serialized passed-check records into the per-step dict the
    template consumes for inline-tag rendering.

    Pre:    `passed_checks` is the list serialized by `pipeline.py` from
            `PhysicalConstraintsCheckResult.passes`. Each entry is a dict
            with keys `step` (int, 0 for protocol-level), `check_type`
            (str enum value), `detail_label` (str | None — names the
            spec-view row to anchor on), and `what` (str — the inline
            phrase).

    Post:   Returns a dict mapping `step_order` (>=1) to a dict
            `{detail_label: phrase}`. Entries with `step == 0` or
            `detail_label is None` are skipped — they don't anchor to
            a spec row, so the inline-tag renderer has nothing to do
            with them. Order within each step is the input order;
            later entries for the same (step, label) cell overwrite
            earlier ones (the invariant in `constraints.py` is "one
            CheckResult per cell" so collisions shouldn't occur, but
            the dict assignment makes the behavior deterministic if
            they ever do).

    Side effects: None.
    """
    by_step: Dict[int, Dict[str, str]] = {}
    for entry in passed_checks:
        step = entry.get("step")
        label = entry.get("detail_label")
        phrase = entry.get("what")
        if not step or step < 1 or not label or not phrase:
            continue
        by_step.setdefault(step, {})[label] = phrase
    return by_step


def _palette_class(cited_text) -> str:
    """Deterministic hash of `cited_text` → one of `palette-0` .. `palette-N`.

    md5-based so the same cite always lands on the same hue across runs
    (Python's built-in hash() randomizes per-process via PYTHONHASHSEED).
    Accepts either a single string (atomic / step-level cites) OR a list
    of strings (multi-cite atomic-value cites — wells lists spread across
    bullet points). The hash key is the joined list so different cite
    lists produce different colors; a single-string input is equivalent
    to a one-element list under the join.
    """
    import hashlib
    if not cited_text:
        return "palette-0"
    if isinstance(cited_text, list):
        key = "\u0001".join(cited_text)
    else:
        key = str(cited_text)
    digest = hashlib.md5(key.encode("utf-8")).digest()
    return f"palette-{digest[0] % _PALETTE_SIZE}"


def _render_provenanced_value(
    value: Any,
    prov,
    prov_id: Optional[str] = None,
    instruction: Optional[str] = None,
) -> str:
    """Wrap a provenanced value in an HTML span colored by its source AND
    annotated with its review-lifecycle state.

    Per ADR-0005 + ADR-0009, `prov` carries:
      - source-attribution: cited_text (when source='instruction') or
        positive_reasoning + why_not_in_instruction (when source is
        domain_default/inferred).
      - review lifecycle: review_status (default 'original' when no reviewer
        or user has touched the value yet) and reviewer_objection (set iff
        review_status == 'reviewed_disagree').

    The JS tooltip consumes the data-* attrs at hover time. The
    `prov-review-<status>` CSS class drives the inline ⚠ / ✎ badges for
    high-signal review states.

    Color rules:
      - source="instruction" with a recoverable cited_text → adds `palette-N`
        class so the value matches its cite span's hue in the instruction.
      - other sources → categorical class only (.prov-domain_default etc.)
        with dotted-underline styling driven from CSS.

    Review-state rules:
      - review_status != 'original' → adds `prov-review-<status>` class.
        The CSS pseudo-element on this class draws the inline badge for
        the two attention-worthy states (reviewed_disagree, user_edited).
        Other non-original states (reviewed_agree, user_accepted_suggestion,
        user_confirmed, user_skipped) get the class but no inline badge —
        their state is surfaced in the tooltip only.

    Linkage rules:
      - data-prov-id is ALWAYS emitted when prov_id is provided. Per
        ADR-0011 Phase 2b/4 polish, the data-prov-id is a CELL IDENTIFIER
        used by resolution arrows (extracted column → resolved column)
        AND panel-row hover (lab-state / labware panel rows light up
        matching cells). The cite ↔ value pair-highlight (the original
        use of data-prov-id) only fires for spans whose cite is
        recoverable in the instruction column — but the JS pair-highlight
        already no-ops gracefully when no matching cite span exists,
        so universal emission doesn't break that path.
    """
    import html
    source = getattr(prov, "source", "inferred")
    cited_text = getattr(prov, "cited_text", None)
    positive_reasoning = getattr(prov, "positive_reasoning", None)
    why_not_in_instruction = getattr(prov, "why_not_in_instruction", None)
    review_status = getattr(prov, "review_status", "original") or "original"
    reviewer_objection = getattr(prov, "reviewer_objection", None)
    confidence = getattr(prov, "confidence", 0.0)

    classes = [_PROV_CLASS.get(source, "prov-inferred")]

    # cited_text is List[str] post-normalizer. Drop empties just in case.
    cite_list = list(cited_text) if isinstance(cited_text, list) else (
        [cited_text] if cited_text else []
    )
    cite_list = [c for c in cite_list if c]

    # Decide whether the cite is recoverable (drives both palette + linkage).
    # For a multi-cite list, "recoverable" means EVERY cite resolves in the
    # instruction. If any fails, the value is fabrication-flagged elsewhere
    # so we don't want to claim a clean color-match here.
    cite_recoverable = False
    if source == "instruction" and cite_list:
        if instruction is None:
            cite_recoverable = True
        else:
            cite_recoverable = all(
                _find_cite_position(instruction, c) is not None for c in cite_list
            )

    if cite_recoverable and cite_list:
        classes.append(_palette_class(cite_list))

    # Review-lifecycle class. The CSS pseudo-element on this class draws
    # the inline ⚠ / ✎ badge for the two attention-worthy states; other
    # non-original states get the class but render no inline badge.
    if review_status != "original":
        classes.append(f"prov-review-{review_status.replace('_', '-')}")

    rendered_value = html.escape(str(value))

    # data-prov-id ALWAYS emitted when prov_id is provided. Per ADR-0011
    # Phase 2b/4 polish, the attribute is the cell's identity for arrow
    # anchoring + panel-row hover; the cite ↔ value pair-highlight (the
    # original use) is a separate concern that no-ops gracefully when
    # no matching cite span exists in the instruction column.
    extra_attrs = ""
    if prov_id:
        extra_attrs = f' data-prov-id="{html.escape(prov_id, quote=True)}"'

    # Stash provenance fields on the element so the JS tooltip can read them
    # without us having to compute / escape a long native title= string.
    extra_attrs += f' data-prov-source="{html.escape(source, quote=True)}"'
    extra_attrs += f' data-prov-confidence="{confidence:.2f}"'
    extra_attrs += f' data-prov-review-status="{html.escape(review_status, quote=True)}"'
    if cite_list:
        # Join with " | " so a multi-cite tooltip can show all cites
        # at a glance. Single-cite case shows exactly the original.
        joined_cites = " | ".join(cite_list)
        extra_attrs += f' data-prov-cited="{html.escape(joined_cites, quote=True)}"'
    if positive_reasoning:
        extra_attrs += f' data-prov-positive-reasoning="{html.escape(positive_reasoning, quote=True)}"'
        # Backwards-compat alias for any external consumer that still reads
        # the legacy single-reasoning attr (kept until ADR-0010).
        extra_attrs += f' data-prov-reasoning="{html.escape(positive_reasoning, quote=True)}"'
    if why_not_in_instruction:
        extra_attrs += f' data-prov-why-not-in-instruction="{html.escape(why_not_in_instruction, quote=True)}"'
    if reviewer_objection:
        extra_attrs += f' data-prov-reviewer-objection="{html.escape(reviewer_objection, quote=True)}"'

    # The ▴ marker that used to prepend non-instruction atoms was removed —
    # the color class + dotted-underline already communicate "model-filled"
    # without the third visual cue. The marker was visual noise.
    return f'<span class="{" ".join(classes)}"{extra_attrs}>{rendered_value}</span>'


def _render_revisioned_value(field, value_formatter, prov_getter, *,
                              prov_id: Optional[str], instruction: Optional[str]):
    """Render the evolution chain of a tracked field as inline value spans
    separated by → arrows. Falls back to the single-span renderer when the
    field has no `prior_revisions`.

    Pre:    `field` is a tracked head (ProvenancedVolume / Duration /
            Temperature / String, LocationRef). `value_formatter` is a
            callable taking a field-like instance and returning the
            display string for that revision. `prov_getter` extracts the
            Provenance for the relevant sub-slot from a revision (e.g.
            `lambda lr: lr.wells_provenance` for the wells row on a
            LocationRef). `prov_id` is the cell anchor used for cite ↔
            value pair-highlight + lab-state row hover; priors do NOT
            get a prov_id (only the head). `instruction` is the raw
            instruction text used to verify cite-recoverability for
            palette assignment, same as `_render_provenanced_value`.

    Post:   Returns one HTML string. When the field has no priors
            (or all priors project to a None provenance via
            `prov_getter`), the output is identical to calling
            `_render_provenanced_value(value_formatter(field),
            prov_getter(field), ...)` — backwards-compatible.
            When at least one prior projects cleanly, the output is
            `<prior-span> → <prior-span> → ... → <head-span>` with each
            prior wrapped in `.prior-rev` for the strikethrough/dim
            styling and the arrows wrapped in `.rev-arrow` for spacing.
            Priors carry `data-rev-idx` so JS can identify them by
            chain position.

    Side effects: None. Pure formatting.
    """
    priors_raw = list(getattr(field, "prior_revisions", []) or [])
    head_prov = prov_getter(field)
    head_text = value_formatter(field)

    # Build the chronological chain — oldest prior first, head last.
    # Each item: (text, prov, is_head). Priors whose projected
    # provenance is None are skipped (the sub-field wasn't populated
    # at that revision; rendering would be unattributed).
    items = []
    for rev in priors_raw:
        rev_prov = prov_getter(rev)
        if rev_prov is None:
            continue
        items.append((value_formatter(rev), rev_prov, False))
    items.append((head_text, head_prov, True))

    # Collapse consecutive duplicates by displayed text. A push_revision
    # call always snapshots the WHOLE tracked object, so a revision can
    # appear in the chain even when its projection for THIS sub-field
    # didn't change (e.g., the stage 2.5 labware-assignment push leaves
    # the wells row's text unchanged). Adjacent-identical text segments
    # are visual noise — collapse runs to keep only state transitions.
    #
    # From each run, pick a single representative: prefer the head when
    # the head is in the run (so the head's provenance — typically the
    # most-resolved attribution — survives the collapse), else keep the
    # first item in the run (the earliest snapshot carrying that value).
    def _flush(run, out):
        if not run:
            return
        head_in_run = next((x for x in run if x[2]), None)
        out.append(head_in_run if head_in_run is not None else run[0])

    deduped = []
    current_run = []
    current_text = None
    for item in items:
        text = item[0]
        if text == current_text:
            current_run.append(item)
        else:
            _flush(current_run, deduped)
            current_run = [item]
            current_text = text
    _flush(current_run, deduped)

    # When only the head survives, render as a single span (no chain).
    if len(deduped) == 1:
        text, p, _ = deduped[0]
        return _render_provenanced_value(
            text, p, prov_id=prov_id, instruction=instruction,
        )

    segments = []
    for i, (text, p, is_head) in enumerate(deduped):
        if is_head:
            segments.append(_render_provenanced_value(
                text, p, prov_id=prov_id, instruction=instruction,
            ))
        else:
            inner = _render_provenanced_value(
                text, p, prov_id=None, instruction=instruction,
            )
            segments.append(
                f'<span class="prior-rev" data-rev-idx="{i}">{inner}</span>'
            )
    return '<span class="rev-arrow" aria-hidden="true">→</span>'.join(segments)


# ============================================================================
# Phase 3: Span recovery — find cited_text positions in the instruction.
# The predicate lives in nl2protocol.citing so the verifier (extractor) and
# the visual surface (reporter) share one implementation; ADR-0003 calls
# this out as a requirement for cited-text-driven verification.
# ============================================================================

from nl2protocol.citing import (
    find_cite_position as _find_cite_position,
    normalize_for_match as _normalize_for_match,
)


def _collect_arrow_targets(spec) -> list:
    """Collect cite-spans to wrap in the instruction column.

    Per the visual design (two non-competing languages):
      - Composite Q1 cites become ARROW endpoints (one thick arrow per step,
        instruction phrase → step block). Marked with kind="composite-step".
      - Atomic instruction-sourced cites become COLOR-MATCHED spans in the
        instruction; the matching value in the spec column shares the same
        color. No arrow. Marked with kind="atomic-color".
      - Q2 cohesion cites are ON-HOVER highlights that light up when the
        user hovers the corresponding step block. Marked with kind="q2-cohesion".

    Returns a list of dicts: [{prov_id, cited_text, kind}].
    """
    targets = []
    for step_idx, step in enumerate(spec.steps):
        sid = f"s{step_idx}"
        comp = step.composition_provenance

        # Composite Q1 (step trigger) — arrow endpoint.
        targets.append({
            "prov_id": f"{sid}-step-trigger",
            "cited_text": comp.step_cited_text,
            "kind": "composite-step",
        })

        # Composite Q2 (parameter cohesion) — on-hover highlights, no arrow.
        # Each step gets one Q2 group; `step_id` lets JS toggle them all on hover.
        for ci, cite in enumerate(comp.parameters_cited_texts):
            targets.append({
                "prov_id": f"{sid}-q2-{ci}",
                "step_id": sid,
                "cited_text": cite,
                "kind": "q2-cohesion",
            })

        # Atomic per-value cites — color-matched in instruction column.
        # Each unique cited_text hashes to a palette slot; the same slot is
        # applied to the spec value span so reader connects them by hue.
        # LocationRefs carry TWO provenances (description + wells); each that
        # is instruction-sourced contributes its own cite-span, both anchored
        # to the same field prov_id so hovering the cell highlights both.
        atomic_fields = [
            ("volume", step.volume, ("provenance",)),
            ("substance", step.substance, ("provenance",)),
            ("duration", step.duration, ("provenance",)),
            ("temperature", step.temperature, ("provenance",)),
            ("source", step.source, ("description_provenance", "wells_provenance")),
            ("destination", step.destination, ("description_provenance", "wells_provenance")),
        ]
        for field_name, field_obj, prov_attrs in atomic_fields:
            if field_obj is None:
                continue
            for attr in prov_attrs:
                prov = getattr(field_obj, attr, None)
                if prov is None:
                    continue
                if getattr(prov, "source", None) != "instruction":
                    continue
                cited = getattr(prov, "cited_text", None)
                if not cited:
                    continue
                # cited_text is List[str] post-normalizer. Emit one target
                # per cite so each substring gets its own colored span in
                # the instruction. All cites for the same field share the
                # palette derived from the FULL list (the hash key is the
                # joined list), so a wells-list with N cites colors all N
                # spans the same hue — connecting them visually as one
                # logical citation cluster while still highlighting each
                # actual phrase the LLM quoted.
                cite_iter = cited if isinstance(cited, list) else [cited]
                palette = _palette_class(cited)
                # Cites under wells_provenance target the indented wells
                # row (its own prov_id) so the cite-span ↔ value hover
                # pairs the right visual row. Description / atomic
                # provenances target the parent prov_id.
                if (field_name in ("source", "destination")
                        and attr == "wells_provenance"):
                    target_prov_id = f"{sid}-{field_name}-wells"
                else:
                    target_prov_id = f"{sid}-{field_name}"
                for cite_idx, single_cite in enumerate(cite_iter):
                    if not single_cite:
                        continue
                    targets.append({
                        "prov_id": target_prov_id,
                        "cited_text": single_cite,
                        "kind": "atomic-color",
                        "palette_class": palette,
                    })
    return targets


def _render_instruction_with_marks(instruction: str, targets: list) -> str:
    """Wrap each cited substring in the instruction with a <span> carrying
    the prov_ids and kinds of every target that covers it.

    Overlap handling: cites are NOT mutually exclusive any more. When two or
    more targets cover the same chars, the instruction is segmented at every
    boundary and each segment carries the union of overlapping ids/kinds.
    `data-cite-id` becomes a space-separated list (use `~=` token match in
    CSS/JS); CSS classes accumulate (`cite-composite-step cite-atomic-color`).
    This unblocks two real cases:
      - Step 2's cite is a substring of step 1's (both arrows must draw).
      - An atomic value cite ("20uL") sits inside a composite step cite
        ("Mix at 20uL") — both should hover-link to their own targets.

    Targets whose `cited_text` can't be located in the instruction are
    dropped silently here — graceful degrade is handled by the value-side
    renderer (it omits `data-prov-id` so no broken hover is promised).
    """
    import html

    # Resolve each target to a char range; drop unrecoverable ones.
    spans = []
    for t in targets:
        pos = _find_cite_position(instruction, t["cited_text"])
        if pos is None:
            continue
        if pos[0] >= pos[1]:
            continue
        spans.append({
            "start": pos[0],
            "end": pos[1],
            "prov_id": t["prov_id"],
            "kind": t["kind"],
            "step_id": t.get("step_id"),
            # Carry palette_class through to the sweep-line layer so
            # _wrap_overlapping_segment can emit `.palette-N` on the cite
            # <span>. Without this, the cite-side hover-flip CSS rule
            # (.cite-marker.cite-atomic-color.palette-N.cite-active) never
            # matches, leaving only the faint .cite-active fallback —
            # the visual-attribution-invisible bug from PDF p.3.
            "palette_class": t.get("palette_class"),
        })

    if not spans:
        return html.escape(instruction)

    # Sweep line over span boundaries. At each char position we know the set
    # of spans currently covering it; emit one DOM <span> per maximal segment
    # of constant cover.
    events = []
    for s in spans:
        events.append((s["start"], 0, s))   # start sorts before end at same pos
        events.append((s["end"], 1, s))
    events.sort(key=lambda e: (e[0], e[1]))

    out = []
    cursor = 0
    active = []
    for pos, kind_marker, span in events:
        if pos > cursor:
            seg_text = instruction[cursor:pos]
            if active:
                out.append(_wrap_overlapping_segment(seg_text, active))
            else:
                out.append(html.escape(seg_text))
            cursor = pos
        if kind_marker == 0:
            active.append(span)
        else:
            active.remove(span)
    if cursor < len(instruction):
        out.append(html.escape(instruction[cursor:]))
    return "".join(out)


def _wrap_overlapping_segment(seg_text: str, active_spans: list) -> str:
    """Emit one DOM <span> for a segment covered by `active_spans`.

    Class list: "cite-marker" + one cite-<kind> per kind in cover + palette-N
    if any atomic-color cite is in cover (the palette class drives the hue).
    Data attrs: data-cite-id (space-separated prov_ids; use ~= in JS),
                data-q2-step-id (space-separated step_ids when Q2 cites cover).
    """
    import html
    kinds = sorted({s["kind"] for s in active_spans})
    classes = ["cite-marker"] + [f"cite-{k}" for k in kinds]
    # First atomic-color cite drives the palette hue (multiple atomics
    # rarely overlap; the prompt asks for tight cites).
    palette_cls = next(
        (s.get("palette_class") for s in active_spans
         if s["kind"] == "atomic-color" and s.get("palette_class")),
        None,
    )
    if palette_cls:
        classes.append(palette_cls)
    cite_ids = " ".join(s["prov_id"] for s in active_spans)
    q2_step_ids = " ".join(
        s["step_id"] for s in active_spans
        if s["kind"] == "q2-cohesion" and s.get("step_id")
    )
    attrs = [
        f'class="{" ".join(classes)}"',
        f'data-cite-id="{html.escape(cite_ids, quote=True)}"',
    ]
    if q2_step_ids:
        attrs.append(f'data-q2-step-id="{html.escape(q2_step_ids, quote=True)}"')
    return f'<span {" ".join(attrs)}>{html.escape(seg_text)}</span>'


def _step_class(grounding: List[str]) -> str:
    """CSS class for a step block based on its CompositionProvenance grounding.

    Per ADR-0005's "instruction" invariant, every step has instruction
    grounding — orphan steps are rejected at parse time, so step-orphan
    is no longer a possible state. Compound grounding (instruction +
    domain_default) renders as 'mixed' to surface the domain-knowledge
    expansion in the visualization.
    """
    if "domain_default" in grounding:
        return "step-grounded-mixed"
    return "step-grounded-instruction"


def _format_location(loc) -> str:
    """Render a LocationRef as `<description> (<wells>) [<resolved_label>]`.

    The resolved_label is appended in brackets when the labware resolver
    has filled it in (stage 3.5). Visually this is the headline difference
    between the extracted-spec column (no resolved_label) and the
    complete-spec column (resolved_label populated). Without surfacing it
    the two columns look identical to a reader.
    """
    bits = []
    if loc.well:
        bits.append(f"well {loc.well}")
    if loc.wells:
        head = ", ".join(loc.wells[:6])
        bits.append(f"wells [{head}{'...' if len(loc.wells) > 6 else ''}]")
    if loc.well_range:
        bits.append(loc.well_range)
    text = f"{loc.description} ({', '.join(bits)})" if bits else loc.description
    if getattr(loc, "resolved_label", None):
        text += f"  [{loc.resolved_label}]"
    return text


def _format_labware_label(loc) -> str:
    """Just the labware portion of a LocationRef: '"tube rack"' or
    '"tube rack" → labware: [reagent_rack]' when resolved.

    Pre:    `loc` is a LocationRef with `description` (required) and
            an optional `resolved_label`.
    Post:   Returns '"description"' alone if resolved_label is null;
            '"description" → labware: [resolved_label]' otherwise.
            The double quotes are P2-2: they make the user's verbatim
            wording visually distinguishable from neighboring plain
            text on the row (substance, action name). The arrow +
            "labware:" prefix is CARRY-D1: makes the description →
            config-key mapping direction visible at a glance, per
            the user's PDF p.2 literal `tube_rack → labware: [reagent_rack]`.
            No well info in this string — that's `_format_wells_only`'s job.
    """
    text = f'"{loc.description}"'
    if getattr(loc, "resolved_label", None):
        text += f" \u2192 labware: [{loc.resolved_label}]"
    return text


def _format_wells_only(loc) -> Optional[str]:
    """Just the well/wells/well_range portion of a LocationRef.

    Pre:    `loc` is a LocationRef with at most one of well, wells,
            well_range populated (per the LocationRef contract).
    Post:   Returns 'well A1' / 'wells A1, A2, A3, A4' (with '...'
            suffix when >6 wells) / 'A1-A12'. Returns None when no
            well field is populated — caller skips the row.
    """
    if loc.well:
        return f"well {loc.well}"
    if loc.wells:
        head = ", ".join(loc.wells[:6])
        suffix = "..." if len(loc.wells) > 6 else ""
        return f"wells {head}{suffix}"
    if loc.well_range:
        return loc.well_range
    return None


_PROV_ID_RENDERABLE_FIELDS = {
    "volume", "duration", "temperature", "substance", "source", "destination",
}


def _field_path_to_prov_id(field_path: str) -> Optional[str]:
    """Map a Gap.field_path to the matching `data-prov-id` used in the
    rendered spec columns — but only for fields the renderer actually
    emits as prov-id-bearing cells.

    Pre:    `field_path` is a dotted path like `steps[N].volume`,
            `steps[N].source`, `steps[N].source.resolved_label`,
            `initial_contents[N].volume_ul`, or `steps[N].constraint`.

    Post:   Returns `s{N}-{field}` only when:
              * The path matches `steps[N].<field>` or
                `steps[N].<field>.<subfield>` (subfield writes update
                the parent ref's primary cell — same prov_id as the
                top-level write).
              * `<field>` is in `_PROV_ID_RENDERABLE_FIELDS` — the six
                fields the spec-column renderer attaches data-prov-id
                to (volume, duration, temperature, substance, source,
                destination).
            Returns None otherwise — `initial_contents[N].volume_ul`
            (no prov-id-bearing cell), `steps[N].constraint` (placeholder
            field; not rendered as a cell), unknown shapes. Callers
            downstream skip None paths since they can't anchor an arrow.

    Side effects: None.
    """
    import re
    m = re.match(r"steps\[(\d+)\]\.(\w+)(?:\.(\w+))?$", field_path)
    if m and m.group(2) in _PROV_ID_RENDERABLE_FIELDS:
        field = m.group(2)
        subfield = m.group(3)
        # LocationRef subfields point at the specific sub-row in the
        # split rendering: `wells_provenance` lands on the wells row
        # (its own prov_id), description_provenance + resolved_label*
        # land on the labware row (the parent prov_id). This lets
        # resolution arrows and cite-marker spans target the correct
        # visual row without ambiguity.
        if field in ("source", "destination") and subfield == "wells_provenance":
            return f"s{m.group(1)}-{field}-wells"
        return f"s{m.group(1)}-{field}"
    return None


def _collect_resolution_arrows(events) -> list:
    """Build the list of arrows to draw from extracted-spec to resolved-spec.

    Per ADR-0011 Phase 2b: each `gap_resolved` event becomes one arrow IF
    (a) the resolution committed a value (skipped/aborted are not drawn —
    no destination cell change to encode), and (b) the field_path maps to
    a `data-prov-id` the renderer exposes (steps[N].field shapes only —
    initial_contents and constraint paths don't have prov-id-bearing cells
    today).

    Pre:    `events` is the captured event list from a CapturingReporter
            (or an HTMLReporter, since it inherits). Order matters only
            for tie-breaking when the same field gets resolved multiple
            times across iterations — the LAST resolution wins.

    Post:   Returns a list of dicts ready to embed in the template as JSON:
              {
                "prov_id":            str,    # data-prov-id shared across columns
                "resolution_kind":    str,    # auto_accepted / user_accepted_suggestion / user_edited / user_confirmed
                "field_path":         str,    # for tooltip / debug surface
                "step_order":         Optional[int],
                "value_repr":         str,    # already capped at 200 chars by orchestrator
              }
            Drawable resolution_kinds: auto_accepted, user_accepted_suggestion,
            user_edited, user_confirmed. Other kinds (user_skipped,
            user_aborted) are filtered out. The same prov_id resolved across
            iterations keeps only the LAST resolution (the final state the
            user sees in the resolved column).

    Side effects: None.
    """
    drawable_kinds = {
        "auto_accepted",
        "user_accepted_suggestion",
        "user_edited",
        "user_confirmed",
    }
    by_prov_id = {}  # prov_id -> dict (latest wins)
    for event in events:
        if event.kind != "gap_resolved":
            continue
        kind = event.data.get("resolution_kind", "")
        if kind not in drawable_kinds:
            continue
        prov_id = _field_path_to_prov_id(event.data.get("field_path", ""))
        if prov_id is None:
            continue
        by_prov_id[prov_id] = {
            "prov_id": prov_id,
            "resolution_kind": kind,
            "field_path": event.data.get("field_path", ""),
            "step_order": event.data.get("step_order"),
            "value_repr": event.data.get("value_repr", ""),
        }
    return list(by_prov_id.values())


def _lab_state_row_target_prov_ids(spec, labware: str, well: str) -> list:
    """Find every spec cell whose source/destination LocationRef references
    the given (labware, well) tuple. Returns a list of prov-ids the JS
    can use to highlight the matching cells when a lab-state row is hovered.

    Per ADR-0011 Phase 4 (cross-column row hover): each lab-state row
    carries a space-separated `data-target-cells` attribute carrying these
    prov-ids; JS toggles `.prov-active` on those cells when the row is
    hovered.

    Pre:    `spec` is a ProtocolSpec (or None). `labware` is the
            row's labware name (config label or user description, whichever
            the row stores). `well` is the row's well name; empty string
            for prefilled-labware rows that cover all wells uniformly.
    Post:   Returns a list of "s{step_idx}-{role}" strings where the step's
            source or destination LocationRef has `resolved_label OR
            description == labware` AND well coverage matches:
              * empty `well` (prefilled row) → every cell on this labware matches
              * single `ref.well == well` → match
              * `well` in `ref.wells` list → match
              * `ref.well_range` coverage is NOT computed in this MVP —
                future polish; today's user case (Bradford and similar)
                doesn't need it.
            Empty list when spec is None.
    Side effects: None.
    """
    if spec is None:
        return []
    matches = []
    for step_idx, step in enumerate(getattr(spec, "steps", [])):
        sid = f"s{step_idx}"
        for fname in ("source", "destination"):
            ref = getattr(step, fname, None)
            if ref is None:
                continue
            ref_label = getattr(ref, "resolved_label", None) or getattr(ref, "description", "")
            if ref_label != labware:
                continue
            if well == "":
                matches.append(f"{sid}-{fname}")
                continue
            if getattr(ref, "well", None) == well:
                matches.append(f"{sid}-{fname}")
                continue
            if well in (getattr(ref, "wells", None) or []):
                matches.append(f"{sid}-{fname}")
                continue
    return matches


def _labware_mapping_row_target_prov_ids(spec, description: str,
                                          resolved_label: Optional[str]) -> list:
    """Find every spec cell whose source/destination LocationRef matches
    the given description OR resolved_label. Returns a list of prov-ids
    for the JS to highlight on row hover.

    Pre:    `spec` is a ProtocolSpec (or None). `description` is the
            user-language wording stored on the row. `resolved_label` is
            the config label the row resolved to (or None if unresolved).
    Post:   Returns a list of "s{step_idx}-{role}" strings where either:
              * `ref.description == description`, OR
              * `resolved_label is not None AND ref.resolved_label == resolved_label`
            Both conditions OR'd because the row links the description to
            its label; either match identifies a cell relevant to this row.
            Empty list when spec is None.
    Side effects: None.
    """
    if spec is None:
        return []
    matches = []
    for step_idx, step in enumerate(getattr(spec, "steps", [])):
        sid = f"s{step_idx}"
        for fname in ("source", "destination"):
            ref = getattr(step, fname, None)
            if ref is None:
                continue
            if (getattr(ref, "description", None) == description or
                (resolved_label is not None and
                 getattr(ref, "resolved_label", None) == resolved_label)):
                matches.append(f"{sid}-{fname}")
    return matches


def _render_script_with_step_tags(script: str, step_line_map: dict) -> str:
    """Wrap each script line in a <span class="code-line"> tagged with the
    spec step that produced it, so the Validated Spec ↔ Generated Python
    hover-pair (#72) can highlight matching lines on either side.

    Pre:    `script` is the raw \\n-joined script string returned by
            `generate_python_script`. `step_line_map` is the
            {step_idx: [line_idx, ...]} dict from the same call. Lines
            not in any step's list (prelude — module/labware/pipette
            loads, blank separators) get a span with no step tag.
    Post:   Returns the inner HTML for `<pre class="code">`. Each line
            is a `<span class="code-line"[data-script-step="s{idx}"]>...</span>`
            ending in `\\n` so the <pre> still renders the script as
            multi-line text. HTML-escaped per line. Empty input → empty
            string.

    Static reports + the live-mode generated_script event both consume
    this helper so the spec→code linkage shape is identical across paths.
    """
    import html as _html
    if not script:
        return ""
    # Invert step_line_map → {line_idx: step_idx}
    line_to_step: dict = {}
    for step_idx, line_idxs in (step_line_map or {}).items():
        # JSON keys are strings after serialization; coerce to int.
        try:
            si = int(step_idx)
        except (TypeError, ValueError):
            continue
        for li in line_idxs or []:
            try:
                line_to_step[int(li)] = si
            except (TypeError, ValueError):
                continue
    out = []
    for li, line in enumerate(script.split("\n")):
        escaped = _html.escape(line)
        if li in line_to_step:
            tag = f' data-script-step="s{line_to_step[li]}"'
            out.append(f'<span class="code-line"{tag}>{escaped}</span>')
        else:
            out.append(f'<span class="code-line">{escaped}</span>')
    return "\n".join(out)


def _collect_lab_state_rows(spec) -> list:
    """Walk spec.initial_contents and prefilled_labware; return one row
    per pre-run lab-state entry the user should see audited.

    Per ADR-0011 Phase 2c: the lab-state panel is the audit surface for
    "what's in the lab before the protocol runs." Replay-mode renders
    the rows static (read-only); future live-mode (Phase 3) makes the
    same shape editable. Phase 4 polish: each row carries
    `target_prov_ids` for cross-column hover-highlighting.

    Pre:    `spec` is a ProtocolSpec or CompleteProtocolSpec, post-validation
            (the version visible in column 4). May be None when no spec
            event was captured.
    Post:   Returns a list of dicts:
              {
                "labware":          str,
                "well":             str,        # well name or empty for prefilled-labware rows
                "substance":        str,
                "volume_ul":        Optional[float],
                "kind":             str,        # "well" or "prefilled" — for downstream rendering
                "target_prov_ids":  str,        # space-separated, for data-target-cells
              }
            Empty list when spec is None or has no initial_contents +
            prefilled_labware. Initial_contents come first (per-well rows),
            then prefilled_labware (whole-labware uniform rows).
    Side effects: None.
    """
    if spec is None:
        return []
    rows = []
    for ic in getattr(spec, "initial_contents", []):
        rows.append({
            "labware": ic.labware,
            "well": ic.well,
            "substance": ic.substance,
            "volume_ul": ic.volume_ul,
            "kind": "well",
            "target_prov_ids": " ".join(_lab_state_row_target_prov_ids(spec, ic.labware, ic.well)),
        })
    for pf in getattr(spec, "prefilled_labware", []):
        rows.append({
            "labware": pf.labware,
            "well": "",                  # prefilled = uniform across labware; no specific well
            "substance": pf.substance,
            "volume_ul": pf.volume_ul,
            "kind": "prefilled",
            "target_prov_ids": " ".join(_lab_state_row_target_prov_ids(spec, pf.labware, "")),
        })
    return rows


def _collect_labware_mapping_rows(spec) -> list:
    """Walk all LocationRefs in `spec.steps` and return the unique
    description → resolved_label mappings.

    Per ADR-0011 Phase 2c: the labware-mapping panel is the audit
    surface for "which config labware does each user-language description
    map to." Replays the work the legacy `_confirm_labware_assignments`
    does today. Phase 4 polish: each row carries `target_prov_ids` for
    cross-column hover-highlighting.

    Pre:    `spec` is a ProtocolSpec post-validation (column-4 version).
            May be None.
    Post:   Returns a list of dicts:
              {
                "description":     str,
                "resolved_label":  str|None,
                "target_prov_ids": str,         # space-separated, for data-target-cells
              }
            Deduped by `description` (first-seen wins on resolved_label —
            in practice the resolver guarantees consistent assignment per
            description). resolved_label may be None for refs the LLM
            resolver couldn't pick AND the orchestrator's
            LabwareAmbiguityDetector somehow didn't catch — should be
            rare; surfaced for visibility.
    Side effects: None.
    """
    if spec is None:
        return []
    seen = {}
    for step in getattr(spec, "steps", []):
        for ref in (getattr(step, "source", None), getattr(step, "destination", None)):
            if ref is None:
                continue
            desc = getattr(ref, "description", None)
            if not desc or desc in seen:
                continue
            seen[desc] = getattr(ref, "resolved_label", None)
    return [
        {
            "description": desc,
            "resolved_label": label,
            "target_prov_ids": " ".join(
                _labware_mapping_row_target_prov_ids(spec, desc, label)
            ),
        }
        for desc, label in seen.items()
    ]


def _atomic_provenance_stats(spec) -> dict:
    """Walk every atomic provenanced value across the spec and tally
    instruction-sourced vs non-instruction-sourced. Surfaced in the report
    header so readers can see how much of the spec was literally lifted
    vs filled in by the model.
    """
    total = 0
    non_instr = 0
    atomic = ("volume", "substance", "duration", "temperature")
    for step in spec.steps:
        for fname in atomic:
            field = getattr(step, fname, None)
            if field is None:
                continue
            prov = getattr(field, "provenance", None)
            if prov is None:
                continue
            total += 1
            if getattr(prov, "source", None) != "instruction":
                non_instr += 1
        for role in ("source", "destination"):
            ref = getattr(step, role, None)
            if ref is None:
                continue
            for prov_attr in ("description_provenance", "wells_provenance"):
                prov = getattr(ref, prov_attr, None)
                if prov is None:
                    continue
                total += 1
                if getattr(prov, "source", None) != "instruction":
                    non_instr += 1
    pct = (non_instr * 100.0 / total) if total else 0.0
    return {"total": total, "non_instr": non_instr, "non_instr_pct": pct}


_TRACKED_FIELDS_ORDER = (
    "volume", "source", "destination", "substance", "duration", "temperature",
)

# Per-action expectation: which tracked fields the action EXPECTS to use.
# When a step's field is null AND the field is in this set, the renderer
# emits a ✗ placeholder cell carrying the field's data-prov-id (so
# resolution arrows have an origin anchor and panel-row hover has a
# target). Fields not in the set are skipped entirely when null — that
# avoids cluttering pause/comment/module-command step blocks with N
# unrelated ✗ rows.
_ACTION_EXPECTED_FIELDS = {
    "transfer":             {"volume", "source", "destination", "substance"},
    "distribute":           {"volume", "source", "destination", "substance"},
    "consolidate":          {"volume", "source", "destination", "substance"},
    "serial_dilution":      {"volume", "source", "destination", "substance"},
    "mix":                  {"volume"},
    "aspirate":             {"volume", "source"},
    "dispense":             {"volume", "destination"},
    "blow_out":             {"destination"},
    "touch_tip":            {"destination"},
    "delay":                {"duration"},
    "pause":                set(),  # duration OR note — neither rendered as a tracked cell
    "comment":              set(),
    "set_temperature":      {"temperature"},
    "wait_for_temperature": {"temperature"},
    "engage_magnets":       set(),
    "disengage_magnets":    set(),
    "deactivate":           set(),
}


def _empty_field_row(prov_id: str, label: str) -> Dict[str, Any]:
    """Return a detail-row dict for a tracked field that's null on this
    step but expected by the action — the value cell shows a ✗ placeholder
    carrying the field's prov_id (so resolution arrows / panel-row hover
    have an anchor when the field eventually gets filled in)."""
    value_html = (
        f'<span class="cell-empty" data-prov-id="{prov_id}">'
        f'✗ <em>(not extracted)</em>'
        f'</span>'
    )
    return {
        "label": label,
        "value_html": value_html,
        "check": "",
        "indented": False,
        "prov_id": prov_id,
        "is_empty": True,
    }


def _step_to_render_dict(step, step_idx: int = 0, instruction: Optional[str] = None,
                         inline_checks: Optional[Dict[str, str]] = None) -> dict:
    """Convert an ExtractedStep into the dict shape the template expects.

    `instruction` (when provided) lets `_render_provenanced_value` decide
    whether each cited_text is actually recoverable. If a cite isn't
    present in the instruction we degrade gracefully — color the value
    but omit `data-prov-id` so we don't promise a broken hover link.

    `inline_checks` (P1-8) is the {detail_label: phrase} dict for THIS
    step's passing constraint checks. When provided, each detail line
    whose logical label matches a key gets a `<span class="inline-check">`
    appended (the green ✓ tag next to the value). Pass None for
    columns that don't surface constraint outcomes (extracted, resolved).
    Recognized labels: "volume" / "source_labware" / "source_wells" /
    "destination_labware" / "destination_wells".

    Per ADR-0011 Phase 2b/4 polish (2026-05-05): for each action,
    `_ACTION_EXPECTED_FIELDS` enumerates which tracked fields a step
    *should* have. For each tracked field:
      * non-null     → render the value cell (existing behavior)
      * null + expected by action → render ✗ placeholder with matching prov_id
      * null + NOT expected → skip entirely (no clutter)
    This keeps the extracted-spec column and resolved-spec column
    structurally parallel for fields the orchestrator might fill in,
    enabling resolution arrows + cross-column hover.
    """
    comp = step.composition_provenance
    grounding = list(getattr(comp, "grounding", []))
    sid = f"s{step_idx}"

    def _check_phrase(label: str) -> str:
        """Return the plain-text passing-check phrase for `label`, or ''
        when no check applies. The template wraps it with the ✓ glyph +
        styling — this helper just provides the text content."""
        if not inline_checks:
            return ""
        phrase = inline_checks.get(label)
        return phrase or ""

    def _row(label, value_html, *, check="", indented=False,
              prov_id=None, is_empty=False):
        """Build one detail-row dict. The template renders these as
        cells in a 3-column grid (label / value / check)."""
        return {
            "label": label,
            "value_html": value_html,
            "check": check,
            "indented": indented,
            "prov_id": prov_id,
            "is_empty": is_empty,
        }

    detail_lines = []
    expected = _ACTION_EXPECTED_FIELDS.get(step.action, set())

    if step.volume is not None:
        v = _render_revisioned_value(
            step.volume,
            lambda f: f"{f.value} {f.unit}",
            lambda f: f.provenance,
            prov_id=f"{sid}-volume",
            instruction=instruction,
        )
        detail_lines.append(_row("volume", v, check=_check_phrase("volume"),
                                  prov_id=f"{sid}-volume"))
    elif "volume" in expected:
        detail_lines.append(_empty_field_row(f"{sid}-volume", "volume"))

    # source / destination render as TWO rows each: a labware row
    # (description + resolved_label bracket) and an indented wells row.
    # Each row's value_html surfaces its OWN sub-provenance for hover.
    for role, role_ref in (("source", step.source), ("destination", step.destination)):
        if role_ref is not None:
            desc_prov = role_ref.description_provenance
            if desc_prov:
                v_lab = _render_revisioned_value(
                    role_ref,
                    _format_labware_label,
                    lambda lr: lr.description_provenance,
                    prov_id=f"{sid}-{role}",
                    instruction=instruction,
                )
            else:
                import html as _html
                v_lab = _html.escape(_format_labware_label(role_ref))
            detail_lines.append(_row(role, v_lab,
                                      check=_check_phrase(f"{role}_labware"),
                                      prov_id=f"{sid}-{role}"))

            wells_text = _format_wells_only(role_ref)
            if wells_text:
                wells_prov = getattr(role_ref, "wells_provenance", None)
                if wells_prov:
                    v_wells = _render_revisioned_value(
                        role_ref,
                        _format_wells_only,
                        lambda lr: lr.wells_provenance,
                        prov_id=f"{sid}-{role}-wells",
                        instruction=instruction,
                    )
                else:
                    import html as _html
                    v_wells = _html.escape(wells_text)
                detail_lines.append(_row("wells", v_wells, indented=True,
                                          check=_check_phrase(f"{role}_wells"),
                                          prov_id=f"{sid}-{role}-wells"))
        elif role in expected:
            detail_lines.append(_empty_field_row(f"{sid}-{role}", role))

    if step.substance is not None:
        v = _render_revisioned_value(
            step.substance,
            lambda f: f.value,
            lambda f: f.provenance,
            prov_id=f"{sid}-substance",
            instruction=instruction,
        )
        detail_lines.append(_row("substance", v, prov_id=f"{sid}-substance"))
    elif "substance" in expected:
        detail_lines.append(_empty_field_row(f"{sid}-substance", "substance"))

    if step.duration is not None:
        v = _render_revisioned_value(
            step.duration,
            lambda f: f"{f.value} {f.unit}",
            lambda f: f.provenance,
            prov_id=f"{sid}-duration",
            instruction=instruction,
        )
        detail_lines.append(_row("duration", v, prov_id=f"{sid}-duration"))
    elif "duration" in expected:
        detail_lines.append(_empty_field_row(f"{sid}-duration", "duration"))

    if step.temperature is not None:
        v = _render_revisioned_value(
            step.temperature,
            lambda f: f"{f.value}°C",
            lambda f: f.provenance,
            prov_id=f"{sid}-temperature",
            instruction=instruction,
        )
        detail_lines.append(_row("temperature", v, prov_id=f"{sid}-temperature"))
    elif "temperature" in expected:
        detail_lines.append(_empty_field_row(f"{sid}-temperature", "temperature"))

    if step.replicates is not None:
        detail_lines.append(_row("replicates", f"{step.replicates}×"))

    # Post-actions (mix / blow_out / touch_tip applied AFTER the main
    # action). Each post-action emits one row; the value cell combines
    # the repetition count and the volume span.
    post_actions = getattr(step, "post_actions", None) or []
    for pa_idx, pa in enumerate(post_actions):
        bits = []
        if pa.repetitions is not None:
            bits.append(f"×{pa.repetitions}")
        if pa.volume is not None:
            v = _render_revisioned_value(
                pa.volume,
                lambda f: f"{f.value} {f.unit}",
                lambda f: f.provenance,
                prov_id=f"{sid}-{pa.action}-{pa_idx}-volume",
                instruction=instruction,
            )
            bits.append(v)
        rhs = " ".join(bits) if bits else '<em class="dim">(no parameters)</em>'
        detail_lines.append(_row(pa.action, rhs))

    # Surface the step's `note` field as a footnote inside the step block.
    # Pause/comment steps usually carry their content here (e.g. the
    # incubation message, the comment text, the user-action prompt) — without
    # this, those step blocks render with no body at all.
    note = getattr(step, "note", None)
    if note:
        import html as _html
        note_line = f'<div class="step-note">{_html.escape(note)}</div>'
    else:
        note_line = None

    return {
        "order": step.order,
        "action": step.action,
        # Q1 (step existence): the verbatim phrase that triggered this step kind
        "step_cited_text": comp.step_cited_text,
        "step_reasoning": comp.step_reasoning,         # only set when grounding has domain_default
        # Hash the step_cited_text to a palette slot so the tooltip's
        # blockquote left-bar uses the step's signature hue.
        "step_palette_class": _palette_class(comp.step_cited_text),
        # Q2 (parameter cohesion): how the values fit together as one step
        "parameters_cited_texts": list(comp.parameters_cited_texts),
        "parameters_reasoning": comp.parameters_reasoning,
        "confidence": f"{comp.confidence:.0%}",
        "step_class": _step_class(grounding),
        "grounding": grounding,
        "detail_lines": detail_lines,
        "note_line": note_line,
        # The composite step trigger's prov_id — the arrow lands on the step block.
        "step_trigger_prov_id": f"{sid}-step-trigger",
    }


class HTMLReporter(CapturingReporter):
    """Buffers events; at finalize(), renders a self-contained HTML report.

    The output file is one HTML document — inline CSS, no external resources,
    no JS. Open it in any browser. Self-contained means you can email it,
    commit it to a repo, or link it from a portfolio.

    Pre:    Constructed with `output_path` (where the HTML file will be
            written at finalize()).

    Post:   Each emit() collects a StageEvent (inherited from
            CapturingReporter). finalize() reads the buffered events,
            extracts instruction / spec / complete_spec / generated_script
            from them, walks the provenance fields, renders the Jinja2
            template to `self.output_path`.
    """

    def __init__(self, output_path: str):
        super().__init__()
        self.output_path = output_path

    def finalize(self) -> None:
        from datetime import datetime
        from pathlib import Path

        try:
            from jinja2 import Environment, FileSystemLoader, select_autoescape
        except ImportError:
            print("HTMLReporter requires jinja2: pip install 'nl2protocol[reporting]'")
            return

        # Find the template file relative to this module.
        template_dir = Path(__file__).parent / "reporting_templates"
        env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=select_autoescape(["html"]),
        )
        template = env.get_template("report.html.jinja")

        # Pull the load-bearing data out of the captured events.
        # Phase 3b collapse: all three spec events reference the SAME
        # mutated spec object (events are captured by reference, the spec
        # is mutated in place). By finalize() time they all point to the
        # final state. We pick the most-resolved available and render it
        # once into the unified "Protocol Steps" column; the chain UI on
        # each cell surfaces the per-field temporal evolution that the
        # legacy three-column layout used to convey.
        instruction_event = self.first_of_kind("raw_instruction")
        spec_event = self.first_of_kind("extracted_spec")
        resolved_spec_event = self.first_of_kind("resolved_spec")
        completed_spec_event = self.first_of_kind("completed_spec")
        script_event = self.first_of_kind("generated_script")
        # Phase 3i (#72): summary banner above the validated-spec column
        # showing what the constraint checker found. Mirrors what live
        # mode displays via JS on constraint_check_done; static path
        # captures the same event from the buffered list.
        constraint_event = self.first_of_kind("constraint_check_done")
        constraint_summary = None
        if constraint_event is not None:
            cdata = constraint_event.data or {}
            raw_warnings = list(cdata.get("warnings", []))
            # P1-9: route warnings by step. Per-step warnings are
            # surfaced inline at their step row; global / protocol-level
            # warnings stay in the top banner's <ul>. The original
            # `warnings` field is preserved for any downstream consumer
            # that depends on the flat list.
            per_step_warnings, global_warnings = _split_warnings_by_step(raw_warnings)
            # P1-8: route passed-check records by step + detail_label so
            # the template can stamp inline green tags next to value
            # rows. Each (step, label) cell from the checker becomes one
            # entry in the per-step dict.
            raw_passes = list(cdata.get("passed_checks", []))
            inline_checks_by_step = _passes_to_inline_checks(raw_passes)
            constraint_summary = {
                "violation_count": cdata.get("violation_count", 0),
                "warnings": raw_warnings,
                "warnings_by_step": per_step_warnings,
                "warnings_global": global_warnings,
                "passed_checks": raw_passes,
                "inline_checks_by_step": inline_checks_by_step,
            }

        instruction = (instruction_event.data.get("instruction", "")
                       if instruction_event else "")
        # Phase 3b collapse: ONE unified step list rendered into a single
        # "Protocol Steps" column. Pick the most-resolved spec available
        # (completed > resolved > extracted). Since the captured events
        # all reference the same mutated spec object by finalize() time,
        # they all point to the final state; the .get() chain is just a
        # graceful fallback if a stage was skipped (failed run).
        spec = spec_event.data.get("spec") if spec_event else None
        rspec = resolved_spec_event.data.get("spec") if resolved_spec_event else None
        cspec = completed_spec_event.data.get("spec") if completed_spec_event else None
        unified_spec = cspec or rspec or spec

        protocol_steps = []
        if unified_spec:
            # P1-8: pass per-step inline-checks at construction so the
            # green ✓ tags get embedded into the right detail_lines.
            inline_by_step = (
                constraint_summary.get("inline_checks_by_step")
                if constraint_summary else None
            ) or {}
            protocol_steps = [
                _step_to_render_dict(
                    s, idx, instruction,
                    inline_checks=inline_by_step.get(s.order),
                )
                for idx, s in enumerate(unified_spec.steps)
            ]

        # P1-9: attach per-step warnings to each step dict so the template
        # can render them inline below the step body.
        if constraint_summary and protocol_steps:
            per_step_warnings = constraint_summary.get("warnings_by_step") or {}
            per_step_inline = constraint_summary.get("inline_checks_by_step") or {}
            for step_dict in protocol_steps:
                order = step_dict.get("order")
                if order is None:
                    continue
                if order in per_step_warnings:
                    step_dict["warnings"] = per_step_warnings[order]
                if order in per_step_inline:
                    step_dict["inline_checks"] = per_step_inline[order]

        # Build the marked-up instruction HTML with <span data-cite-id="...">
        # wrappers for arrow rendering. Use the most-resolved spec available
        # (validated > resolved > extracted) so cite-position resolution sees
        # the most accurate value set; cite spans themselves don't change
        # across snapshots since they reference instruction text only.
        spec_for_marks = cspec or rspec or spec
        if spec_for_marks and instruction:
            arrow_targets = _collect_arrow_targets(spec_for_marks)
            instruction_html = _render_instruction_with_marks(instruction, arrow_targets)
        else:
            import html as _html
            instruction_html = _html.escape(instruction)

        generated_script = (script_event.data.get("script", "")
                            if script_event else "")
        # Phase 3i (#72): step→script-line map for the hover-pair
        # linkage between Validated Spec blocks and script lines.
        # Empty dict when no script_event captured.
        script_step_line_map = (script_event.data.get("step_line_map", {})
                                 if script_event else {})
        generated_script_html = _render_script_with_step_tags(
            generated_script, script_step_line_map,
        )

        success = bool(script_event)  # if we got to script generation, the run succeeded

        # Atomic-provenance stats use the most-resolved spec available.
        # Surfaced in the header.
        stats_spec = cspec or rspec or spec
        prov_stats = _atomic_provenance_stats(stats_spec) if stats_spec else {"total": 0, "non_instr": 0, "non_instr_pct": 0.0}

        # ADR-0011 Phase 2b: resolution arrows for the storytelling layer.
        # Embedded as JSON so the template's JS can iterate them at render
        # time and draw an SVG path per arrow (extracted column → resolved
        # column). Skipped/aborted resolutions and unmappable field paths
        # are filtered out by _collect_resolution_arrows.
        import json as _json
        resolution_arrows_json = _json.dumps(_collect_resolution_arrows(self.events))

        # ADR-0011 Phase 2c: bulk-defaults panels — read-only audit
        # summaries below the column grid. Lab-state walks the validated
        # spec's initial_contents (+ prefilled_labware); labware-mapping
        # walks all LocationRefs and dedupes by description.
        lab_state_rows = _collect_lab_state_rows(cspec or rspec)
        labware_mapping_rows = _collect_labware_mapping_rows(cspec or rspec)

        rendered = template.render(
            instruction=instruction,                # raw text (for any text-only fallback)
            instruction_html=instruction_html,      # marked-up HTML with cite spans
            # Phase 3b: single unified "Protocol Steps" column. Legacy
            # template var names left unwired (template no longer
            # references spec_steps / resolved_steps / validated_steps).
            protocol_steps=protocol_steps,
            generated_script=generated_script,
            success=success,
            prov_stats=prov_stats,
            resolution_arrows_json=resolution_arrows_json,
            lab_state_rows=lab_state_rows,
            labware_mapping_rows=labware_mapping_rows,
            constraint_summary=constraint_summary,
            script_step_line_map=script_step_line_map,
            generated_script_html=generated_script_html,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.output_path).write_text(rendered, encoding="utf-8")
