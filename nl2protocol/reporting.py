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

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Literal, Optional, Protocol


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
    "extracted_spec",       # data = {"spec": ProtocolSpec}        — column 2 (post-extraction, immutable LLM output)
    "resolved_spec",        # data = {"spec": ProtocolSpec}        — column 3 (post-orchestrator gap resolution; ADR-0011 Phase 2a)
    "completed_spec",       # data = {"spec": CompleteProtocolSpec} — column 4 (post-validation: labware confirm + constraint check)
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


def _palette_class(cited_text: str) -> str:
    """Deterministic hash of `cited_text` → one of `palette-0` .. `palette-N`.

    md5-based so the same cite always lands on the same hue across runs
    (Python's built-in hash() randomizes per-process via PYTHONHASHSEED).
    """
    import hashlib
    if not cited_text:
        return "palette-0"
    digest = hashlib.md5(cited_text.encode("utf-8")).digest()
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

    Linkage rules (graceful degrade for unrecoverable cites):
      - data-prov-id is emitted ONLY when an instruction-sourced cited_text
        was actually located in `instruction`. If the LLM produced a cite
        that doesn't appear verbatim, we DON'T promise a hover link we
        cannot deliver — the value still gets its color but no data-prov-id.
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

    # Decide whether the cite is recoverable (drives both palette + linkage).
    cite_recoverable = False
    if source == "instruction" and cited_text:
        if instruction is None:
            # Caller didn't pass instruction — assume recoverable (legacy).
            cite_recoverable = True
        else:
            cite_recoverable = _find_cite_position(instruction, cited_text) is not None

    if cite_recoverable and cited_text:
        classes.append(_palette_class(cited_text))

    # Review-lifecycle class. The CSS pseudo-element on this class draws
    # the inline ⚠ / ✎ badge for the two attention-worthy states; other
    # non-original states get the class but render no inline badge.
    if review_status != "original":
        classes.append(f"prov-review-{review_status.replace('_', '-')}")

    rendered_value = html.escape(str(value))

    # data-prov-id ONLY when we can actually link to a cite span.
    extra_attrs = ""
    if prov_id and cite_recoverable:
        extra_attrs = f' data-prov-id="{html.escape(prov_id, quote=True)}"'

    # Stash provenance fields on the element so the JS tooltip can read them
    # without us having to compute / escape a long native title= string.
    extra_attrs += f' data-prov-source="{html.escape(source, quote=True)}"'
    extra_attrs += f' data-prov-confidence="{confidence:.2f}"'
    extra_attrs += f' data-prov-review-status="{html.escape(review_status, quote=True)}"'
    if cited_text:
        extra_attrs += f' data-prov-cited="{html.escape(cited_text, quote=True)}"'
    if positive_reasoning:
        extra_attrs += f' data-prov-positive-reasoning="{html.escape(positive_reasoning, quote=True)}"'
        # Backwards-compat alias for any external consumer that still reads
        # the legacy single-reasoning attr (kept until ADR-0010).
        extra_attrs += f' data-prov-reasoning="{html.escape(positive_reasoning, quote=True)}"'
    if why_not_in_instruction:
        extra_attrs += f' data-prov-why-not-in-instruction="{html.escape(why_not_in_instruction, quote=True)}"'
    if reviewer_objection:
        extra_attrs += f' data-prov-reviewer-objection="{html.escape(reviewer_objection, quote=True)}"'

    # Non-instruction atoms get a ▴ marker so readers can spot at a glance
    # which values were filled in by the model vs literally lifted from
    # the instruction. The marker is documented in the legend.
    marker = "" if source == "instruction" else '<span class="non-instr-marker" aria-hidden="true">▴</span>'

    return f'{marker}<span class="{" ".join(classes)}"{extra_attrs}>{rendered_value}</span>'


# ============================================================================
# Phase 3: Span recovery — find cited_text positions in the instruction.
# ============================================================================

def _normalize_for_match(s: str) -> str:
    """Lowercase + collapse whitespace. Used for substring matching where
    minor spacing differences shouldn't cause misses."""
    import re
    return re.sub(r"\s+", " ", s.lower()).strip()


