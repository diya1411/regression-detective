# Evaluation results (offline, history index off)

- Cases: 8  ·  accuracy **100%**
- False-confidence rate: **0%**  ·  wrong culprits named: 0
- Mean wall-clock per investigation: 8.2s

| # | case | expected | got | culprit(s) | classification | bisection probes / log2 min | confidence | time | result |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Single defect commit, clean 32-commit range | resolved | resolved | c9f0b0d | defect | 4 / 5 | 0.96 | 9.6s | ✅ |
| 2 | Genuinely flaky test, no real regression | flaky | flaky | – | flaky | 0 / 4 | 0.92 | 4.6s | ✅ |
| 3 | Dependency minor release breaks the test; repository code unchanged | dependency | dependency | – | dependency_change | 0 / 4 | 0.968 | 6.6s | ✅ |
| 4 | Defect commit adjacent to two commits that fail to build | resolved | resolved | 21c4b37 | defect | 3 / 4 | 0.969 | 10.2s | ✅ |
| 5 | Intentional behaviour change; the test is stale | resolved | resolved | 3dab4ac | stale_test | 3 / 4 | 0.973 | 8.6s | ✅ |
| 6 | Two independent defects in the range | multi_cause | multi_cause | a3731b5, 11c403c | defect | 7 / 5 | 0.962 | 12.1s | ✅ |
| 7 | Culprit outside the search window (bad last-good assumption) | window_invalid | window_invalid | – | window_invalid | 0 / 4 | 0.904 | 6.4s | ✅ |
| 8 | Budget exhausted mid-bisection | budget_exhausted | budget_exhausted | – | unclear | 3 / 6 | 0.6 | 7.6s | ✅ |
