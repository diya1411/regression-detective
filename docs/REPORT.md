# Regression Detective — Short Report

**An Autonomous Agent for Root-Cause Diagnosis of CI Test Regressions**

Repository: https://github.com/diya1411/regression-detective

---

## 1. Introduction

When a test that used to pass starts failing on the main branch, someone has to find out why. Usually that
means reading the failure, guessing at likely commits, checking them out one by one, ruling out flakiness
and dependency updates, and finally explaining and fixing the break. It is slow, repetitive work, and it is
easy to blame the wrong commit.

Regression Detective automates this work end to end. It is not a chatbot that answers questions about code.
It is an agent that **runs experiments**. It checks out historical commits in a sandbox, installs their
dependencies, runs the test, and uses each result to decide what to try next. When the evidence converges,
it explains the mechanism and proposes a validated patch as a draft pull request for a human to review.

## 2. Problem Statement

Given a repository and one failing test, determine *why* the test fails and what to do about it. The
difficulty is that the cause is not always a bad commit. It may be:

1. a commit inside the window since the test last passed,
2. a flaky (nondeterministic) test, with no real regression at all,
3. a dependency that published a new version,
4. a change in the CI environment, or
5. a change in external test data.

A plain `git bisect` script assumes cause 1, so it gives confidently wrong answers in the other cases. The
right next step depends on what earlier experiments revealed. That makes this a search under uncertainty,
which suits an agent rather than a fixed pipeline.

## 3. Objectives

1. Hold all five explanations as competing hypotheses and eliminate them **by experiment**, not by argument.
2. Choose each next experiment by its value: the information it is expected to yield per unit of cost.
3. Isolate the culprit commit in close to the theoretical minimum number of probes (log₂ of the window).
4. Never name a culprit without a **counterfactual** check: reverting it must make the test pass.
5. Explain the failure using the repository's own code, documentation and history, and distinguish a
   genuine defect from an intentional change whose test is out of date.
6. Propose a minimal patch, validate it against the full test suite, and open it only as a draft.
7. Make every claim in the final report traceable to recorded tool output.
8. Make the investigation visible live, so a viewer can see the agent reasoning.

## 4. Proposed Agent Architecture

The system has **one orchestrator and nine specialist agents**, wired as a LangGraph state machine with
two feedback loops (architecture diagram in the submission document).

| Agent | Role |
|---|---|
| **Investigation Orchestrator** | Owns the hypothesis tree and the budget, picks every next action, and decides when to stop. Never uses a tool directly. |
| Triage | Parses the failure into a symptom signature, establishes the commit window, and computes which commits touch the test's code (via the AST). |
| Hypothesis | Generates the five hypotheses with prior probabilities and elimination criteria, informed by similar past failures. |
| Flakiness | Runs the test N times at HEAD and at the last-good commit. |
| Bisection | Probes commits, repairs or skips unbuildable ones, runs the counterfactual revert, and extends the window backwards when needed. |
| Dependency | Diffs manifests, reconstructs package versions as of the last-good date from PyPI, and runs the pin-and-rerun experiment. |
| Environment | Compares CI configuration and runner metadata; statically scans the test path for external data access. |
| Explanation | Writes the causal account and the defect vs stale-test classification. |
| Patch | Proposes a minimal fix and validates it; a failed attempt loops back with the failure output. |
| Evidence Verifier | Audits every claim against the evidence log before the report is shown. |

**Planning.** The hypothesis tree is a probability distribution over both *kinds* of cause and
*individual commits*. Each possible action has a likelihood model (how probable each outcome is under each
hypothesis). The orchestrator computes each action's expected information gain, divides by its estimated
cost in seconds, and dispatches the best one, plus a second independent action in parallel. Cost estimates
start from defaults and are recalibrated from measured build and run times.

**Re-planning.** When evidence contradicts an assumption the tree was built on, the orchestrator adds a
new hypothesis instead of forcing the evidence to fit. Two cases trigger this:

- the last-good commit fails in the sandbox → "the regression predates the window"
- reverting the isolated culprit doesn't fix HEAD → "a second defect exists after it", which starts a
  second bisection with the first culprit reverted at every probe

