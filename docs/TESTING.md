# Testing strategy

This document describes how the project is tested, what's covered, and — equally important — what's deliberately not tested. It exists so a reader can understand the testing discipline without scanning every test file, and so future work on the suite stays consistent with the existing approach.

The framework used here follows the standard test pyramid (unit → integration → system → acceptance), with explicit scoping decisions about which layers we exercise and why. Most tests are deterministic and run without an API key; a smaller LLM-dependent layer is gated behind `ANTHROPIC_API_KEY`.

## Where tests sit on the pyramid

```
                ┌─────────────────────────┐
                │  Acceptance / End-to-End │   not automated — see examples/
                └─────────────────────────┘
              ┌────────────────────────────┐
              │   System (full pipeline)    │  test_failure_modes.py (LLM-gated)
              └────────────────────────────┘
            ┌──────────────────────────────┐
            │       Integration             │  tests/integration/ (mocked LLM)
            └──────────────────────────────┘
          ┌────────────────────────────────┐
          │             Unit                │  tests/test_*.py
          │                                 │  tests/property/ (Hypothesis)
          │                                 │  tests/test_boundaries.py (BVA)
          └────────────────────────────────┘
```

279 tests total. The base is wide — contract tests on every public function in the deterministic core, plus Hypothesis property tests, plus explicit boundary-value tests. The middle is filled by mocked-LLM integration tests of the stage chain. The top is partially covered by LLM-gated failure-mode tests; full end-to-end runs are exercised manually via `examples/` rather than asserted by CI.

## What's tested

### Unit tests — deterministic, no API key required

