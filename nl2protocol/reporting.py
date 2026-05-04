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
    "extracted_spec",       # data = {"spec": ProtocolSpec}
    "completed_spec",       # data = {"spec": CompleteProtocolSpec}
    "generated_script",     # data = {"script": "..."}
    "warning",              # data = {"message": "...", "context": "..."}
    "error",                # data = {"message": "...", "stage": "..."}
    "info",                 # data = {"message": "..."}  — short status notes
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


def _render_provenanced_value(value: Any, prov, label: str = "") -> str:
    """Wrap a provenanced value in an HTML span colored by its source.

    `prov` is a Provenance dataclass (has .source, .reason, .confidence).
    The `title` attribute (HTML hover tooltip) shows the reason + confidence.
    """
    import html
    source = getattr(prov, "source", "inferred")
    reason = getattr(prov, "reason", "")
    confidence = getattr(prov, "confidence", 0.0)
    css_class = _PROV_CLASS.get(source, "prov-inferred")
    tooltip = f"{source} (confidence {confidence:.0%}): {reason}"
    rendered_value = html.escape(str(value))
    rendered_tooltip = html.escape(tooltip, quote=True)
    return f'<span class="{css_class}" title="{rendered_tooltip}">{rendered_value}</span>'


def _step_class(grounding: List[str], is_orphan: bool) -> str:
    """CSS class for a step block based on its CompositionProvenance grounding."""
    if is_orphan:
        return "step-orphan"
    if "instruction" in grounding and len(grounding) == 1:
        return "step-grounded-instruction"
    return "step-grounded-mixed"


def _step_to_render_dict(step) -> dict:
    """Convert an ExtractedStep into the dict shape the template expects.

    Walks the step's provenanced fields (volume, source, destination, etc.)
    and renders each one as a colored HTML span. Builds a list of detail
    lines that the template iterates over.

    A step is considered "orphan" (no instruction origin) when its
    composition_provenance.grounding does NOT include "instruction" —
    e.g. grounding=["domain_default"] alone means the LLM added this step
    purely from domain knowledge. In Phase 3 these steps will get no
    composite arrow back to the instruction column.
    """
    comp = step.composition_provenance
    grounding = list(getattr(comp, "grounding", []))
    is_orphan = "instruction" not in grounding

    detail_lines = []

    if step.volume is not None:
        v = _render_provenanced_value(
            f"{step.volume.value} {step.volume.unit}",
            step.volume.provenance,
        )
        detail_lines.append(f'<span class="label">volume:</span> {v}')

    if step.source is not None:
        bits = []
        if step.source.well:
            bits.append(f"well {step.source.well}")
        if step.source.wells:
            bits.append(f"wells [{', '.join(step.source.wells[:6])}{'...' if len(step.source.wells) > 6 else ''}]")
        if step.source.well_range:
            bits.append(step.source.well_range)
        loc_text = f"{step.source.description} ({', '.join(bits)})" if bits else step.source.description
        if step.source.provenance:
            v = _render_provenanced_value(loc_text, step.source.provenance)
        else:
            import html as _html
            v = _html.escape(loc_text)
        detail_lines.append(f'<span class="label">source:</span> {v}')

    if step.destination is not None:
        bits = []
        if step.destination.well:
            bits.append(f"well {step.destination.well}")
        if step.destination.wells:
            bits.append(f"wells [{', '.join(step.destination.wells[:6])}{'...' if len(step.destination.wells) > 6 else ''}]")
        if step.destination.well_range:
            bits.append(step.destination.well_range)
        loc_text = f"{step.destination.description} ({', '.join(bits)})" if bits else step.destination.description
        if step.destination.provenance:
            v = _render_provenanced_value(loc_text, step.destination.provenance)
        else:
            import html as _html
            v = _html.escape(loc_text)
        detail_lines.append(f'<span class="label">destination:</span> {v}')

    if step.substance is not None:
        v = _render_provenanced_value(step.substance.value, step.substance.provenance)
        detail_lines.append(f'<span class="label">substance:</span> {v}')

    if step.duration is not None:
        v = _render_provenanced_value(
            f"{step.duration.value} {step.duration.unit}",
            step.duration.provenance,
        )
        detail_lines.append(f'<span class="label">duration:</span> {v}')

    if step.temperature is not None:
        v = _render_provenanced_value(
            f"{step.temperature.value}°C",
            step.temperature.provenance,
        )
        detail_lines.append(f'<span class="label">temperature:</span> {v}')

    if step.replicates is not None:
        detail_lines.append(f'<span class="label">replicates:</span> {step.replicates}×')

    return {
        "order": step.order,
        "action": step.action,
        "justification": comp.justification,
        "confidence": f"{comp.confidence:.0%}",
        "step_class": _step_class(grounding, is_orphan),
        "is_orphan": is_orphan,
        "detail_lines": detail_lines,
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
        instruction_event = self.first_of_kind("raw_instruction")
        spec_event = self.first_of_kind("extracted_spec")
        completed_spec_event = self.first_of_kind("completed_spec")
        script_event = self.first_of_kind("generated_script")

        instruction = (instruction_event.data.get("instruction", "")
                       if instruction_event else "")
        spec_steps = []
        if spec_event:
            spec = spec_event.data.get("spec")
            if spec:
                spec_steps = [_step_to_render_dict(s) for s in spec.steps]

        completed_steps = []
        if completed_spec_event:
            cspec = completed_spec_event.data.get("spec")
            if cspec:
                completed_steps = [_step_to_render_dict(s) for s in cspec.steps]

        generated_script = (script_event.data.get("script", "")
                            if script_event else "")

        success = bool(script_event)  # if we got to script generation, the run succeeded

        rendered = template.render(
            instruction=instruction,
            spec_steps=spec_steps,
            completed_steps=completed_steps,
            generated_script=generated_script,
            success=success,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.output_path).write_text(rendered, encoding="utf-8")
