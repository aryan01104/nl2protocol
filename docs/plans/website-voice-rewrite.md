# Website voice rewrite — make the copy not read as AI-generated

Branch: `website-voice-rewrite`. Goal: keep the information and structure; change the prose so it reads as written by one person (Aryan), not generated. Weight copy over design — the layout is fine.

## The tells to remove (assessed from current copy)

1. **Em-dash dramatic pause.** Overused `—` for appositives/reveals. ("review job instead — every value…", "loudly — no silent broken protocols").
2. **Negation-contrast snap.** "X — not Y", "a check, not a guarantee", trailing punchy fragments ("Real schema change.", "The user's eye is the only check.").
3. **"Other tools X and hope. Here, Y."** setup→payoff contrast. ("generate code and hope", "produce output and hope you don't look closely").
4. **Self-referential emphasis.** "that's the point", "the whole point of the project", "publishing this is the differentiator".
5. **Honesty meta-commentary.** "Honest about…", "Honest engineering means…", "the system has receipts".
6. **Rule-of-three + "etc."** ("transfers, PCR, magnetic-bead cleanup, ELISA, etc.", "Architectural drift, contract gaps, design tradeoffs").
7. **Emphasis fragments + uniform typographic polish.** Machine-consistent `&mdash; &ndash; &rsquo; &ldquo; &hellip;`.
8. **Parallel marketing headers.** "Every value carries provenance." / "The script is simulator-verified."

## Rewrite rules (apply per tell)

- **Em-dashes:** cap at roughly one per section. Replace the rest with a period (split the sentence), a comma, or parentheses. Prefer ending sentences on the noun, not on a tacked-on reveal.
- **Negation-contrast:** keep at most one per page. Convert the rest to a plain positive statement of what happens.
- **Contrast cliché ("X and hope"):** state the actual mechanism instead. Say what other tools do without the rhetorical "and hope".
- **Self-reference / honesty meta:** delete. Let the provenance feature and the limitations page demonstrate the claim instead of announcing it.
- **Triads:** cut to two items or to one concrete example; drop "etc." where the list is illustrative.
- **Typography:** allow some inconsistency. Straight quotes/apostrophes are acceptable and read more human. Don't hand-polish every entity.
- **Headers:** make a couple of them plainer/less parallel so the set doesn't scan as a generated rhythm.
- **Voice anchor:** first person where natural ("I built", "I haven't tested"), specific over sweeping, occasional informality. Match the README's voice (check it first as the reference register).

## Scope — files, in order

1. `website/index.html` — hero pitch, deck, aside, "How it works" bodies, "What's different" (highest tell density; biggest payoff).
2. `website/limitations/index.html` — class ledes + limit bodies; the "Honest about…" meta description.
3. `website/docs/index.html` — ledes, callouts, surface descriptions.
4. `website/log/index.html` + `website/log/01-architectural-limits/index.html` — "Honest engineering means…", "publishing this is the differentiator", fix-plan prose.
5. `<meta description>` / `og:` tags in each file — they repeat the same generated phrasing.

Leave untouched: CSS, layout, code blocks, field-reference tables, the cited-span hover mechanic (the `data-prov` text on the hero is part of the product demo, not marketing copy — rewrite only if it reads generated).

## Process

1. Pull the README voice as the reference register before editing.
2. Rewrite page by page, top of scope list down. Diff each page's prose only.
3. After each page, re-scan for the 8 tells; don't introduce new ones (a rewrite can drift into a *different* generated cadence).
4. Optional check: read the rewritten hero aloud — if it sounds like a pitch deck, redo it.
5. View pages in the browser to confirm nothing broke structurally (entities, links).
6. Hold the commit/push until the user reviews the rewritten copy.

## Decisions (from user, 2026-06-18)

- **Register:** polished.
- **Person:** impersonal (no first-person "I").
- **Academic figure / numbered-claims framing:** keep for now.
- **Reference voice:** README.md — dense, fact-specific (file names, exact counts, "Naive:/Constraint:" framing), no marketing-contrast rhetoric. Bring the site toward that matter-of-fact density.
