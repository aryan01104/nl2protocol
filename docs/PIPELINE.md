# nl2protocol — pipeline

In labs that need liquid-handling automation, lab members spend significant time writing protocol code by hand or relying on RAG-based converters that produce code without provenance. Even when such code happens to work, verifying it is painful: there's no link back to the original instruction, no explicit accounting of which physical constraints (labware, pipettes, config) were honored, and no clear place to push back on the system when it's wrong.

A useful protocol-generation system, in increasing order of strictness, should produce code that is:

- **Syntactical** — meets the grammar of Opentrons protocol code.
- **Semantic with respect to instruction** — produces meaningful code with respect to what the user actually asked for, and surfaces the reasoning so the user can audit it.
- **Valid with respect to config and labware** — honors the physical constraints of the labware (well counts, capacities), the set of labware that exists in the user's config, and the Opentrons machine's own limits. If the instruction can't be validly transformed given those constraints, the system says so rather than producing broken code.

nl2protocol is a pipeline that achieves all three for an `(instruction, config)` input pair. Below is the pipeline in linear order, explaining what each stage does, what claims it generates or verifies, and where its limitations are.

---

## Stage 1 — Is the input valid?

**Question:** is the user instruction a plausible natural-language protocol instruction at all?

A Haiku-tier classifier reads the raw instruction (no config visibility) and decides one of:

- `PROTOCOL` — proceed.
- `QUESTION` — the user is asking, not instructing.
- `AMBIGUOUS` — too vague to extract meaning (no specific volumes, wells, or labware).
- `INVALID` — not a liquid-handling protocol at all (e.g. "centrifuge this").

Non-protocol classifications halt the pipeline with a user-readable message. The check is cheap and fast — saves an expensive Sonnet call on inputs that can't possibly succeed.

**Limitation:** this stage does NOT check the config. The classifier only looks at instruction text.

---

## Stage 2 — Extract: produce a schematic representation of the protocol

**Question:** what is the user describing, in a form we can reason about and verify later?

One Sonnet call reads the instruction and emits a `ProtocolSpec` — a structured Pydantic object containing a list of steps and any initial-lab-state declarations. The point of this stage is to convert prose into a representation small enough for downstream verification (either by a smaller reviewer model, or deterministically).

Critically, the spec is built around **claims**, not just values. Every value the LLM extracts comes packaged with provenance: an assertion about where the value came from and what evidence backs it. The rest of the pipeline is, in essence, a series of progressively stricter checks against those claims.

### 2a. Each step-level extraction has a grounding (composition provenance)

Two questions have to be answered for every step:

**Step existence** — why does a step of this kind exist at all? Every step must trace back to the instruction:

- `grounding` is a list. It must include `"instruction"`. A step with `grounding=["domain_default"]` alone is rejected at parse time as a hallucination, since every step in the output protocol must follow from the instruction, whether directly or through a documented domain expansion that decomposes the compression into protocol-level specificity.
- `step_cited_text` — a verbatim substring from the instruction that triggered this step. Even if the step is the result of domain expansion (e.g. "do a Bradford assay" → inserting an incubation step), the system must point at the exact phrase that justified the expansion.
- `step_reasoning` — optional in general, but required when `grounding` includes `"domain_default"`. Explains how the cited instruction phrase expanded into this specific step via domain knowledge ("the standard Bradford workflow includes a 5-minute incubation between dye addition and absorbance read").

**Parameter cohesion** — even if the generic step is necessary, what justification ties these specific parameters into this step. Step existence explains the necessity of the step kind; parameter cohesion justifies the presence of these atomic values in this step.

- `parameters_cited_texts` — a list of verbatim phrases from the instruction supporting the parameters.
- `parameters_reasoning` — one paragraph linking the cites to the values.

### 2b. Each step has values, each value has its own provenance

A provenance is the claimed basis for including a value in the spec. Provenances are first checked for whether their claims are valid (the cited text actually exists in the instruction; reasoning holds up under reviewer scrutiny), then used for downstream auditability.

The kinds of values that can appear on a step:

- `volume`
- `source` (labware description + well / wells / range)
- `destination` (labware description + well / wells / range)
- `substance`
- `duration`
- `temperature`

Every populated value carries its own `Provenance` object, separate from the step-level composition provenance. The source of a value is one of:

**`source = "instruction"`** (the user literally wrote it in the NL instruction):

