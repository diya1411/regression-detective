# Test report

## Method

Ground truth comes from construction: [eval/build_fixtures.py](../eval/build_fixtures.py) builds a small
but realistic `checkout-service` repository for each scenario in the design's test plan (§10). Each one has
a defect, or a deliberate non-defect, injected at a known commit, plus an exported CI history, a PR/issue
history and a CI failure archive. The builder is deterministic (fixed authors, dates and content), so the
commit SHAs are identical on every machine. [eval/run_eval.py](../eval/run_eval.py) runs the full agent on
each case in the Docker sandbox and scores the report against `truth.json`. No judgement is involved.

```bash
python -m eval.build_fixtures
python -m eval.run_eval --offline                         # → eval/results/results.md
python -m eval.run_eval --offline --no-history --tag no-history
```

Both runs below are **offline** (no LLM; deterministic fallbacks for symptom parsing, explanation and
patching). The Gemini code path is covered by `tests/test_llm_e2e.py` with a fake client. Rerunning without
`--offline` once a key is set gives the LLM-on numbers.

## Cases and pass criteria

| # | scenario | pass criterion |
|---|---|---|
| 1 | single defect commit, clean 32-commit range | correct SHA, classified *defect*, ≤ 6 bisection probes |
| 2 | genuinely flaky test (set-iteration order varies per process) | reports *flaky*, never starts a bisection |
| 3 | dependency minor release breaks the test, repository code unchanged | names the package, H1 eliminated, no culprit commit |
| 4 | defect commit next to two unbuildable commits (install failure, syntax error) | correct SHA despite skips |
| 5 | intentional behaviour change (ADR-0007), test is stale | correct SHA, classified *stale test*, patch edits the test |
| 6 | two independent defects | both commits found, status *multi-cause* |
| 7 | culprit outside the window (CI wrongly reported last-good as green) | *window invalid*, no culprit named |
| 8 | 64 commits, probe budget of 3 | *budget exhausted*, no culprit named, true culprit inside the reported range |

## Results (offline, history index on)

| # | case | got | culprit(s) | classification | bisection probes / log2 min | confidence | time | result |
|---|---|---|---|---|---|---|---|---|
| 1 | single defect | resolved | c9f0b0d | defect | 4 / 5 | 0.977 | 9.6 s | ✅ |
| 2 | flaky | flaky | – | flaky | 0 / 4 | 0.986 | 4.6 s | ✅ |
| 3 | dependency release | dependency (rdfixture-money 1.0.0 → 1.1.0) | – | dependency change | 0 / 4 | 0.969 | 6.6 s | ✅ |
| 4 | unbuildable neighbours | resolved | 21c4b37 | defect | 3 / 4 | 0.984 | 10.5 s | ✅ |
| 5 | stale test | resolved | 3dab4ac | stale test | 3 / 4 | 0.985 | 8.5 s | ✅ |
| 6 | two defects | multi-cause | a3731b5, 11c403c | defect | 7 / 5 (two bisections) | 0.978 | 12.2 s | ✅ |
| 7 | window invalid | window invalid (passes at last-good~8) | – | – | 0 / 4 | 0.94 | 6.5 s | ✅ |
| 8 | budget exhausted | budget exhausted (8 candidates left) | – | – | 3 / 6 | 0.60 | 7.6 s | ✅ |

**Metrics**

- Isolation accuracy: 4/4 single-culprit cases, plus both culprits in the multi-cause case.
- **False-confidence rate: 0/8.** No wrong answer at confidence ≥ 0.7, and no wrong culprit ever named.
- Probe efficiency: at or below the log2 minimum on every single-bisection case. The AST relevance prior
  saves a probe in cases 1, 4 and 5. Case 6 runs two bisections over 32 and 23 commits.
- Early termination: case 2 stops after the flakiness check; case 3 after the pin test; case 7 after
  three window-extension probes.
- Mean wall-clock: 8.3 s per investigation, with cached dependency images (a cold first build adds about
  2–5 s per distinct requirement set).
- Unconfirmed conclusions are capped at 0.6 confidence by design, which is why case 8 sits below the 0.7
  threshold and its patch would be suppressed.

## RAG ablation (retrieval point one)

The same corpus with `--no-history` also scores 8/8, so correctness does not depend on the history index.
The index changes the agent's starting beliefs, and with them its confidence:

| case | priors with history index | priors without |
|---|---|---|
| c1 (pricing defect; archive has 3 similar pricing defects) | H1 0.72 · H2 0.05 · H3 0.16 · H4 0.05 · H5 0.03 | H1 0.50 · H2 0.15 · H3 0.20 · H4 0.10 · H5 0.05 |
| c2 (the same test was resolved as flaky before) | H1 0.36 · **H2 0.47** · H3 0.12 · H4 0.04 · H5 0.02 | H2 0.15 |

Final confidence with vs. without: c1 0.977 vs 0.960, c2 0.986 vs 0.920, c7 0.940 vs 0.904. On these small
repositories, probes are cheap enough that the first action, the flakiness check, is the same either way.
On a repository with expensive builds, the higher flakiness prior from the archive increases that check's
lead over the alternatives.

## Representative decision trace (case 6, two defects)

```
FLAKINESS    10 runs at last-good → 10 passed · HEAD 10/10 fail — deterministic · H2 eliminated by experiment
BISECTION    probe 10e7ad9 → FAIL · signature differs from HEAD (assert 123.41 == 129.90)
BISECTION    probe 8dc85de → PASS · probe f060ab4 → FAIL · probe a3731b5 → FAIL — 1 commit remains
BISECTION    revert a3731b5 at HEAD → still FAILS — new failure: assert 129.00 == 129.90
HYPOTHESIS   New hypothesis H6: Second defect after a3731b5 — tree expanded
BISECTION    probe 9b14c24 → PASS · probe d816064 → FAIL · probe 11c403c → FAIL   (each with a3731b5 reverted)
BISECTION    revert a3731b5, 11c403c at HEAD → PASS — counterfactual confirmed
ORCHESTRATOR Search converged — multiple causes: a3731b5, 11c403c
```

Run `python -m detective trace eval-c6` to print any case's full trace, including each decision's EIG and
cost.

## Unit and integration tests

`pytest`: 25 tests, all passing.

- `test_belief_policy.py`: entropy, Bayes updates, EIG of a midpoint probe (≈ P(H1) bits), relevance-weighted
  midpoint, confirmation required before *confirmed*, flakiness check before bisection, mass conservation on
  spawn.
- `test_tools_rag.py`: changed-line hunk parsing (context lines excluded), AST chunking, import closure,
  external-I/O scan, identifier tokenisation, BM25 exact-symbol ranking, RRF with corpus filters, static
  dependency discovery (`setup.py` read but never run, `-r` includes, pins), redaction, pytest-report parsing,
  symptom parsing, negation-aware intent detection.
- `test_llm.py`: Gemini wrapper model fallback on 404, schema passed as `response_json_schema`, redaction
  before sending, call budget, lenient JSON parsing.
- `test_llm_e2e.py`: a complete investigation with the LLM path **on**, using a fake Gemini client. The patch
  is the LLM's search/replace edit, validated in Docker. A claim citing a non-existent evidence id is rejected,
  and an author-marked inference is never upgraded to supported.
- `test_e2e.py`: complete offline investigations (cases 1 and 2) in the Docker sandbox.

## Known gaps

- The corpus is synthetic. Historical regression/fix pairs from real open-source repositories are the
  planned next addition.
- No measurements yet against the live Gemini API; with a key, run `python -m eval.run_eval --tag gemini`.