**Stopping.** The investigation ends when a hypothesis passes both the probability threshold (0.9) *and*
its confirmation experiment, or the probe/time budget runs out (the narrowed range is reported), or no
remaining action can change the conclusion.

## 5. Prompt Design

Gemini is used where judgement over unstructured text is needed. The control decisions are computed, so
every one can be explained with numbers.

- **One system prompt per role** (Triage, Hypothesis, Explanation, Patch, Verifier), each with a narrow
  task and shared ground rules: use only the input, cite evidence ids exactly, prefer "unknown" over a guess.
- **Structured output.** Every call passes a JSON schema (`response_json_schema`), and responses are parsed
  and validated. Malformed or out-of-schema output falls back to deterministic logic.
- **Few-shot examples.** The Hypothesis prompt contrasts a good, falsifiable hypothesis (it names the
  experiment that would eliminate it) with a vague, untestable one, and gives calibrated base-rate priors.
- **Evidence-grounded claims.** The Explanation agent must return a list of claims, each citing evidence
  ids and marked *observed* or *inferred*. It may classify a change as an intentional behaviour change only
  if a retrieved document says so.
- **Adversarial self-critique.** The Verifier prompt is written as an auditor trying to find unsupported
  claims, and is told not to reward fluent writing. An inferred claim can never be upgraded to supported.
- **Feedback prompting.** On retry, the Patch prompt includes the previous attempt's diff and its failure
  output.
- **Operational safeguards.** Credential-shaped strings are redacted before every call, and calls are
  logged per investigation. The model fallback list (`gemini-3.8-flash` → `3.7` → `3.5` → `2.5`) skips any
  model whose quota is exhausted.

## 6. RAG Implementation

Four corpora are indexed per investigation.

| Corpus | Chunking | Used by |
|---|---|---|
| Code | function/class level via Python's AST, tagged with commit SHA | Explanation |
| Docs | per heading (README, design notes, ADRs) | Explanation |
| History | per closed issue / merged PR | Hypothesis, Explanation |
| CI archive | per past failure, with its eventual resolution | Hypothesis |

Retrieval is **hybrid**. BM25 runs over identifier-aware tokens (`line_total` also matches `line`,
`total`), because exact symbol names matter in code. It is combined with dense retrieval using Gemini
embeddings (`gemini-embedding-001`), and the two rankings are merged with reciprocal rank fusion.
Embeddings are cached, and files touched by the culprit are added to the index incrementally.

Retrieval does two jobs that change what the agent does:

1. **Seeding priors.** Similar past failures shift the starting probabilities. In one test, a past flaky
   resolution of the same test raised the flakiness prior from 0.15 to 0.47. Where the archive held three
   similar pricing defects, the bad-commit prior rose from 0.50 to 0.72.
2. **Grounding the explanation.** The agent retrieves the functions the culprit changed (before and after),
   the tests that cover them, related design documents, and PRs touching the same files. This is how it
   tells a defect apart from a stale test. In TC5 it found ADR-0007 declaring banker's rounding intentional,
   so it fixed the test rather than the code.

## 7. Tool / API Integration

All tools are real; none is simulated.

| Tool | Used for |
|---|---|
| **Process sandbox** | exports a commit, installs its dependencies into a cached virtualenv, runs pytest |
| **Git (CLI)** | commit ranges, diffs, file history, revert patches |
| **pytest + JSON report** | structured pass/fail, assertion messages, failure signatures |
| **PyPI JSON API** | release dates, used to reconstruct the dependency versions as of the last-good date |
| **GitHub REST API** | CI run history, issues and PRs for the retrieval corpora, opening the draft PR |
| **Python AST** | semantic chunking, the test's import closure, mapping diffs to functions, external-I/O scan |
| **Gemini API** | structured generation and embeddings |

The sandbox actions **change state**, and their results drive the next decision. Build failures are
treated differently from test failures:

- a dependency-install failure is retried with the nearest buildable commit's dependencies
- a syntax error is skipped, never counted as a failure
- every probe has a hard timeout

## 8. Agent Workflow

1. **Triage** reproduces the failure, extracts the symptom, sets the window, and down-weights commits that
   don't touch the test's code.