- `cited_text` is required — one or more verbatim substrings from the instruction containing the value. Accepts either a single string OR a list of strings (the schema normalizes single-string inputs to a one-element list). Multiple entries cover the case where the value's grounding is spread across the instruction — e.g. a wells list `[A1, A2, A3, A4]` captured from four bullet points: `["Plasmid A1 to cells B1", "Plasmid A2 to cells B2", ...]`. Every entry must appear verbatim; comma-joining bullets into one fake substring is rejected by the verifier.
- For volumes specifically, an `exact: true/false` flag distinguishes "user stated this exact number" from hedged ("about 50uL").
- No reasoning fields — the citation IS the justification.

**`source = "domain_default"` or `"inferred"`** (the LLM filled it in):

- `positive_reasoning` is required — one sentence answering "why is THIS the right value?". For `domain_default`, cite the protocol and standard practice. For `inferred`, state the derivation.
- `why_not_in_instruction` is required — one sentence answering "why did I have to infer this instead of cite it?". Names the specific element the instruction lacks.
- No `cited_text` allowed, tautologically.

**Confidence** — a 0.0–1.0 score across all sources. Calibration: 1.0 = user literally wrote it; 0.8 = standard protocol default; 0.6 = reasonable inference; 0.4 = weak guess.

### 2c. Why the two reasoning fields are kept separate

It would be simpler to fold positive and negative reasoning into one string. We don't, because a downstream reviewer model grades each claim independently. Bundling them together would mean the reviewer can only agree/disagree as a unit — and a wrong negative claim ("the value WAS in the instruction, you missed it") would drag down the verdict on the positive. Keeping them split lets each be checked on its own.

### 2d. What this stage validates

The `ProtocolSpec` schema enforces:

- Step orders form a permutation of `{1, 2, ..., N}` — no gaps, no duplicates.
- At least one step.
- Per-provenance: structural consistency of the source / cite / reasoning fields. Pydantic rejects malformed claims at parse time, before any logic runs.

It does NOT validate, per an architectural choice:

- Whether claims are truthful (does the cite actually exist in the instruction?). Deferred.
- Whether the value the LLM picked is plausible. Deferred.
- Whether the labware exists in the config. Deferred.
- Whether the spec faithfully represents the instruction (did the LLM drop a step?). No formal check. Implicit trust.

### 2e. What this stage explicitly does NOT do

- Does not pick a config label. Labware is referred to by user-language description ("tube rack", "the reservoir") only. Config translation is the next stage.
- Does not verify citations. This stage produces claims; later stages check them.
- Does not require fields to be non-null. If the LLM can't reliably pick a value in one large run, we'd rather it leave the field null than guess wrong. Nulls become explicit gaps that the orchestrator handles later with more focused attention.
- Does not check hardware constraints. Volumes, capacities, pipette limits — all deferred to the constraint checker.

### 2f. Two asymmetries worth knowing

**Null values carry no provenance.** A populated value carries a Provenance. A null value carries nothing — no record of why it was left null. So the system can't distinguish "the LLM correctly inferred there was no source mentioned" from "the LLM forgot to extract a source that was actually there." Both look identical downstream.

**Two fields are bare floats with no provenance even when populated.** `WellContents.volume_ul` and `LabwarePrefill.volume_ul` (the initial-contents volumes the user states in the instruction, like "50uL aliquots of competent cells") are bare floats, not Provenanced wrappers. When the LLM extracts these volumes from the instruction, the cite is lost — the value gets through but the citation story is unrecoverable. The IC modal shows these rows as "from instruction" without a citation to back it. The other Provenanced* types (volume on transfer steps, duration, temperature, substance) carry full provenance; only these two initial-contents shapes are exceptions, by historical accident rather than design.

---

## Stage 3 — Resolve labware

**Question:** which config labware does each user-language description map to?

User-language descriptions ("tube rack", "the reservoir", "PCR plate") need to be translated to config-canonical labels ("sample_rack", "reagent_reservoir", "assay_plate_96") before downstream stages can validate against config constraints.

One Sonnet call does the mapping in a batch. The prompt receives:

- The full config's labware section (label → load_name → slot).
- Every unique description from the spec, with per-description step context (what action, what wells, what role).

Sonnet returns a `{description: config_label_or_null}` dict. The resolver wraps each successful pick in a `LabwareSuggestion` (carrying the picked label + reasoning + confidence + the full list of valid config labels for the confirmation UI). The resolver does not mutate the spec — it returns suggestions only. The pipeline writes `resolved_label` and `resolved_label_provenance` onto each LocationRef **after the user confirms** in Stage 4c.

