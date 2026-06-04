# Evals — what to capture per run

Three-layer capture model for the 20-case eval suite. Layer 1 every run, Layer 2 when something interesting happens, Layer 3 once enough cases are graded for trends.

Each layer entry below is tagged:

- **AUTO** — emitted into the run dir by the pipeline; no work for you.
- **HAND** — your judgment, written into the case's `actual.md`.

## How to run

Two paths produce the same artifact shape:

```bash
# Headless (cheap, scriptable, auto-accepts all confirmations)
python evals/run.py 01_lotterhos_magbead          # one run
python evals/run.py 01_lotterhos_magbead -n 3     # three runs
python evals/run.py --all                         # all 20

# Visual (slower, you drive the modals)
NL2PROTOCOL_LOCAL_DEV=1 python -m nl2protocol --serve --examples-dir evals
# then open http://127.0.0.1:8000/, pick from the dropdown, drive each case
```

Both write per-run artifacts to `evals/runs/<case>/run_<ts>/`:

| File | Contents | Status |
|---|---|---|
| `instruction.txt`, `config.json`, `expected.md`, `source.md` | Case fixtures (copied so the run dir is self-contained) | AUTO |
| `report_<ts>.html` | Static provenance report | AUTO |
| `metrics_<ts>.{md,json}` | Calls, tokens, wall-clock, per-stage breakdown | AUTO |
| `pipeline_state_<ts>.json` | Full spec + gap log + script (machine-readable) | AUTO |
| `debug_script_<ts>.py` | Generated Opentrons script (when pipeline succeeded) | AUTO |
| `console.txt` *(headless only)* | Full pipeline trace | AUTO |
| `prompts.txt` *(headless only)* | Every auto-accepted confirmation | AUTO |
| `result.json` | Run identity (case, timestamps, git_sha, error, `hand_grade: "TODO"`, `notes: ""`) | AUTO + HAND slots |

Grading lives in `evals/<case>/actual.md` — one file per case, hand-written, accumulating one block per run. Skeleton at the bottom of this doc.

## Layer 1 — minimum per case

Fill in for every run. Target: 5 minutes per case.

| Stat | Status | What it tells you |
|---|---|---|
| Outcome category | HAND | `code` / `ask` / `refuse` / `crash` — basic disposition. |
| Predicted gaps surfaced (N of M) | HAND | `expected.md` lists predicted gaps; how many actually came through? Direct measure of expectation calibration. |
| Surprise gaps | HAND | Questions/refusals the tool produced that you didn't predict. Where real learning happens. |
| Total LLM calls | AUTO | From `metrics_<ts>.md` ("LLM calls" row). |
| Total input tokens | AUTO | From `metrics_<ts>.md`. Scales with instruction length — feeds the token-vs-length Layer 3 scatter. |
| Total output tokens | AUTO | From `metrics_<ts>.md`. Tells you whether cost is in prompts or responses. |
| Wall-clock latency | AUTO | From `metrics_<ts>.md` ("Wall-clock") or `result.json.duration_s`. |
| One-paragraph observation | HAND | What went well, what went wrong, what was unexpected. Free text. |

## Layer 2 — when something interesting happens

Only worth filling in for non-trivial outcomes.

| Stat | Status | When to capture |
|---|---|---|
| Per-stage tokens + time breakdown | AUTO | Already in the per-stage table of `metrics_<ts>.md`. Read off when one stage dominates. |
| Question quality — targeted or scattershot? | HAND | When the tool asks 5+ questions; worth judging whether they were the right ones. |
| Generated code shape vs reference | HAND | When code is the outcome — does it match the structural arc in `expected.md`? Qualitative until a simulator state-trace comparison is written up. |
| Confidence numbers per cited value | AUTO (raw) | `pipeline_state_<ts>.json` carries `Provenance.confidence` on every value. Feeds a future heatmap. |
| Repeatability — same case 3×, note variance | HAND (judgment, AUTO data) | For 2–3 cases only. Run with `-n 3` (headless) or three visual passes; read the three `result.json` files. Tells you if the system is stable or noisy. |

## Layer 3 — aggregate across the 20 runs

After most cases are done. Future aggregation script can glob `evals/*/actual.md` (for prose) + `evals/runs/*/run_*/result.json` + `metrics_*.json` (for numbers).

| Aggregation | What it answers |
|---|---|
| Per protocol kind — outcomes + tokens + latency | Which kinds work; which kinds break the tool. |
| Per error type — outcome distribution | Where the error-detection layer is strong vs. weak. |
| Token cost vs. instruction length (scatter) | A/B pairs: `01_lotterhos_magbead` ↔ `15_magbead_compressed`, `06_openwetware_elisa_wash` ↔ `16_elisa_compressed`. |
| Question burden distribution | Over-asking on simple cases? Under-asking on complex ones? |
| vs. Claude Code — paired outcome buckets | The headline comparison: "tool catches what Claude Code (no config awareness) doesn't." |

## Where to grade

One `actual.md` per case, next to `expected.md`. Append one block per run. A roll-up script can later glob `evals/*/actual.md` for the Layer 3 prose fields.

### Per-case skeleton

Copy this into `evals/<case>/actual.md` (or append, if the file exists) after each run. Auto fields can be lifted straight off `metrics_<ts>.md` and `result.json` in the run dir.

```markdown
# actual — <case name>

## Run <ts> · commit <git_sha>
- Run dir: `evals/runs/<case>/run_<ts>/`
- Model: <from metrics_<ts>.md>
- LLM calls: <from metrics>
- Input tokens: <from metrics>
- Output tokens: <from metrics>
- Wall-clock: <from metrics>

### Outcome
- Category: <code / ask / refuse / crash>
- Predicted gaps surfaced: N of M
- Surprise gaps:

### Observation
<one paragraph: what went well / wrong / unexpected>

### Layer 2 (only if interesting)
- Stage breakdown:
- Question quality:
- Code shape vs. expected:
- Repeatability:
```
