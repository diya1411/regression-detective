# Regression Detective

**An autonomous agent for root-cause diagnosis of CI test regressions.**

Give it a repository and one failing test. It proposes competing explanations (bad commit, flaky test,
dependency release, CI environment change, external fixture change) and then *runs experiments* to tell them
apart. It checks out historical commits into a sandboxed throwaway workspace, installs their dependencies and runs the
test. Each result updates a Bayesian hypothesis tree and decides the next experiment. When the evidence
converges, it confirms the culprit with a counterfactual (revert it at HEAD and show that the test passes),
explains the mechanism with retrieval over the repository's code, docs and PR history, and validates a
minimal patch. An adversarial verifier then checks that every claim in the report traces back to a
recorded tool observation.

```
triage → hypothesize → orchestrate ⇄ dispatch (flakiness · bisection · dependency · environment)
                           └→ explain → patch ⟲ → verify → report + draft PR
```

On the 8-case ground-truth corpus it gets **8/8 correct with a 0% false-confidence rate**, both with live
Gemini (~100 s per investigation) and offline (~8 s) (see [docs/test-report.md](docs/test-report.md)).

---

## Quick start

Requirements: Python ≥ 3.10 and git. Nothing else — no Docker.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # add GEMINI_API_KEY (optional; without it the agents use deterministic fallbacks)

python -m eval.build_fixtures  # build the 8 ground-truth repositories under eval/fixtures/
python -m detective serve      # http://127.0.0.1:8000 — pick an eval case, press Investigate
```

Command line:

```bash
# investigate any local repo (or a GitHub URL)
python -m detective investigate --repo path/to/repo --test tests/test_cart.py::test_checkout_total \
    --last-good <sha> [--meta dir] [--offline] [--max-probes 16] [--open-pr]