def _find_cite_position(instruction: str, cited_text: str) -> Optional[tuple]:
    """Find the (start, end) char offsets where `cited_text` appears in
    `instruction`. Case-insensitive. Returns None if not found.

    First-match-wins for substrings that appear multiple times. Phase 3
    accepts this as a known simplification — multi-cite disambiguation
    could be a future improvement (e.g. carry the LLM's character offset
    if/when the prompt asks for it).
    """
    import re
    if not cited_text:
        return None
    # Try exact case-insensitive match first.
    pattern = re.compile(re.escape(cited_text), re.IGNORECASE)
    m = pattern.search(instruction)
    if m:
        return m.start(), m.end()
    # Fallback: collapse whitespace on both sides and try again.
    norm_instruction = _normalize_for_match(instruction)
    norm_cite = _normalize_for_match(cited_text)
    pattern2 = re.compile(re.escape(norm_cite))
    m2 = pattern2.search(norm_instruction)
    if m2:
        # Map back to the original instruction by counting chars.
        # Approximate: use the same offsets (acceptable for whitespace-only diffs).
        approx_start = m2.start()
        approx_end = m2.end()
        # Clamp to valid range — instruction may be shorter due to original whitespace.
        approx_end = min(approx_end, len(instruction))
        return approx_start, approx_end
    return None


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
        for field_name, field_obj in [
            ("volume", step.volume),
            ("substance", step.substance),
            ("duration", step.duration),
            ("temperature", step.temperature),
            ("source", step.source),
            ("destination", step.destination),
        ]:
            if field_obj is None:
                continue
            prov = getattr(field_obj, "provenance", None)
            if prov is None:
                continue
            if getattr(prov, "source", None) != "instruction":
                continue
            cited = getattr(prov, "cited_text", None)
            if not cited:
                continue
            targets.append({
                "prov_id": f"{sid}-{field_name}",
                "cited_text": cited,
                "kind": "atomic-color",
                "palette_class": _palette_class(cited),
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
    m = re.match(r"steps\[(\d+)\]\.(\w+)(?:\.\w+)?$", field_path)
    if m and m.group(2) in _PROV_ID_RENDERABLE_FIELDS:
        return f"s{m.group(1)}-{m.group(2)}"
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
    for step in spec.steps:
        for fname in ("volume", "substance", "duration", "temperature", "source", "destination"):
            field = getattr(step, fname, None)
            if field is None:
                continue
            prov = getattr(field, "provenance", None)
            if prov is None:
                continue
            total += 1
            if getattr(prov, "source", None) != "instruction":
                non_instr += 1
    pct = (non_instr * 100.0 / total) if total else 0.0
    return {"total": total, "non_instr": non_instr, "non_instr_pct": pct}


def _step_to_render_dict(step, step_idx: int = 0, instruction: Optional[str] = None) -> dict:
    """Convert an ExtractedStep into the dict shape the template expects.

    `instruction` (when provided) lets `_render_provenanced_value` decide
    whether each cited_text is actually recoverable. If a cite isn't
    present in the instruction we degrade gracefully — color the value
    but omit `data-prov-id` so we don't promise a broken hover link.
    """
    comp = step.composition_provenance
    grounding = list(getattr(comp, "grounding", []))
    sid = f"s{step_idx}"

    detail_lines = []

    if step.volume is not None:
        v = _render_provenanced_value(
            f"{step.volume.value} {step.volume.unit}",
            step.volume.provenance,
            prov_id=f"{sid}-volume",
            instruction=instruction,
        )
        detail_lines.append(f'<span class="label">volume:</span> {v}')

    if step.source is not None:
        loc_text = _format_location(step.source)
        if step.source.provenance:
            v = _render_provenanced_value(loc_text, step.source.provenance, prov_id=f"{sid}-source", instruction=instruction)
        else:
            import html as _html
            v = _html.escape(loc_text)
        detail_lines.append(f'<span class="label">source:</span> {v}')

    if step.destination is not None:
        loc_text = _format_location(step.destination)
        if step.destination.provenance:
            v = _render_provenanced_value(loc_text, step.destination.provenance, prov_id=f"{sid}-destination", instruction=instruction)
        else:
            import html as _html
            v = _html.escape(loc_text)
        detail_lines.append(f'<span class="label">destination:</span> {v}')

    if step.substance is not None:
        v = _render_provenanced_value(step.substance.value, step.substance.provenance, prov_id=f"{sid}-substance", instruction=instruction)
        detail_lines.append(f'<span class="label">substance:</span> {v}')

    if step.duration is not None:
        v = _render_provenanced_value(
            f"{step.duration.value} {step.duration.unit}",
            step.duration.provenance,
            prov_id=f"{sid}-duration",
            instruction=instruction,
        )
        detail_lines.append(f'<span class="label">duration:</span> {v}')

    if step.temperature is not None:
        v = _render_provenanced_value(
            f"{step.temperature.value}°C",
            step.temperature.provenance,
            prov_id=f"{sid}-temperature",
            instruction=instruction,
        )
        detail_lines.append(f'<span class="label">temperature:</span> {v}')

    if step.replicates is not None:
        detail_lines.append(f'<span class="label">replicates:</span> {step.replicates}×')

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
        # ADR-0011 Phase 2a: three spec snapshots feed three columns.
        #   spec_event       → column 2 (extracted, post-LLM)
        #   resolved_event   → column 3 (resolved, post-orchestrator)
        #   completed_event  → column 4 (validated, post-CompleteProtocolSpec promotion)
        # Resolved or completed missing → render the columns empty; downstream
        # tests check both column headers exist regardless.
        instruction_event = self.first_of_kind("raw_instruction")
        spec_event = self.first_of_kind("extracted_spec")
        resolved_spec_event = self.first_of_kind("resolved_spec")
        completed_spec_event = self.first_of_kind("completed_spec")
        script_event = self.first_of_kind("generated_script")

        instruction = (instruction_event.data.get("instruction", "")
                       if instruction_event else "")
        spec_steps = []
        spec = spec_event.data.get("spec") if spec_event else None
        if spec:
            spec_steps = [_step_to_render_dict(s, idx, instruction) for idx, s in enumerate(spec.steps)]

        resolved_steps = []
        rspec = resolved_spec_event.data.get("spec") if resolved_spec_event else None
        if rspec:
            resolved_steps = [_step_to_render_dict(s, idx, instruction) for idx, s in enumerate(rspec.steps)]

        validated_steps = []
        cspec = completed_spec_event.data.get("spec") if completed_spec_event else None
        if cspec:
            validated_steps = [_step_to_render_dict(s, idx, instruction) for idx, s in enumerate(cspec.steps)]

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
            spec_steps=spec_steps,
            resolved_steps=resolved_steps,
            validated_steps=validated_steps,
            generated_script=generated_script,
            success=success,
            prov_stats=prov_stats,
            resolution_arrows_json=resolution_arrows_json,
            lab_state_rows=lab_state_rows,
            labware_mapping_rows=labware_mapping_rows,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.output_path).write_text(rendered, encoding="utf-8")
