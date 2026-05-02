# nl2protocol

A neuro-symbolic system that turns natural-language lab instructions into Opentrons OT-2 robot protocols. An LLM does the parts that require interpretation — extracting structured intent from language, judging final correctness against the original instruction. Deterministic Python does the parts that require precision — hardware mapping, code generation, simulation.

Every value the LLM produces carries a structured provenance citation back to its source. The resulting spec is machine-checked against the user's instruction before any code is generated, and the user is asked to confirm exactly the values that the system cannot verify on its own.

This is a CLI-native interactive pipeline, not a batch converter: stage-by-stage progress streams to the user, and uncertain values (inferred fields, weak domain defaults, ambiguous labware mappings) are dynamically routed to per-item confirmation rather than silently accepted.

## Demo

Real stdout from a run on a 28-line bacterial transformation protocol (4 plasmids, temperature module heat-shock cycle, dual pipettes). Cut for length — full uncropped output: [`examples/03_bacterial_transformation/run_output.txt`](examples/03_bacterial_transformation/run_output.txt).

```
$ nl2protocol -i instruction.txt -c lab_config.json

────────────────────────────────────────────────────────────
[Stage 1/8] Validating instruction and config...
  Config validated.
  Classifying input...
  Input validated as protocol instruction.

────────────────────────────────────────────────────────────
[Stage 2/8] Reasoning through protocol instruction...
  Protocol type: heat_shock_bacterial_transformation
  Steps: 13
  Reasoning:
    Bacterial transformation using heat shock. 4 plasmids (A1-A4) and
    corresponding cells (B1-B4). Explicit volumes 2/20/100/200uL, temps
    4°C/42°C, durations 30 min / 45 sec / 2 min / 1 hr. The 4 plasmid
    transfers share the same volume — compress to one step over B1-B4...

────────────────────────────────────────────────────────────
[Stage 3/8] Validating and completing specification...
  Specification is complete.
  Locked volumes: [2.0, 20.0, 50.0, 100.0, 200.0]

────────────────────────────────────────────────────────────
[Stage 3.5/8] Resolving labware references...
  All labware resolved.

────────────────────────────────────────────────────────────
[Stage 4/8] Checking hardware constraints...
  All constraints satisfied.

============================================================
PROTOCOL SPECIFICATION
============================================================
  STEPS:
    1. SET_TEMPERATURE 4.0°C
    2. TRANSFER 2.0uL of plasmid DNA from tube rack to wells B1-B4
    3. MIX 20.0uL at wells B1-B4
    4. PAUSE for 30.0 minutes
    ... (9 more: heat shock cycle, SOC recovery, final pauses)

  31 parameter(s) auto-accepted (instruction/config, confidence >= 0.7)
  NEEDS CONFIRMATION (2):
    [1] reagent_rack A1-A4, "plasmid DNA"      — no volume stated
    [2] reagent_rack C1, "SOC recovery medium" — no volume stated
============================================================

────────────────────────────────────────────────────────────
[Stage 5/8] Converting specification to protocol schema...
  Schema generated.
... (Stages 6-8: script generation, simulation, intent verification — all passed, confidence 95%)

────────────────────────────────────────────────
  Protocol generated successfully.
  Commands:   43 (8x transfer, 8x mix, 4x pause, 3x set_temperature, ...)
  Pipettes:   p20_single_gen2 (left), p300_single_gen2 (right)
  Output:     output/generated_protocol.py
```

Three things to notice in the run above:

- **Chain-of-thought reasoning is visible**, not hidden in a black box (Stage 2). You see what the LLM understood before it produced any structured output.
- **The protocol is presented in scientific terms** (`SET_TEMPERATURE 4.0°C`, `TRANSFER 2.0uL of plasmid DNA from tube rack to wells B1-B4`) before any hardware code is generated.
- **The system identifies only the values that need user input** — 2 items out of 13 steps, with 31 parameters auto-accepted — rather than asking the user to vet everything.

