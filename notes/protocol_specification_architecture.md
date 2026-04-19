# Protocol Specification Architecture (Draft)

Date: 2026-04-18
Status: Tabled for later discussion

## Core Insight

The system shouldn't guess parameters — it should achieve **sufficient specification** before generating. If we don't have enough information, ask the user rather than infer and hope.

## Protocol Structure

A protocol has a known shape. Parameters fill in the shape:

```
PROTOCOL: PCR Plate Setup
  PARAMETERS (must be known before execution):
    - master_mix_volume: 10.5 uL        ← user specified
    - template_volume: 2 uL             ← user specified
    - mix_reps: 3                        ← user specified
    - mix_volume: 10 uL                  ← user specified
    - n_samples: 24                      ← user specified (wells A1-H3)
    - tip_strategy: new tip per sample   ← user specified
    - which_pipette_for_template: p20    ← user specified
    - which_pipette_for_mastermix: ???   ← NOT specified, should ask

  STEPS:
    1. FOR each destination well (n=24):
         distribute master_mix_volume from source to dest
    2. FOR each sample (n=24):
         transfer template_volume from sample to dest
         mix mix_reps times at mix_volume
```

## Proposed Pipeline

```
1. IDENTIFY protocol type (from RAG, LLM knowledge, protocol library)
2. EXTRACT parameter template for that protocol type
   → which did user specify?
   → which have sensible defaults?
   → which are unknown?
3. ASK about unknowns (interactive, like Claude planning mode)
4. GENERATE schema with fully-specified parameters (no guessing)
5. VALIDATE + SIMULATE (existing pipeline)
```

Step 4 has no degrees of freedom for critical parameters. The LLM translates a fully-specified parameter set into schema format, not making scientific decisions.

## Approach Options

**Option A: Protocol templates** — define known protocol types with parameter schemas. LLM identifies template, extracts params, we ask about gaps. Rigid but reliable.

**Option B: LLM-inferred parameter list** — LLM says "here's what I need to know." We check what user provided, ask about rest. Flexible but parameter inference could hallucinate.

**Option C: Hybrid** — common protocols have templates (A). Novel instructions fall back to (B) with heavier validation.

## Connection to Ginkgo/OpenAI Work

Ginkgo's LLM chose reagent concentrations (creative part), but plate layout, total volume, replicate count, control wells were hardcoded in Pydantic model. LLM wasn't allowed to decide those.

Our insight maps to: **separate creative decisions from mechanical ones, resolve creative ones before generation starts.**

## Known Errors This Would Fix

- **pcr_mastermix volume change (10.5→20.0)**: With sufficient specification, pipette assignment would be resolved before generation. The system would know 10.5uL needs p20, not p300.
- **distribute multi-channel**: With protocol identification, the system would know "distribute to individual wells" implies single-channel.

## Open Questions

- Do we build a protocol template library or infer on the fly?
- How do we handle novel protocols not in any template?
- What's the right UX for asking clarifying questions?
