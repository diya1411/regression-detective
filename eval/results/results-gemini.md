# Evaluation results (gemini, history index on)

- Cases: 8  ·  accuracy **100%**
- False-confidence rate: **0%**  ·  wrong culprits named: 0
- Mean wall-clock per investigation: 102.3s

| # | case | expected | got | culprit(s) | classification | bisection probes / log2 min | confidence | time | result |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Single defect commit, clean 32-commit range | resolved | resolved | c9f0b0d | defect | 4 / 5 | 0.979 | 128.3s | ✅ |
| 2 | Genuinely flaky test, no real regression | flaky | flaky | – | flaky | 0 / 4 | 0.97 | 78.0s | ✅ |
| 3 | Dependency minor release breaks the test; repository code unchanged | dependency | dependency | – | dependency_change | 0 / 4 | 0.97 | 49.4s | ✅ |
| 4 | Defect commit adjacent to two commits that fail to build | resolved | resolved | 21c4b37 | defect | 3 / 4 | 0.975 | 147.1s | ✅ |
| 5 | Intentional behaviour change; the test is stale | resolved | resolved | 3dab4ac | stale_test | 3 / 4 | 0.975 | 144.9s | ✅ |
| 6 | Two independent defects in the range | multi_cause | multi_cause | a3731b5, 11c403c | defect | 7 / 5 | 0.986 | 114.4s | ✅ |
| 7 | Culprit outside the search window (bad last-good assumption) | window_invalid | window_invalid | – | window_invalid | 0 / 4 | 0.94 | 82.2s | ✅ |
| 8 | Budget exhausted mid-bisection | budget_exhausted | budget_exhausted | – | unclear | 3 / 6 | 0.6 | 74.3s | ✅ |