2. **Hypothesis** retrieves similar failures and seeds the five priors.
3. **Orchestrator loop**:
   1. score every available action by information gain ÷ cost
   2. dispatch the best (one or two in parallel)
   3. absorb the evidence with Bayes' rule, prune the commit range, and spawn hypotheses if needed
   4. check whether to stop

   Typically the flakiness check runs first, because it verifies bisection's own assumptions.
4. **Explanation** retrieves context and produces the classified causal account with cited claims.
5. **Patch** proposes a fix, validates it against the failing test and the full suite, and loops on failure.
6. **Verifier** checks every claim, computes confidence, and suppresses the patch if confidence is below
   threshold.
7. **Report** produces JSON + Markdown and a draft PR proposal; the UI shows the final state.

An example trace (TC6, two defects):

1. Flakiness check: deterministic.
2. Four probes isolate `a3731b5`.
3. Reverting it at HEAD still fails, with a *different* assertion value.
4. A "second defect" hypothesis is added to the tree.
5. Three probes, each with `a3731b5` reverted, isolate `11c403c`.
6. Reverting both makes the test pass, and the agent reports a multi-cause regression.

## 9. Safety Mechanism

- **Execution safety.** Historical commits can contain anything, so each probe runs as a fresh subprocess
  on a throwaway export outside any repository, with:
  - a scrubbed environment (no host tokens or variables)
  - HTTP(S) traffic routed to a dead proxy
  - its own process group, killed as a whole on timeout
  - CPU, file-size and open-file limits

  `setup.py` is parsed statically and never executed. This is process-level containment rather than a VM,
  which is stated in every report.
- **Repository safety.** The investigated repository is only ever read. The agent may push only branches
  named `regression-detective/*`, never the default branch, never with force, and it opens only *draft*
  PRs.
- **Epistemic safety.**
  - No culprit is named without a counterfactual experiment.
  - Eliminations by static analysis are marked "unreachable, not tested".
  - Every claim must cite recorded evidence, and an invented evidence id is rejected.
  - Below the confidence threshold the patch is suppressed and the output becomes a findings summary that
    lists what is still missing.
- **Resource ethics.** Each run has a declared budget of probes, wall-clock time and LLM calls, shown live
  in the UI.
- **Data handling.** Logs and code are redacted for credentials before any LLM call. An offline mode keeps
  everything local.

## 10. Results

The evaluation corpus has eight ground-truth cases covering:

- a single defect
- a flaky test
- a dependency release
- unbuildable commits
- a stale test
- two independent defects
- a wrong search window
- budget exhaustion

Results:

- **With Gemini:** 8/8 correct, false-confidence rate 0%, no wrong commit blamed, no claim rejected by the
  verifier.
- **Offline (no LLM):** 8/8 correct, which shows the reasoning does not depend on the language model.
- **History index disabled:** still 8/8, but with lower starting confidence.
- **Probe efficiency:** at or below the log₂ minimum on every single-culprit case. The AST relevance prior
  saved a probe in three of them.
- **Time:** about 100 s per investigation with Gemini, about 8 s offline.
- **Tests:** 26 automated tests pass.

## 11. Limitations

- The evaluation corpus is synthetic. Real open-source regression/fix pairs would make it more convincing.
- Bisection assumes the test changes from pass to fail once. A test that is flaky only under specific
  conditions can still defeat it.
- One failing test per investigation. Forty tests broken by one change would first need clustering.
- Python and pytest only. Projects are run from `PYTHONPATH` rather than built, so compiled extensions
  are not supported.
- The sandbox limits processes but is not a VM, so only trusted repositories should be investigated.
- A patch that passes the suite is not necessarily the right fix, which is why it is only ever a draft.
- On Gemini's free tier (20 requests per model per day), a full eight-case evaluation needs the model
  fallback list.

## 12. Future Scope

- Cluster simultaneous failures by shared suspect code and run one investigation per cluster.
- Learn likelihoods and priors from a repository's own resolved-failure history, so the agent improves on
  that codebase over time.
- A proactive mode that estimates the regression risk of incoming pull requests before merge.
- An interactive mode where a developer injects their own hypothesis mid-investigation and the agent tests
  it, making the hypothesis tree collaborative.
- Adapters for other ecosystems (npm/Jest, Maven/JUnit) and a mined corpus of historical open-source
  regressions.
