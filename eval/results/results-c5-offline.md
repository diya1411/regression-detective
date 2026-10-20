# Evaluation results (offline, history index on)

- Cases: 1  ·  accuracy **100%**
- False-confidence rate: **0%**  ·  wrong culprits named: 0
- Mean wall-clock per investigation: 4.4s

| # | case | expected | got | culprit(s) | classification | bisection probes / log2 min | confidence | time | result |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Intentional behaviour change; the test is stale | resolved | resolved | 3dab4ac | stale_test | 3 / 4 | 0.985 | 4.4s | ✅ |
