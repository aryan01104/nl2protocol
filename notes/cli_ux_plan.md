# CLI UX Improvement Plan

## Context
The CLI currently has ~85 user-facing messages. Most are good, but 8 are cryptic (no explanation, no fix guidance), there's no spinner during 5-10s LLM calls, raw API errors leak through, stage numbering is broken (Stage 2b/7), and there's no end-of-run summary. Research from clig.dev and "12 Rules of CLI UX" says every error needs: what happened + how to fix.

## Changes (6 items, 5 files + 1 new file)

### 1. New file: `nl2protocol/spinner.py` (~40 lines)
Zero-dependency spinner using threading + sys.stderr. Context manager interface:
```python
with Spinner("Reasoning through protocol..."):
    response = client.messages.create(...)
```
- Braille dots animation, ASCII fallback for non-UTF8
- No-op when stderr is not a TTY (piped/CI)
- Used at 5 LLM call sites: input_validator, extractor (extract + refine), app (verify_intent_match), config_generator

### 2. Add `format_api_error()` to `nl2protocol/errors.py` (~25 lines)
Catches Anthropic API errors and returns human-friendly strings:
- AuthenticationError → "Check your ANTHROPIC_API_KEY"
- "credit balance" → "Add credits at console.anthropic.com"
- RateLimitError → "Wait 30-60 seconds and retry"
- Timeout → "Check your network connection"

### 3. Rewrite 8 cryptic messages
Each gets: what happened + likely cause + what to try.

| File | Current message | Fix |
|------|----------------|-----|
| app.py:543 | "Failed to understand instruction" | Add likely causes + example instruction |
| app.py:600 | "Refinement failed" | Explain what refinement is + suggest adding detail |
| app.py:658 | "Deterministic conversion failed: {e}" | Classify the exception, suggest config check |
| app.py:675 | "Script generation failed: {e}" | Suggest simplifying instruction |
| app.py:~142 | "Verification error: {e}" to stdout | Downgrade to quiet stderr warning (non-fatal) |
| cli.py:526 | "Could not infer configuration" | Suggest using -c instead, give example |
| cli.py:580 | "Failed after N attempts" | Point to stage output above, list common fixes |
| input_validator.py:143 | "Input validation error: {e}" (raw API) | Catch specific API errors, skip gracefully |

### 4. Renumber stages 1-8
| New | Description |
|-----|-------------|
| 1/8 | Validating input |
| 2/8 | Reasoning through instruction |
| 3/8 | Validating and completing spec |
| 4/8 | Checking hardware constraints |
| (confirmation prompt — no stage header) |
| 5/8 | Converting spec to schema |
| 6/8 | Generating Python script |
| 7/8 | Running simulation |
| 8/8 | Verifying intent match |

### 5. End-of-run summary
Print after successful generation:
```
────────────────────────────────
  Protocol generated successfully.
  Steps:     9 commands
  Labware:   4 items (slots 1,2,3,5)
  Pipettes:  p300 (left), p20 (right)
  Simulation: passed
  Output:    generated_protocol_20260420.py
────────────────────────────────
```

### 6. Route progress/errors to stderr
All `print()` in app.py `run_pipeline` → `print(..., file=sys.stderr)`. Only file paths in cli.py stay on stdout.

## Implementation order
1. `spinner.py` (standalone, no deps)
2. `errors.py` (add format_api_error)
3. `input_validator.py` (fix raw API error, add spinner)
4. `extractor.py` (add spinner to extract + refine)
5. `app.py` (renumber stages, rewrite messages, add spinner to verify, add summary, route to stderr)
6. `cli.py` (rewrite messages 6-7, call summary)

## Sources
- [clig.dev](https://clig.dev/) — Command Line Interface Guidelines
- [12 Rules of CLI UX](https://dev.to/chengyixu/the-12-rules-of-great-cli-ux-lessons-from-building-30-developer-tools-39o6)
- [UX Patterns for CLI Tools](https://lucasfcosta.com/2022/06/01/ux-patterns-cli-tools.html)