| File | Tests | What it exercises |
|---|---|---|
| `test_constraints.py` | 122 | Hardware constraint checker (`get_pipette_range`, `get_pipette_for_volume`, `get_all_pipette_ranges`, `ConstraintChecker.check_all`, `WellState.add/remove`, `WellStateTracker.dispense/aspirate/get_volume/get_substances/well_has_liquid`) — one prescriptive contract test per docstring clause. |
| `test_spec_validators.py` | 22 | Pydantic model-validators on `ExtractedStep.coerce_replicates`, `ProtocolSpec.validate_step_ordering`, `CompleteProtocolSpec.validate_completeness`. |
| `test_labware.py` | 17 | `get_well_info` — Opentrons-known labware path, all heuristic-substring patterns (parametrized), and pin-tested wart paths (default-96-well silent fallback). |
| `test_input_validator.py` | 18 | Phase-1 deterministic length-check path of `InputValidator.classify` (LLM call mocked out). |
| `test_errors.py` | 18 | `format_error_for_cli`, `format_api_error` dispatch tables; exception-class message construction. |
| `test_config.py` | 19 | `normalize_config`, `enrich_config_with_wells`, `ConfigLoader.__init__`, `ConfigLoader.load_config`. |
| `test_validate_config.py` | 19 | `ConfigValidator.validate` orchestration, the two convenience functions, `ValidationResult.__str__`. |
| `test_confirmation_grouping.py` | 18 | Confirmation queue grouping logic — rectangular blocks, partial blocks, irregular sets. |
| `test_tip_strategy.py` | 7 | Tip-strategy expansion: `new_tip_each` vs. `same_tip` across single/multi-source/cross-step cases. |
| `test_boundaries.py` | 18 | Explicit edge-of-range tests at exact thresholds (0.01uL tolerance, pipette range edges via `math.nextafter`, Pydantic `ge`/`le`/`gt` boundaries). See [Boundary inventory](#boundary-inventory). |

### Property-based tests — Hypothesis, deterministic

| File | Properties | What it exercises |
|---|---|---|
| `tests/property/test_constraint_properties.py` | 13 | Helper-level invariants: `get_pipette_range` returns correct range for any recognized prefix; `get_pipette_for_volume` returns mounts whose range covers the volume; `WellState.add/remove` cumulative arithmetic and substance dedup. |
| `tests/property/test_check_all_properties.py` | 7 | Orchestration-level invariants on `ConstraintChecker.check_all`: determinism, spec/config purity, every violation has all 6 structured fields, volume-completeness, volume-soundness, well-validity completeness. Built on inline Hypothesis strategies for `ProtocolSpec`, `LocationRef`, `Provenance`, etc. |

### Integration tests — mocked Anthropic client, deterministic

| File | Tests | What it exercises |
|---|---|---|
| `tests/integration/test_pipeline_with_mocks.py` | 5 | Stage chain with the LLM mocked at `SemanticExtractor`'s client boundary: extract → constraints → spec_to_schema → generate_python_script. Covers happy paths (single transfer, multi-step), and cases where downstream stages catch what the LLM can't (oversized volume, well outside labware grid), plus malformed-LLM-response handling. Skips the interactive `_prompt_input` paths in `ProtocolAgent.run_pipeline` by design — those couple to UX, not pipeline shape. |

### System tests — LLM-dependent, gated on `ANTHROPIC_API_KEY`

| File | Tests | What it exercises |
|---|---|---|
| `test_failure_modes.py` | 12 | Two halves: (a) deterministic failure-mode tests on hand-crafted specs (volume exceeds pipette, missing labware/module, multi-gap configs, well-state with primed wells, config-validator rejecting invalid load_names); (b) LLM-dependent tests verifying real extraction handles edge cases (misspellings, equivalent labware names, compact instructions, non-protocol input correctly rejected). |

The `requires_llm` marker (a `pytest.mark.skipif` checking for the env var) keeps the suite green for contributors without API access. CI-style runs can include LLM tests by exporting the key first.

## What's deliberately not tested

Following the principle that *exhaustive testing is impossible*, here is what we explicitly do not cover, with reasons:

- **Generated Opentrons script execution on real hardware.** The Opentrons simulator (Stage 7) is the test oracle for protocol correctness at the API level. We do not run scripts on a physical OT-2 in CI — that requires hardware, network access, and would not catch new classes of bugs that the simulator misses.
- **LLM intent verification (Stage 8) accuracy.** The Stage 8 verifier is itself an LLM call; testing whether its judgments are correct would require an evaluation set of (instruction, generated_script, expected_verdict) triples, which we have not built. This is acknowledged in the README's Limitations and tracked as future work.
- **Domain knowledge claims** (e.g., "is 5 minutes the right Bradford incubation?"). These are routed to user confirmation by design (see [ADR-0002](adr/0002-provenanced-protocol-spec.md)). The system does not assert correctness on these and tests cannot either.
- **Long-running fuzzing.** Hypothesis-based property tests (`tests/property/`) cover the constraint checker's helpers and orchestrator with ~50–100 generated inputs per property; broader fuzzing (e.g., multi-hour AFL-style runs) is not done.
- **End-to-end pipeline runs in CI.** Full pipeline runs cost API tokens and are non-deterministic. The `examples/` directory acts as a manually-verified end-to-end witness instead.

## Testing principles followed

The course (CS 5150) lists seven principles. The ones that meaningfully shape this suite:

**Principle #1 — Testing shows the presence of defects, not their absence.** The suite is not a correctness proof. It is a regression net plus a deliberate set of negative tests (in `test_failure_modes.py`) designed to keep error paths working as the codebase changes.

**Principle #2 — Exhaustive testing is impossible.** Concretely, we don't try to cover every (volume, pipette, well, labware, module) combination. We test boundary conditions (volume at pipette range edges, well at capacity, no tips remaining) and rely on Pydantic to catch shape-level violations elsewhere.

**Principle #4 — Defects cluster.** Test density is highest where bugs have historically appeared: well state tracking (cross-step volume accounting was a recurring source of bugs), pipette range matching (the `10.5uL` retry-corruption bug that motivated the deterministic schema split), and tip strategy (a subtle area where the wrong tip-handling pattern silently breaks downstream science).

**Principle #5 — The pesticide paradox: re-running the same tests gives false security.** As features land, new failure-mode tests are added (`test_failure_modes.py` started with 4 tests and grew to 12). The suite is intended to grow with the system, not be frozen.

**Principle #7 — Verification ≠ validation.** This suite handles verification (does the code behave as specified — does the constraint checker correctly flag a tip exhaustion, does well state correctly track primed wells). Validation (does the system meet the user's actual need) is delegated to the user via the confirmation UX in the pipeline, since "the user's actual need" for a lab protocol can only be answered by the user.

### Test design methods used

Following the lecture's distinction between black-box and white-box test design:

- **Specification-based (black-box):** most unit tests are spec-based — they construct a hand-crafted `ProtocolSpec` matching some scenario described in domain terms ("a transfer step requesting 500uL with only a p20 loaded should flag a violation"), then assert the constraint checker reports the expected violation. The tests don't depend on which lines of `constraints.py` execute, only on the contract.
- **Boundary-value analysis:** present implicitly — `test_500ul_exceeds_p20` is a boundary test (just over p20's max), `test_well_overflow_warns` exercises the boundary of well capacity, several constraint tests check exactly at pipette range edges. These should be made more explicit in future work.
- **Equivalence classes:** the failure-mode tests partition the input space by which stage / which check is being violated (volume, labware, module, tip, well state, config) — each test is a representative of its equivalence class.
- **White-box / coverage-driven:** not used as a primary *design* driver — tests are written against contracts, not against uncovered lines. Coverage is measured and reported (see [Coverage baseline](#coverage-baseline)) as a quality signal, not a target to chase.

## How to run

```bash
# Default — deterministic tests only, no API key needed
pytest tests/ -v

# Include LLM-dependent tests
ANTHROPIC_API_KEY=... pytest tests/ -v

# With coverage (pytest-cov is in dev dependencies)
pytest tests/ --cov=nl2protocol --cov-report=term-missing
```

## Coverage baseline

Measured on 2026-05-02 with the deterministic suite (61 tests, no API key), reporting **both line and branch coverage**. Headline: **41% line / 37% branch overall**, which sounds low until you split it by layer.

### Why both line and branch

Line coverage alone is a weak signal for code that's mostly conditionals. A line like `if violation: report.append(...)` reaches 100% line coverage from a single test that triggers the violation — the false-branch (no violation) path is invisible to the metric. Branch coverage closes that gap: every conditional must have *both* edges taken. It is strictly stronger than line coverage and is the more honest number for the constraint checker, which is almost entirely conditionals.

We report both because they answer different questions: line coverage answers *"was this code reached?"* and branch coverage answers *"was every decision exercised in both directions?"* Both gaps matter; neither subsumes the other in practice (an unreached line still has zero branch coverage of its own).

Stronger measures exist and are deliberately not used here as the *primary* signal:

- **MC/DC** (modified condition/decision) is the aviation-grade standard for compound boolean expressions but requires a specialized analyzer and rarely justifies itself outside safety-critical code.
- **Mutation testing** (e.g., `mutmut`) is the gold standard — it actively perturbs the source and asks whether any test catches the change, which catches the "100% coverage but no assertions" failure mode. It is run on the highest-stakes module only (see [Mutation testing](#mutation-testing-of-validationconstraintspy)) because a full-repo mutation run would be too slow for the size of this suite.

### By module

| Module | Line | Branch | Why |
|---|---|---|---|
| `models/spec.py` | 94% | 89% | `ProtocolSpec` schema — exercised by every constraint test that constructs a spec. |
| `validation/constraints.py` | 88% | 84% | Hardware constraint checker — the single largest test target in the repo. The 4-point line→branch gap (36 partially-covered branches) is mostly "violation tested, no-violation not asserted" — a real follow-up. |
| `extraction/resolver.py` | 82% | 77% | Labware resolver — exercised incidentally by constraint and failure-mode tests. |
| `models/schema.py` | 74% | 64% | Pydantic schemas for stages 1–3; the 10-point gap reflects untested validator else-paths (only the rejection cases are asserted, not the accept cases). |
| `extraction/schema_builder.py` | 55% | 50% | Deterministic schema-building paths covered; LLM-driven branches not. |
| `extraction/extractor.py` | 32% | 27% | LLM-driven; only exercised when `ANTHROPIC_API_KEY` is set. |
| `validation/validator.py` | 22% | 16% | Stage 8 LLM intent verifier — deliberately not unit-tested (see "What's deliberately not tested"). |
| `pipeline.py`, `cli.py`, `robot.py` | 9% / 0% / 14% | 7% / 0% / 11% | End-to-end orchestrators — exercised by `examples/` runs and manual CLI use, not by CI. |
| **TOTAL** | **41%** | **37%** | |

The split is intentional: the **deterministic core sits at 77–89% branch coverage**, and that's where bugs cluster (per principle #4). The low-coverage modules are exactly the layers we deliberately don't unit-test (LLM calls, CLI orchestration, hardware control) — covering them with mocks would inflate the number without catching real defects. The global figure reflects the scoping decision documented above, not a gap in discipline.

**Floor to maintain:** `validation/constraints.py` should not drop below **80% branch**, and `models/spec.py` should not drop below **85% branch**. These are the modules whose contracts the rest of the system depends on.

To reproduce:

```bash
pytest tests/ --cov=nl2protocol --cov-branch --cov-report=term-missing
```

## Reading a test failure

A test failure means one of three things, in decreasing order of likelihood:

1. A real regression — a recent change broke a contract that a test asserts.
2. A test that drifted from the contract — the underlying behavior changed deliberately and the test wasn't updated. Fix the test if so.
3. A flaky LLM test — the model produced something different on this run. Re-run; if it's consistent, the test's assertion is too strict (likely depending on exact LLM phrasing) and should be loosened.

## Mutation testing — `validation/constraints.py`

To verify the 87% branch coverage on `validation/constraints.py` is real (not theatre — code that runs but has no assertions on its behavior), the module was mutation-tested with [mutmut](https://mutmut.readthedocs.io/) v3.5 against `tests/test_constraints.py`. Mutation testing perturbs the source (flip `>` to `>=`, change a constant, delete a branch) and checks whether any test catches the change.

**Results across phases (all on the same 845 mutants):**

| Phase | Test set | Killed | Survived | Score |
|---|---|---|---|---|
| 0 — Contract tests only | `test_constraints.py` | 333 | 490 | **40.5%** |
| 1 — + helper properties | + `tests/property/test_constraint_properties.py` | 349 | 496 | **41.3%** |
| 2 — + `check_all` properties | + `tests/property/test_check_all_properties.py` | **378** | 467 | **44.7%** |

The progression confirms the technique works — each phase added properties that killed previously-surviving mutants. Lift was modest because the bulk of survivors are in private `_check_*` helpers (alt-pipette suggestion logic, suggestion-message construction) where neither leaf-helper nor orchestration-level properties reach without targeted strategies.

The 40.5% is striking next to 87% branch coverage and is exactly what mutation testing exists to surface: branch coverage proves the *line* ran; mutation testing proves the *behavior* was asserted on. The two answer different questions.

Sampling surviving mutants reveals three categories:

1. **Real gaps** — boundary off-by-ones the tests miss. Example: `current < 0.01` → `current <= 0.01` survives because no test exercises the well-volume boundary at exactly 0.01uL. These are addressable with targeted tests (planned for `test_boundaries.py`).
2. **Defensive-code mutants** — `dict.get(k, {})` → `dict.get(k, None)` survives because no test exercises a missing-labware path; the `{}` default is unreachable in practice. These reflect dead defensive branches, not test gaps.
3. **String-wording mutants** — `"NOTE:"` → `"note:"` survives because the relevant tests check `"assumed" in msg.lower()` rather than pinning exact case. These survive *by design* — wording isn't load-bearing.

**Interpretation:** the 40.5% is a *floor on test sensitivity*, not a bug count. The next iteration of the test suite should target category 1 (real gaps) — particularly boundary tests around the 0.01uL tolerance and 0.5uL pipette-range edges.

**Reproduce:**
```bash
mutmut run        # uses [mutmut] section in setup.cfg
mutmut results    # list survivors
mutmut show <id>  # diff for a specific mutant
```

## Boundary inventory

Every numeric boundary in the deterministic core, the tests that cover it, and the principle (BVA — boundary-value analysis) it enforces. Centralized here so the boundary discipline is visible at a glance — the actual tests live in scattered files for locality.

| Boundary | Source location | Edge tests live in |
|---|---|---|
| Pipette range — exact min/max (inclusive) | `validation/constraints.py:get_pipette_for_volume` | `test_constraints.py:TestGetPipetteForVolume.test_range_endpoints_are_inclusive` |
| Pipette range — smallest float below min | (same) | `test_boundaries.py:TestPipetteRangeFloatEdges.test_smallest_float_below_min_not_covered` |
| Pipette range — smallest float above max | (same) | `test_boundaries.py:TestPipetteRangeFloatEdges.test_smallest_float_above_max_not_covered` |
| Pipette spec prefix collision (p10 vs p1000) | `validation/constraints.py:get_pipette_range` | `test_constraints.py:TestGetPipetteRange.test_p1000_does_not_collide_with_p10` |
| Well-state 0.01 uL tolerance — well_has_liquid at exactly 0.01 | `validation/constraints.py:WellStateTracker.well_has_liquid` | `test_boundaries.py:TestWellHasLiquidAtExactThreshold` |
| Well-state 0.01 uL tolerance — remove at exact +0.01 overdraw | `validation/constraints.py:WellState.remove` | `test_boundaries.py:TestWellStateRemoveAtExactToleranceBoundary` |
| Well-state 0.01 uL tolerance — within (0.005) and outside (0.5) | (same) | `test_constraints.py:TestWellStateRemove.test_within_tolerance_overdraw_succeeds` and `test_outside_tolerance_overdraw_fails` |
| Well capacity overflow — at and above estimated capacity | `validation/constraints.py:WellStateTracker.dispense` | `test_constraints.py:TestWellStateTracker.test_well_overflow_warns` |
| Provenance.confidence — exact 0.0 and 1.0 (inclusive endpoints) | `models/spec.py:Provenance` | `test_boundaries.py:TestProvenanceConfidenceBoundaries` |
| Provenance.confidence — just outside [0, 1] | (same) | (same) |
| ProvenancedVolume.value — exact 0 (rejected) and smallest positive (accepted) | `models/spec.py:ProvenancedVolume` | `test_boundaries.py:TestProvenancedVolumeBoundaries` |
| ExtractedStep.replicates — coercion at 1, 0, negative | `models/spec.py:ExtractedStep.coerce_replicates` | `test_spec_validators.py:TestCoerceReplicates` |
| ExtractedStep.order — `ge=1` boundary at 0 | `models/spec.py:ExtractedStep` | `test_spec_validators.py:TestValidateStepOrdering.test_zero_based_rejected_by_field_constraint` |
| ProtocolSpec.steps — minimum length 1 | `models/spec.py:ProtocolSpec` | (Pydantic-enforced; covered transitively by every other test that passes a spec) |
| Step ordering — gap, duplicate, starts-at-2 | `models/spec.py:ProtocolSpec.validate_step_ordering` | `test_spec_validators.py:TestValidateStepOrdering` |
| Tip sufficiency — tip count > tipracks × 96 | `validation/constraints.py:_check_tip_sufficiency` | `test_constraints.py:TestTipSufficiency` |
| Input length — < 3 chars and > 10000 chars | `validation/input_validator.py:InputValidator.classify` | `test_input_validator.py:TestClassifyTooShort`, `TestClassifyTooLong` |

The principle: every numeric threshold deserves at least four tests — *just inside* both endpoints (BVA equivalence-class membership) and *just outside* both endpoints (boundary-violation detection). Where existing tests skip the *exact-edge* value (because the edge is a single representable float), `test_boundaries.py` fills the gap.

## Known design warts (surfaced by contract pass)

Documented and pin-tested for follow-up — current behavior is enshrined by tests so a future fix surfaces in the right place:

- **`models/labware.py:get_well_info`** silently returns a fictional 8×12 (96-well plate) layout for any unrecognized load_name AND for empty strings. A typo in `lab_config.json`'s `load_name` is indistinguishable from a real 96-well plate. Caller `validation/constraints.py:_check_well_validity` passes possibly-empty strings, so the wart is reachable in practice. Fix likely: raise `ValueError` on unrecognized/empty input, then update the caller to skip empty load_names explicitly.
- **`validation/validate_config.py:ConfigValidator.validate`** crashes with `AttributeError` when `config["labware"]` or `config["pipettes"]` is the wrong type (e.g. a string instead of a dict). The schema check correctly flags "must be an object", but the short-circuit only triggers on "Missing required" errors, so secondary checks proceed and crash on `labware.items()`. Fix: widen the short-circuit to also skip secondary checks when `config.get("labware")` or `config.get("pipettes")` isn't a dict. Pin-tested by `test_labware_wrong_type_currently_crashes`.
- **`validation/input_validator.py:InputValidator.classify`** mixes deterministic length-pre-checks with the LLM call in one method. Phase 1 (length checks) is therefore not unit-testable without an API key — tests work around it with monkeypatched env + mocked client. Fix: extract the length validation to a static method or module function.
- **`validation/constraints.py:WellStateTracker.get_volume`** conflates "well never tracked" with "well tracked at exactly 0uL" by returning `0.0` for both. Documented as an inspector-only quirk; internal logic does not branch on it.
- **`validation/constraints.py:WellStateTracker.get_substances`** returns a direct reference to the tracker's internal substance list rather than a copy — mutation by the caller mutates the tracker's state. Documented as a known leaky reference; pin-tested so a future copy-on-read fix surfaces.

## Roadmap

Concrete next-steps for the suite, in priority order:

1. **Coverage measurement.** Run `pytest --cov` regularly, document baseline coverage in this file, set a floor (e.g., "constraints checker stays above 90% line coverage").
2. **Property-based tests for the constraint checker** using [Hypothesis](https://hypothesis.readthedocs.io/). The checker has clear invariants suitable for property-based testing — e.g., *for any spec, if every requested volume is outside every loaded pipette's range, the checker must produce at least one volume violation.* This catches edge cases that hand-written tests miss.
3. **Boundary-value tests promoted to a named module.** Pull edge-of-range tests into a `test_boundaries.py` so the boundary discipline is explicit and the cases are easy to extend.
4. **Integration tests with a mocked Anthropic client.** The pipeline as a whole — extractor → verifier → constraint checker → schema_builder → script generator — is currently only tested end-to-end via real LLM calls. Mocking the client lets the pipeline-shape contract be tested deterministically.
5. **Pydantic-validator tests.** Confirm the `Provenance` validator (e.g., `ProvenancedVolume.unit` is required) rejects malformed LLM output at parse time, not just at downstream-use time.
6. **Eval set for Stage 8 intent verification.** A small held-out set of (instruction, script, expected verdict) triples to measure the intent verifier's precision/recall over time. Gated on having time to label the set; tracked alongside the [ADR-0003](adr/0003-cited-text-provenance.md) eval-set work.

Items 1–3 are the highest-leverage additions and are tracked separately in the project's task list.