python -m eval.run_eval [--offline] [--no-history]   # score the agent against ground truth
python -m detective trace last                      # decision trace of the latest investigation
python -m detective graph                           # the agent graph as Mermaid (generated from code)
pytest                                              # unit + end-to-end tests
```

`demo/storyboard.html` is a scripted, backend-free storyboard of one investigation, kept as a fallback for
presentations. The real UI can **replay any recorded investigation** at 1×, 3× or 8×, because every run is an
append-only event log.

## Using Gemini

Set `GEMINI_API_KEY` in `.env`. `GEMINI_MODELS` is an ordered fallback list (default
`gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash,gemini-2.5-flash`), and the first model your key can
access is used. Gemini is used for:

| Agent | What the LLM does | Output contract |
|---|---|---|
| Triage | symptom signature from raw failure output | JSON schema (`TRIAGE_SCHEMA`) |
| Hypothesis | tailored hypotheses, priors, elimination criteria | JSON schema + good/bad few-shots |
| Explanation | causal account, defect-vs-stale-test classification, cited claims | JSON schema with evidence ids |
| Patch | minimal search/replace edits, re-prompted with failure output | JSON schema, applied exactly |
| Verifier | adversarial audit of free-text claims against cited evidence | JSON schema |
| RAG | `gemini-embedding-001` dense vectors (hybrid with BM25) | — |

The orchestrator's decisions are **not** delegated to the LLM. Choosing the next experiment is computed
(expected information gain ÷ estimated cost), so every decision can be explained with numbers. All prompts
are in [detective/agents/prompts.py](detective/agents/prompts.py). Every call is logged to
`<investigation>/llm/`, and logs and code are redacted for credential-shaped strings before they are sent.

## How it maps to the design

| Design component | Code |
|---|---|
| Investigation Orchestrator (§4.1) | [agents/orchestrator.py](detective/agents/orchestrator.py), [policy.py](detective/policy.py), [belief.py](detective/belief.py) |
| Triage (§4.2) | [agents/triage.py](detective/agents/triage.py) |
| Hypothesis (§4.3) | [agents/hypothesis.py](detective/agents/hypothesis.py) |
| Flakiness (§4.4) | [agents/flakiness.py](detective/agents/flakiness.py) |
| Bisection (§4.5) | [agents/bisection.py](detective/agents/bisection.py) |
| Dependency (§4.6) | [agents/dependency.py](detective/agents/dependency.py) |
| Explanation (§4.7) | [agents/explanation.py](detective/agents/explanation.py) |
| Patch (§4.8) | [agents/patch.py](detective/agents/patch.py) |
| Evidence Verifier (§4.9) | [agents/verifier.py](detective/agents/verifier.py) |
| LangGraph control loop (§5) | [graph.py](detective/graph.py) |
| RAG, 4 corpora, hybrid BM25 + dense + RRF (§6) | [rag/](detective/rag) |
| Tools: git, GitHub, process sandbox, pytest JSON, PyPI, AST (§7) | [tools/](detective/tools), [sandbox/](detective/sandbox) |
| Visual search interface (§8) | [server/app.py](detective/server/app.py), [ui/](ui) |
| Safety (§9) | [sandbox/local_sandbox.py](detective/sandbox/local_sandbox.py), [tools/forge.py](detective/tools/forge.py), [redact.py](detective/redact.py), verifier |
| Test plan (§10) | [eval/](eval), [tests/](tests) |

Architecture details, likelihood models and the policy maths are in [docs/architecture.md](docs/architecture.md).

## What the agent actually does differently from a bisect script

- **Hypothesis tree as a probability distribution.** The leaves are both *kinds* of cause and *individual
  commits*, so one entropy covers "what kind of cause?" and "which commit?". Every action has a likelihood
  model, and evidence updates the tree by Bayes' rule.
- **Cost-aware next action.** Each candidate experiment is scored by expected information gain per estimated
  second. The cost model starts from priors and recalibrates from measured build and run times. Bisection waits
  until the flakiness check has verified its two assumptions: the good endpoint passes, and the test is
  deterministic.
- **The tree grows when evidence contradicts it.** If last-good fails in the sandbox, *"regression predates
  the window"* is added. If reverting the isolated culprit does not fix HEAD, *"second defect after X"* is
  added, and a second bisection runs with the first culprit reverted at every probe (eval case 6).
- **Nothing is named without a counterfactual.** A culprit commit is reported only once reverting it at HEAD
  makes the test pass. A dependency is reported only once pinning it back passes at unchanged code.
- **Unbuildable commits** are retried with the nearest buildable ancestor's dependency set (a repair probe).
  Collection errors are skipped, never counted as fails, and the next probe avoids them.
- **AST relevance prior.** Commits that don't touch the failing test's import closure get less probability
  mass. They are down-weighted, never excluded, so the weighted midpoint reaches the culprit faster.
- **Argued vs. tested.** An elimination by static analysis is marked *unreachable, not tested*. The verifier
  checks structured claims (probe verdicts, run counts, suite results) field by field against the evidence
  they cite, and never upgrades a claim its author marked as inferred.

## Safety

- **Execution:** each probe runs as a fresh subprocess on a throwaway `git archive` export in the system temp
  directory, with a scrubbed environment (no host variables or tokens), HTTP(S) proxies pointed at a dead port,
  its own process group (killed as a whole on the hard timeout), and CPU / file-size / open-file rlimits.
  Dependencies are installed once per requirement set into a cached virtualenv, from manifests only;
  `setup.py` is parsed statically and never executed. This is process-level containment, not a VM: raw
  sockets and reads of world-readable files are still possible, so only investigate repositories you trust.
- **Repository:** the investigated repo is only ever read; probes run on `git archive` exports. The agent
  can push only `regression-detective/*` branches (no force, never the default branch) and opens only
  *draft* PRs, which require `--open-pr`, a `GITHUB_TOKEN`, and a verifier pass above the confidence threshold.
- **Epistemic:** below the confidence threshold the patch is suppressed and the report becomes a findings
  summary that lists the missing evidence.
- **Resources:** each run has a declared budget of probes, wall-clock time and LLM calls, shown live in the UI.
- **Data:** logs and code are redacted (tokens, keys, credentials in URLs, private keys) before any LLM call.
  `--offline` keeps everything local.

## Repository layout

```
detective/
  agents/      orchestrator + 9 specialists, prompts and schemas
  tools/       git, forge (GitHub REST / local export), PyPI registry, pytest report, AST
  sandbox/     process sandbox (cached virtualenvs), dependency discovery, the runner each probe executes
  rag/         corpora (code / docs / history / CI archive), BM25 + dense + RRF
  belief.py    hypothesis tree as a distribution;  policy.py  likelihoods, EIG/cost, action selection
  graph.py     LangGraph wiring;  report.py  report + PR body;  server/  FastAPI + SSE
ui/            live investigation UI (vanilla JS, no build step)
eval/          ground-truth corpus builder and scorer;  results/  latest scored runs
tests/         unit tests, fake-Gemini end-to-end test, full end-to-end investigations
demo/          scripted storyboard (offline fallback)
.rd/           all run data: investigations, evidence, LLM logs, venv cache (gitignored)
docs/          architecture and test report
```

## Limitations

- **Gemini free tier:** 20 requests per model per day; a full 8-case eval needs ~40, so it leans on the model
  fallback list.
- **Eval corpus:** all eight cases are synthetic, so ground truth is known by construction. Mining historical
  open-source regression/fix pairs is the next step (§10).
- **GitHub forge:** written against the REST API but not yet exercised against a live repository.
- **Bisection assumes monotonicity:** a test that is flaky only under specific conditions can still defeat it.
- **Ecosystem:** Python + pytest only, one failing test per investigation, and deps installed from manifests
  with `PYTHONPATH` rather than building the project (compiled extensions aren't supported).
