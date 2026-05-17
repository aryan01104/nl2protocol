# DRAFT — Engineering log post 01: Architectural limits we know about

**Status:** content brief — to be turned into a proper engineering-log post when the `/log` page lands.
**Source:** lifted from the original limitations page (2026-05-17), which conflated user-facing limits with architectural ones. The user-facing version stayed at `/limitations`; this content gets a different home.

---

## Frame

Honest engineering means documenting where the implementation knows it falls short. Below are the architectural limits I know about, organized by class. Each is tagged with whether it's a known bug I'll fix, an intentional design choice, a capacity constraint, or an infrastructure gap that needs bigger work.

**Tags**
- `known bug` — will fix; just hasn't landed yet
- `intentional` — by design at the current scope; may revisit
- `capacity` — operational limit (rate, concurrency, iteration cap)
- `infra gap` — needs bigger architectural work; not on the immediate roadmap

---

## Class 1 — Semantic correctness (load-bearing)

Cases where the system can produce wrong output even when nothing complains.

### The spec can be wrong without anyone noticing. [intentional]

Nothing in the pipeline checks "did the LLM extract every step the user asked for, in the right action types?" If the LLM silently drops a step or misclassifies one, the orchestrator and constraint checker see a smaller, well-formed spec and won't object. Load-bearing weakness for protocols with subtle phrasing. Mitigated only by the visual surface — the user reviews extracted spec in column 2.

### Citation disambiguation is not handled. [intentional]

If `"100uL"` appears multiple times in different roles ("Add 100uL of sample, then mix at 100uL volume"), the LLM picks one occurrence as the cite and the verifier trusts that pick. No formal check that the cited instance is the right instance.

### Null fields carry no provenance. [intentional]

Populated values carry provenance; null values carry nothing. `step.source = None` could mean "correctly inferred there was no source" OR "forgot to extract a source that was actually there." Indistinguishable downstream. Mitigated by gap detectors flagging missing *required* fields, but truly-optional nulls are unauditable.

### No semantic-equivalence check between spec and generated script. [intentional]

The Opentrons simulator catches code-execution problems. It does NOT validate that the script does what the user asked for. User's eye is the final arbiter.

---

## Class 2 — Known false-positives

Verifier complaints that fire on legitimate extractions.

### Spread-citation wells trigger spurious fabrication warnings. [known bug]

`LocationRef.wells` is a `List[str]`, but `LocationRef.wells_provenance` is ONE Provenance for the whole list. Verifier checks each well against any cite entry via substring match. When wells extracted from multi-bullet instructions have cites that don't literally name each well, false-positive fabrication fires per missing well. Root cause: schema shape (per-well provenance would eliminate the class). Real fix is a schema migration. See `docs/GAP_LIFECYCLE.md` Concern 1.

### Citation values that don't literally appear in the cite text. [known bug]

Substring matching, no semantic understanding. Cite says `"top row"`, value is `"A1"` → false positive. Synonyms, paraphrases, abbreviations all hit this. Fix requires per-element cite alignment or a semantic check; both non-trivial.

### Gap modal "Current:" label shows offending value, not field state. [known bug]

Multi-element verifier complaints deduplicate to ONE Gap, but the displayed `current_value` is the last offending element. Modal reads "Current: B2" when the actual field is `[B1, B2, B3, B4]`. UI fix.

---

## Class 3 — Capacity limits

Operational limits on the live demo.

### One pipeline at a time, demo-wide. [capacity]

Live demo runs on a single Fly machine, single-process. Mid-run requests get "demo busy" page. Multi-user requires session-keyed thread bridges + worker queue.

### 5 pipeline runs per IP per hour. [capacity]

Per-IP rate limit on `POST /start`. Defense in depth on top of BYO-key. Plenty of headroom for legitimate use.

### Gap-resolution loop caps at 3 iterations. [capacity]

Detect → suggest → review → apply max 3 passes. Pipeline halts if gaps remain. Safety net against pathological loops; convergence usually happens in 1 pass.

---

## Class 4 — Infrastructure gaps

Architectural shortcuts taken to ship a working portfolio demo.

### Single-user assumption baked into live-mode model. [infra gap]

Thread-bridge between orchestrator and browser handler assumes one pipeline per process. Two concurrent users would race for the bridge. Single-pipeline lock prevents the race, but multi-user requires session-keyed bridges.

### No persistence. [infra gap]

Pipeline state in memory during run; static HTML reports on local disk. Cloud-scale needs database + object storage.

### No authentication. [infra gap]

BYO-key is the only access control today. Acceptable at portfolio scale; not at paid scale.

### No retry on extraction-time LLM hallucinations. [intentional]

If Sonnet drops or invents a step, no automated retry-with-feedback. User catches it in visual surface or doesn't. Adding retry costs real tokens, may not improve precision enough to justify until eval set exists.

---

## When this becomes a post

This isn't a finished log post — it's source material. The eventual post should:
- Open with the *meta-point*: why publishing architectural limits matters (anti-RAG-tool positioning, builds reader trust, signals seniority)
- Possibly group by "what's the user impact" rather than the current "what's the bucket"
- Link to the concrete code in `docs/GAP_LIFECYCLE.md` and `docs/PIPELINE_CALL_GRAPH.md` for readers who want to dig
- Possibly include a "what fixing this looks like" section per item — turns documentation into a roadmap