This **suggest → confirm → apply** pattern keeps the audit trail honest: `resolved_label_provenance.review_status` reflects what the user actually did — `"user_accepted_suggestion"` when they kept the resolver's pick, `"user_edited"` when they overrode it. The provenance can't lie about who made the call. The `labware_resolution_done` event also fires after Stage 4c, so its payload carries the final user-confirmed mappings rather than tentative LLM picks.

Descriptions the resolver returned null for, or labels that don't match a real config entry, stay unresolved going into Stage 4c — the user picks from the dropdown there. Anything still unresolved after Stage 4c gets caught by the orchestrator's `LabwareAmbiguityDetector`.

**Why before the orchestrator?**

The orchestrator's `LabwareAmbiguityDetector` is explicitly designed to flag only refs the resolver couldn't pick. If the resolver ran later, every downstream check would have to handle both user-language and config-label forms; running it first normalizes the spec.

**Limitations**

- Entirely LLM-based. No deterministic pre-pass. If Sonnet hallucinates a mapping (or refuses to map something it should), the next stage catches it via user review. No rule-based fallback.
- Skips null refs. If a step had `source = None`, there's nothing to resolve — the null is deferred to the orchestrator's `MissingFieldsDetector` + `ConfigLookupSuggester`.

---

## Stage 4 — Pre-orchestrator batch confirmations ("lab setup review")

**Question:** before we run a per-decision verification loop, are there big-shape decisions the user can confirm in bulk?

Three batch modals fire in sequence. Each handles a type of decision that's easier to scan in bulk than to answer one-at-a-time:

### 4a. Initial contents (lab-state confirmation)