Three full input → output pairs are in [`examples/`](examples/).

## Quick start

Requires Python 3.10+ and an [Anthropic API key](https://console.anthropic.com/).

```bash
git clone https://github.com/aryan01104/nl2protocol.git
cd nl2protocol
python3 -m venv venv
source venv/bin/activate
pip install .

nl2protocol --setup                   # interactive: prompts for API key, writes .env
cp config_examples/lab_config.example.json lab_config.json

# Run with an inline instruction:
nl2protocol -i "Transfer 100uL from source_plate A1 to dest_plate B1" -c lab_config.json

# Or with both instruction and config as files (typical for real protocols):
nl2protocol -i my_protocol.txt -c lab_config.json
```

The `-i` flag accepts an inline string, a path to a `.txt` file, or a path to a `.pdf` file. For PDF input, install with `pip install ".[pdf]"`. For development, `pip install -e ".[dev]"`.

## What it does

The pipeline runs in 8 stages. Only 4 of them use an LLM, and each LLM call has a constrained job — none of them produce the final output script directly. The other 4 stages are deterministic Python and exist precisely so that user-specified values cannot be silently corrupted by the LLM at any point after extraction.

**Stage 1 — Input classification (LLM).** A short LLM call decides whether the user's input is a real protocol instruction, a question, an ambiguous statement, or something the OT-2 can't do (e.g., centrifugation, absorbance reading). Non-protocol inputs are rejected with a suggestion before any expensive extraction work.

**Stage 2 — Protocol extraction with reasoning (LLM).** The main extraction call. The LLM is instructed to think step-by-step inside `<reasoning>` tags before emitting a structured `<spec>` JSON. The output is a `ProtocolSpec` — a Pydantic-validated structure expressing the protocol in scientific terms (actions, substances, volumes, well descriptions, tip strategy) rather than hardware terms (no slot numbers, no API names, no pipette mounts yet). Every value in the spec carries a `Provenance` object identifying where it came from.

**Stage 3 — Provenance verification + sufficiency check (deterministic).** For each value the LLM tagged with `source="instruction"`, the system independently checks that the value actually appears in the user's instruction text. Mismatches are blocked as fabrication errors before they propagate. The sufficiency check then asks: does every step have everything it needs? If not, the system tries to fill missing fields from the lab config (e.g., resolve "the reservoir" to a specific reservoir), and if that fails, it asks the LLM to refine the spec with the gaps as feedback. Only fully-resolved specs proceed.

**Stage 3.5 — Labware reference resolution (LLM).** A small dedicated LLM call maps the user's labware descriptions ("tube rack", "the PCR plate") to specific labware labels in the user's lab config. The system displays the resolution to the user for confirmation before proceeding — ambiguous mappings get the user's input, obvious one-match cases proceed automatically.

**Stage 4 — Hardware constraint checking (deterministic).** Once the spec is resolved, a constraint checker validates it against the lab config: pipette range vs. volume, tip availability, labware-vs-module compatibility, well capacity vs. operations, mount conflicts. Hardware conflicts are surfaced with explanations rather than silently resolved (e.g., "step 3 needs 5uL but only the p300 is loaded; add a p20 or change the volume").

**Stage 5 — Spec-to-schema conversion (deterministic).** This is the critical stage. A pure-Python function maps the science-language `ProtocolSpec` to the hardware-language `ProtocolSchema`: labware labels become Opentrons API load_names, volumes become arguments to `transfer()` calls, well descriptions become well lists or ranges. Volumes are copied verbatim — there is no LLM in the path between the user's `100uL` and the schema's `aspirate(100, ...)`.

**Stages 6–7 — Script generation and simulation (deterministic).** A pure-Python template renders the schema into Opentrons Python code (same input always produces the same script). The script then runs through the actual Opentrons simulator, which catches hardware-level errors earlier stages can't see — invalid wells given a chosen labware definition, tip rack exhaustion at runtime, deck-coordinate conflicts, deprecated API patterns. Failures are blocking.

**Stage 8 — Intent verification (LLM).** A separate LLM call compares the generated protocol back to the user's original instruction and produces a confidence score plus a list of specific issues if any. This is a defense-in-depth layer, not a contract: it catches semantic mismatches the simulator can't see (e.g., "you asked for mix 10 times, the script mixes once") but it's an LLM judgment with its own noise.

The thread connecting all of this: the LLM is allowed to interpret natural language and produce a structured intermediate, and the LLM is allowed to evaluate the final output, but **the LLM is never in the path that translates verified spec into executable code.** That's a deliberate boundary.

## Architecture decisions

The following decisions shaped the system. Each section is self-contained; ADR links are for the full record.

### Provenance as a first-class schema feature

A standard Pydantic schema enforces shape — required fields, types, ranges, enums. It cannot enforce *meaning*. An LLM extraction can produce a perfectly well-formed `ProtocolSpec` whose 100uL transfer step refers to a value the user never wrote, or whose substance was hallucinated from context.

The fix is to require the LLM to attach a structured citation to every quantity it emits. In this codebase, every volume / duration / temperature / substance / labware reference is wrapped in a `Provenanced*` Pydantic type carrying a `Provenance` object: `{source, reason, confidence}`. The `source` field is the categorical signal — one of `instruction` (user wrote it verbatim), `domain_default` (standard practice for a named protocol), or `inferred` (LLM reasoning with no direct support). The `reason` field is a one-sentence justification, and `confidence` is a self-reported number.

The verifier then dispatches on `source`:

- `source="instruction"` claims are checked against the actual instruction text. The verifier extracts every numeric volume, well position, and duration from the text using regex, then confirms the value actually appears. Mismatches are flagged as fabrication errors and block the pipeline.
- `source="domain_default"` claims with high LLM-self-reported confidence are accepted silently (the LLM has cited a standard practice for a named protocol, and we have no ground-truth to check against beyond domain experts the system doesn't have access to). Weak `domain_default` claims are routed to user confirmation.
- `source="inferred"` claims are always routed to user confirmation. The system treats "LLM reasoning with no anchor" as inherently unverifiable.

The honest limit: provenance is generated by the same LLM that produces the values. It's not an external check. What the technique provides is structured verifiability for the subset of claims that *are* externally checkable (those grounded in the instruction text), and structured triage for the subset that aren't (delegated to the user). Full thinking in [ADR-0002](docs/adr/0002-provenanced-protocol-spec.md).

### Multi-stage pipeline with deterministic schema generation

An earlier version of the system used a single LLM call to produce the full Opentrons schema directly, with a retry loop on validation errors. This produced a specific class of silent failure: when a `10.5uL` value didn't fit the pipette the LLM had assigned, the next retry would change the volume to `20.0uL` to fit the pipette, instead of switching to a smaller pipette. The volume the user explicitly specified became collateral damage to the LLM's effort to produce a passing schema.

The current architecture splits this into two phases with a deterministic boundary between them:

- **Phase 1 (LLM):** the LLM produces a `ProtocolSpec` in *science language*. Actions, substances, volumes, well descriptions, tip strategy. No labware API names, no slot numbers, no pipette mounts. The LLM's job ends with a structured spec that a domain expert could read and verify.
- **Phase 2 (deterministic):** a pure-Python `spec_to_schema()` function maps the science-language spec to a hardware-language `ProtocolSchema` using the user's lab config. This function does pipette assignment by deterministic range check, labware resolution by config lookup, well expansion by parsing well descriptions into lists. There is no LLM in this function. Volumes are copied through unmodified.

The consequence is that values the user specified are protected by code structure, not by LLM compliance. A `10.5uL` in the user's instruction is `10.5uL` in the generated `transfer()` call. The pipeline becomes diagnosable: when something's wrong, you can point to which stage owns the failure (extraction lost meaning, constraints found a hardware conflict, the simulator caught a runtime issue) rather than having one monolithic LLM call to debug.

The cost is more code and more stages. The benefit is that the most expensive failure mode — silent corruption of a user-specified value — is no longer expressible by the architecture.

### Interactive CLI pipeline with structured user confirmation

A natural objection to the provenance system is: what about the claims it can't verify? `domain_default` and `inferred` values are LLM-generated with no external ground truth in the codebase. Why should a user trust them?

The system's commitment: **don't trust them — surface them.** Most LLM-to-code projects in this space are batch converters (input text in, code out, no intermediate user touchpoints). This project is CLI-native instead because lab-protocol generation is a domain where a scientist's interpretation matters at multiple points along the pipeline — labware mappings can be ambiguous, named protocols invoke domain defaults, inferred values warrant a second look. Treating the whole flow as one text-to-code transformation hides exactly the decisions the user would want visibility into.

So the pipeline streams stage-by-stage progress to stderr (stdout stays clean for the generated script), and at specific decision points pauses to ask the user about claims the verifier could not establish on its own. For example, when a Bradford spec includes a 5-min incubation tagged `domain_default` with confidence 0.6:

```
[3] Step 4 INCUBATE 5 min — Bradford assay standard practice
    (domain_default, confidence 0.6, "standard Bradford post-mix incubation")
    Accept / edit / skip?
```

The routing is by source and confidence, not by guess: `inferred` claims always need confirmation; weak `domain_default` claims need confirmation; strong `domain_default` and verified `instruction` claims pass silently. Similar items are grouped (e.g., 8 wells in a column with no stated volume become one prompt, not 8). A 17-step protocol might surface 3 review items, not 17 — users review only what's actually uncertain.

There is a non-interactive mode for CI / scripting (when stdin is not a TTY, the pipeline auto-accepts safe defaults and refuses to generate when ambiguity is unresolvable), but the design assumes a human in the loop by default because that's where the verification mechanism for non-instruction claims actually lives. The same pattern is what current research on LLM hallucination treats as the realistic frontier: deterministic verification of what's checkable, structured triage for what isn't, human in the loop for what's left over.

### Cited-text provenance ([ADR-0003](docs/adr/0003-cited-text-provenance.md), Status: Waiting)

The current verifier checks `source="instruction"` claims by regex-extracting volumes/wells/durations from the user's text and looking up each LLM-claimed value. This works for clean cases (`100uL`) but has known precision/recall problems: a volume written as `100 μL` (Greek mu) or `one hundred microliters` doesn't match the regex, so honest LLM output gets flagged as a false fabrication. Substring matches on substance and labware names produce collisions ("ATP" appears inside "ATPase"). Coverage is fundamentally bounded by what the regex grammar happens to handle.

ADR-0003 proposes promoting `Provenance.reason` (currently free-form prose) to a structured `cited_text` field carrying the verbatim substring of the user's instruction that the LLM is grounding the value in. The verifier then becomes a uniform substring check across all field types: does `cited_text` appear in the instruction, and is the value contained in `cited_text`? This collapses about 115 lines of regex extractors and per-category verifier logic into ~30 lines of substring checks, eliminates the regex coverage problem, and gives the LLM a clearer schema constraint to honor.

The change is in `Waiting` status because LLM reliability on the new format is the unknown that determines whether the design works in practice — and that can only be measured by building an evaluation set. Implementation plan in [`docs/plans/cited-text-provenance-plan.md`](docs/plans/cited-text-provenance-plan.md).

## Research influences

The architecture draws on specific work, mapped to where each idea lives in the code:

- **Chain-of-thought reasoning at extraction time** (Stage 2 — `<reasoning>` block emitted before structured `<spec>` JSON): follows Sebastian Raschka's *Build a Reasoning Model From Scratch* (Manning, 2024), Chapter 4 on inference-time compute scaling. Implemented in [`nl2protocol/extraction/extractor.py`](nl2protocol/extraction/extractor.py) — see the in-file "Raschka, Ch.4" reference.
- **Self-refinement on detected gaps** (Stage 3 — when the sufficiency check finds missing fields, the LLM is re-prompted with explicit gap feedback rather than re-running blind): follows Chapter 5 of the same book on self-refinement as inference-time scaling. Implemented in [`extractor.refine()`](nl2protocol/extraction/extractor.py) — see the in-file "Raschka, Ch.5" reference.
- **Provenance schema and source-typed verification** — this is the project's own design, not lifted from any single paper. Every value carries a structured `{source, reason, confidence}` citation produced by the LLM during extraction; the verifier dispatches on source to apply different checks (regex against the instruction text for `instruction` claims, route-to-user for `inferred` and weak `domain_default` claims). The combination of per-value structured provenance, source-typed dispatch, and explicit human-in-the-loop delegation for unverifiable claims is original to this project. Adjacent research that informed the general direction:
    - *Atomic-claim factuality* (FactScore, SAFE, SelfCheckGPT) — for the pattern of decomposing model output into checkable atoms.
    - *Verbalized uncertainty* (Lin et al. 2022, *Teaching models to express their uncertainty in words*) — for the self-reported `confidence` field.
    - *Citation generation in grounded LLM systems* (Anthropic Citations API, FACTS Grounding) — for the principle that grounded outputs should carry pointers to their sources.

  The reasoning behind the specific design choices, and what these techniques don't solve in the lab-protocol domain, is in [ADR-0002](docs/adr/0002-provenanced-protocol-spec.md) and [ADR-0003](docs/adr/0003-cited-text-provenance.md).
- **XML-tagged separation of reasoning and structured output** in the extraction prompt follows Anthropic's published prompt-engineering guidance. Implemented in [`prompts.py`](nl2protocol/extraction/prompts.py).

## Examples

The [`examples/`](examples/) directory has three end-to-end runs, ordered by complexity. Each example has the input instruction, the lab config, the generated Opentrons script, the simulator log, the per-stage pipeline debug log, and (for examples 1 and 3) the captured CLI stdout.

| # | Directory | Demonstrates |
|---|-----------|--------------|
| 1 | [`01_simple_transfer/`](examples/01_simple_transfer/) | Direct extraction with explicit tip strategy. Three transfers compressed to one logical step, expanded back to three `transfer()` calls. |
| 2 | [`02_pcr_mastermix/`](examples/02_pcr_mastermix/) | Multi-pipette routing (p20 + p300), distribute-vs-transfer operation selection, per-sample `mix_after`. |
| 3 | [`03_bacterial_transformation/`](examples/03_bacterial_transformation/) | 12-step heat-shock protocol with temperature module orchestration, multiple pauses, dual pipettes, per-reagent tip discipline. |

LLM extraction is non-deterministic, so reproducing these locally may produce slightly different scripts. The system's verification stages — not the LLM — are what guarantee any generated script is sound.

## Project structure

```
nl2protocol/
├── nl2protocol/
│   ├── cli.py              # Argument parsing, --setup, --init, output handling
│   ├── pipeline.py         # 8-stage pipeline orchestration
│   ├── config.py           # Lab config loading + Anthropic client init
│   ├── extraction/
│   │   ├── extractor.py    # ProtocolSpec extraction (Stage 2) + provenance verifier
│   │   ├── prompts.py      # Reasoning + extraction prompt templates
│   │   ├── resolver.py     # Labware reference resolution (Stage 3.5)
│   │   └── schema_builder.py  # spec_to_schema() — deterministic hardware mapping
│   ├── models/
│   │   ├── spec.py         # ProtocolSpec + Provenanced* types (the "science language")
│   │   ├── schema.py       # ProtocolSchema (the "hardware language")
│   │   └── labware.py      # Opentrons labware definitions + well info
│   └── validation/
│       ├── input_validator.py    # Stage 1 — classify input as protocol/question/invalid
│       ├── constraints.py        # Stage 4 — hardware constraint checker, well state tracker
│       ├── validate_config.py    # Lab config schema validation
│       └── validator.py          # Stage 8 — LLM intent verification
│
├── examples/               # 3 end-to-end runs (input + output + debug logs)
├── test_cases/             # 13 protocol examples + 12 failure mode cases
├── tests/                  # pytest tests (no API key needed)
├── docs/adr/               # Architecture decision records
├── docs/plans/             # Deferred implementation plans + architecture writeup
└── config_examples/        # Sample lab configs (basic, modules, thermocycler, high-throughput)
```

## Limitations

What the system explicitly does not try to do, with reasons:

- **Domain knowledge claims are not auto-verified.** Values the LLM tags as `domain_default` (e.g., "Bradford assay incubation is 5 minutes") are routed to user confirmation rather than auto-accepted, because the system has no authoritative external knowledge base to check them against. The user is the verification mechanism for these claims by design.
- **The Stage 8 intent verifier is a defense layer, not a contract.** It uses an LLM-as-judge approach to check whether the generated protocol matches the user's instruction. This is empirically useful (it catches real semantic issues that the simulator can't see) but it is itself an LLM call and has its own noise. A "passed" Stage 8 raises confidence but doesn't guarantee correctness.
- **Non-pipetting hardware is out of scope.** Centrifuges, plate readers, gel boxes, microscopes — these aren't on the OT-2 deck and the system makes no attempt to handle them. Instructions that require them will be classified as invalid in Stage 1.
- **No cross-protocol state.** Each run is independent. The system does not remember that a previous run filled some wells, applied some labels, or left tips in a particular state.
- **LLM extraction is non-deterministic.** Running the same instruction twice can produce different specs and different scripts. The verification stages (provenance check, hardware constraints, simulation, intent verification) are what guarantee any individual run is sound — not the LLM's consistency.
- **The verifier's coverage is bounded by its regex extractors.** A volume written as `100 μL` (Greek mu) or `one hundred microliters` is not caught by the current regex; honest LLM output using these forms gets flagged as a false fabrication. ADR-0003 proposes a structured fix; until then, this is a known precision gap.

## Roadmap

The next direction is captured in [ADR-0003 (Waiting)](docs/adr/0003-cited-text-provenance.md): replace regex-based instruction verification with a structured `cited_text` field on `Provenance` carrying the verbatim instruction substring grounding each value. Implementation plan in [`docs/plans/cited-text-provenance-plan.md`](docs/plans/cited-text-provenance-plan.md). Gated on building an evaluation set first, because the change cannot be safely validated by smoke testing alone.

A pipeline architecture writeup explaining what each stage owes the next is at [`docs/plans/pipeline-and-testing.md`](docs/plans/pipeline-and-testing.md).

## Tests

```bash
pytest tests/ -v
```

Tests run without an API key. Coverage: hardware constraint checker, well state tracker, tip strategy, confirmation queue grouping, and a battery of failure-mode tests in [`tests/test_failure_modes.py`](tests/test_failure_modes.py). The failure-mode tests are deliberately designed to trigger specific error paths (invalid wells, missing labware, equivalent labware names, pipette overcommit, etc.) — they're how the system's error handling stays honest.

## Tech stack

- Python 3.10+
- [Pydantic](https://docs.pydantic.dev/) — schema validation, the discipline boundary between LLM output and the rest of the system
- [Anthropic SDK](https://github.com/anthropics/anthropic-sdk-python) — Claude API
- [Opentrons API](https://docs.opentrons.com/) — protocol execution and simulation

## License

MIT.