The spec's `initial_contents` and `prefilled_labware` declarations describe what's already in the lab before the protocol runs. The user is shown a table of `(labware, well, substance, volume)` rows and can edit volumes or confirm defaults. Rows where the LLM extracted a volume from the instruction are shown normal-weight; rows where a suggester filled in a default (typically `WellCapacitySuggester` defaulting to the labware's well capacity) are shown italic-dim with a per-row hint explaining the source of the default. This replaces what would otherwise be N per-Gap modals during the orchestrator loop.

### 4b. Source containers (inferred-source acknowledgment)

If source resolution inferred that certain wells will be source-only (the user needs to physically pre-fill them before running), those inferences are shown as a list. User says yes/no. Aborting means "your instruction needs to clarify where these substances come from."

**Limitation:** this is currently a binary Y/N — the user can't edit the inferred list. If they disagree, they have to abort and re-edit the instruction. The cleaner design (per ADR-0011) folds this into 4a as flagged rows.

### 4c. Labware assignments (config-label review)

The labware resolver's `(description → config_label)` picks are shown as a table with each row's reasoning surfaced inline beneath the dropdown. User can override any mapping via dropdown, or accept all. Submission writes `resolved_label` + a truthful `resolved_label_provenance` to each LocationRef (see Stage 3 for the `review_status` semantics).

**Why these three are batch and not per-Gap**

They're the same shape of decision: many rows, same form, scan and edit. The orchestrator handles per-Gap judgment decisions (one missing field, one fabrication, one ambiguity). Splitting decisions by shape (bulk vs per-item) means each surface matches its natural UX.

**Limitation**

Three sequential modals can feel heavy. A unified "lab setup review" surface with all three concerns in sections would be cleaner and was the original intent in ADR-0011 Pattern 1. The current fragmentation across three modals is an implementation that drifted from the design.

---

## Stage 5 — Orchestrator gap resolution

**Question:** for everything not handled by stages 3 and 4, can we resolve each remaining gap automatically — and if not, ask the user once per gap?

This is the only stage where the spec can change after extraction. The loop runs up to 3 iterations:

`DETECT → topological sort → SUGGEST → REVIEW → CLASSIFY → PRESENT → APPLY → RE-DETECT`

### Detectors — what kinds of gaps exist

| Detector | What it flags |
|---|---|
| `MissingFieldsDetector` | Required fields left null (e.g. transfer with no volume) |
| `ProvenanceWarningDetector` | Values claiming `source="instruction"` whose cite isn't in the text — fabrication |
| `InitialContentsVolumeDetector` | `WellContents` rows with null `volume_ul` (typically already cleared by 4a) |
| `ConstraintViolationDetector` | Hardware-physics violations (volume > pipette capacity, well doesn't exist on labware) |
| `LabwareAmbiguityDetector` | LocationRefs the resolver couldn't pick a config label for |

### Suggesters — proposed fills, in registry order

Each gap is run through the suggesters in order; the first one to return a non-None Suggestion is taken up. The deterministic suggesters come first, cheapest before resorting to most expensive:

| Suggester | Strategy |
|---|---|
| `ConfigLookupSuggester` | Match substance → look up labware in initial_contents / config |
| `CarryoverSuggester` | Inherit from prior step (e.g. wait_for_temperature inherits from set_temperature) |
| `WellCapacitySuggester` | Default `volume_ul` to the labware's well capacity from config |
| `RegexFromNoteSuggester` | Extract numbers from `step.note` text |
| `WellRangeClipSuggester` | Clip out-of-range wells to the labware's valid set |
| `LabwareSuggester` | Pick a config label for ambiguous LocationRefs |
| `LLMSpotSuggester` | Last-resort one-shot LLM call for everything else |

The first six are no-LLM, fast, and correct-or-empty. `LLMSpotSuggester` is the fallback that costs a token.

### Reviewer — the two-claim verifier

For every suggestion from a non-deterministic source (`inferred` or `domain_default`), the `IndependentReviewSuggester` runs a Haiku call evaluating the suggestion's two reasoning claims independently:

- `confirms_positive`: is the `positive_reasoning` sound? (domain-knowledge check)
- `confirms_negative`: is the `why_not_in_instruction` correct? (instruction-text-grounding check)

The verdicts get stamped onto the spec's Provenance objects (`review_status = "reviewed_agree"` or `"reviewed_disagree"` plus an objection string). The audit trail survives past this iteration.

**Why Haiku, not Sonnet:** different model from the extractor by design (per ADR-0008). The same model can't grade its own output without bias. Cheaper and sufficient for the structured two-claim task.

### Auto-accept gate

A gap is resolved automatically (no user prompt) only if all four conditions hold:

1. A suggestion exists.
2. Gap kind is NOT in `{"fabricated", "ambiguous", "constraint_violation"}` — these always go to the user.
3. Suggestion `confidence >= 0.85`.
4. If a review exists: both `confirms_positive` AND `confirms_negative` are True.

Anything failing any gate goes to the user as a per-Gap modal. The user has four actions:

- **Accept** — take the suggester's value. For most gap kinds this replaces the field's value with the suggested one. For fabrication gaps specifically (where the value may already be correct but the citation is malformed), accept **restates** the field's provenance as `source="inferred"` with the suggester's reasoning — value untouched. The user is saying "I trust the value, take the reasoning the system offered."
- **Edit** — type a replacement value. The field is overwritten and the provenance stamped as `user_edited`.
- **Override** — fabrication-only (ADR-0012). Keep the existing value AND the existing fabricated provenance. The audit-visible flag `review_status = "user_overrode_fabrication"` records that the user accepted responsibility for an ungrounded value the verifier flagged.
- **Skip** — valid only for `severity = quality`. Gap stays unresolved.
- **Abort** — halt the pipeline.

### Iteration

After resolving as many gaps as possible in one pass, the detectors re-run on the mutated spec. Some gaps may have been introduced by previous resolutions (e.g. user edited a volume → opens a new constraint violation). Loop until:

- **Converged:** zero gaps remain → spec ready.
- **Aborted:** user clicked Quit on any gap.
- **Cap:** 3 iterations elapsed → if gaps still remain, halt with an error.

**Limitations**

- **No detection of missing steps.** The orchestrator can only see what's in the spec. If the LLM dropped a step at extraction time, no detector will surface it.
- **Detector overlap with stage 4.** `InitialContentsVolumeDetector` and `LabwareAmbiguityDetector` are still in the registry even though stage 4 typically clears them. Defensive but redundant.
- **Constraint check duplication.** `ConstraintViolationDetector` runs in this loop AND the final constraint checker runs in stage 6. Two passes covering overlapping ground.

---

## Stage 6 — Constraint check (final safety net)

**Question:** does the now-resolved spec actually fit the hardware?

A deterministic checker (`ConstraintChecker`) walks the spec one last time and validates hardware physics: every well referenced exists on the named labware, every volume fits the available pipette range, no source/destination collisions, no aspirate-from-empty-well sequences, etc.

If there are hard errors, the user is prompted "proceed anyway?". Accepting commits to a known-broken protocol (CLI prompt was `[y/N]` — defaults to no). Rejecting halts.

This is the last chance to catch issues before the spec is promoted to its strict-typed form.

---

## Stage 7 — Promote to `CompleteProtocolSpec` + build schema

**Question:** is the resolved spec actually complete enough to generate code from?

The spec is cast to `CompleteProtocolSpec` — a stricter subclass of `ProtocolSpec` with per-action completeness rules (transfer needs source + destination + volume; mix needs volume; set_temperature needs temperature; pause needs duration OR note; etc.). If any required field is still null, this raises and the pipeline halts. Belt-and-suspenders against orchestrator-convergence bugs.

Then `spec_to_schema` converts the completed spec into a `ProtocolSchema` — a deterministic intermediate form that captures every Opentrons-level operation (load labware, load pipette, transfer, mix, delay, etc.) plus a step → line map for hover-pairing the spec column with the script column in the visual report.

No LLM. Pure transformation.

---

## Stage 8 — Generate Python script

**Question:** what's the actual Opentrons code for this schema?

Deterministic schema → Python code. No LLM. The script is written to disk for inspection regardless of whether the next stage passes.

---

## Stage 9 — Opentrons simulation

**Question:** does the Opentrons simulator accept the script?

The generated script is run through Opentrons' own simulator. This is the final correctness check — it catches anything the constraint checker missed (e.g. subtle interactions between actions that aren't representable as a single-step constraint).

If the simulator passes, the pipeline is done. The user gets:

- The Python script.
- The full simulation log.
- The state log with audit trails for every gap resolution.
- The HTML report with the visual surface.

If the simulator fails, the script and the failure output are saved to disk for inspection. The pipeline doesn't retry — failure at this stage indicates an inconsistency the upstream stages didn't catch, and the right response is to surface it loudly rather than paper over it.

---

## Why the order is what it is

A summary of the dependencies driving stage order:

- **Extract first** because every downstream check needs structure to operate on.
- **Labware resolve second** because every downstream stage benefits from config-canonical names instead of user-language descriptions.
- **Batch confirmations third** because (a) the user can confirm bulk things efficiently and (b) clearing these early means the orchestrator only handles per-Gap judgment, not bulk decisions.
- **Orchestrator fourth** because each remaining gap is a one-off judgment call that benefits from having the spec mostly-resolved by then.
- **Constraint check fifth** because the orchestrator may have introduced new violations via user edits.
- **Promote + schema build + codegen + simulate** in deterministic sequence because each transforms the spec into a strictly more-specific form, and the simulator is the load-bearing correctness check at the end.

Within each stage, the order matches information dependency: things that produce values others consume run first.

---

## Limitations and known gaps

Honest documentation of failure modes is more useful than pretending there are none. These are the load-bearing weaknesses.

### Semantic-checking limitations

- **The spec can be wrong without anyone noticing.** Nothing in the pipeline checks "did the LLM extract every step the user asked for, in the right action types?". If the LLM silently drops a step or misclassifies one, the orchestrator and constraint checker see a smaller, well-formed spec and won't complain. This is the load-bearing weakness for protocols with subtle phrasing.
- **Cite disambiguation is not handled.** If the instruction contains the substring `"100uL"` multiple times in different roles ("Add 100uL of sample, then mix at 100uL volume"), the LLM picks one occurrence as its cite and the system trusts that pick. No formal check that the LLM cited the right instance of the substring.
- **Null fields carry no provenance.** A `step.source = None` could mean "the LLM correctly inferred there was no source" or "the LLM forgot to extract a source that was actually there." Both look identical to downstream stages.

### Implementation limitations

- **Single-user assumption baked in.** The thread-bridge between the orchestrator and the browser handler assumes ONE pipeline running per process. Multi-user deployment requires session-keyed bridges and a worker queue.
- **No persistence.** Pipeline state lives in memory; static reports write to local disk. Cloud deployment needs a database for run history and object storage for artifacts.
- **No auth.** Local-only tool today.

### Limitations that are mid-migration or drifted from design

- **ADR-0011's column-to-action mapping is out of date.** The ADR says col 2→3 = orchestrator gap resolution and col 3→4 = labware-resolve + constraint check. The code (post-Phase 3f) moved the batch confirmations and labware resolution BEFORE the orchestrator. So col 2→3 actually = labware-suggest + 3 batch confirmations + orchestrator loop; col 3→4 = just constraint check. The `labware_resolution_done` event still fires (now after Stage 4c so its payload reflects user-confirmed mappings), but the column-to-action narrative in the ADR is wrong until updated.

### Limitations that are intentional

- **No retry on LLM hallucinations at extraction.** If Sonnet drops a step or makes one up, there's no automated retry — the user catches it in the visual surface or doesn't. Adding a retry-with-feedback loop is a possible future enhancement but would cost real tokens and may not improve precision enough to justify.
- **No semantic-equivalence check between spec and generated script.** The Opentrons simulator catches code-execution problems but doesn't validate that the script does what the user asked for. The user's eye is the final arbiter.
