# Deep distractor run, 2026-08-19

This follow-up expands the original distractor experiment from 10 to 30 jobs per cell to
50 jobs per cell and adds a 216-distractor rung. It keeps the MCP sidecar, DeepSeek Chat,
seed 1729, five tasks, twelve-turn cap, enterprise-sized backend records, and vague task
wording fixed.

The main sweep contains 250 graded jobs. Two controls add 100 more, for 350 new jobs and
645 model-graded jobs across the original study and this follow-up.

## Main sweep

All distractors in this table are same-domain near misses such as `list_incidents`,
`list_cases`, and versioned copies of `list_accounts`. Each row contains ten repeats over
the same five tasks.

| near-miss distractors | n | success | turns per job | calls per job | wrong calls per job | wrong-call share | tool tokens | schema use | median time | cost per job |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 50 | 78% | 3.00 | 3.26 | 0.12 | 3.7% | 2,349 | 25.3% | 5.8 s | $0.00316 |
| 18 | 50 | 62% | 3.96 | 8.48 | 3.90 | 46.0% | 3,585 | 20.5% | 6.7 s | $0.00638 |
| 54 | 50 | 64% | 4.78 | 10.64 | 6.42 | 60.3% | 6,129 | 14.6% | 10.8 s | $0.00713 |
| 108 | 50 | 58% | 4.46 | 12.58 | 7.58 | 60.3% | 9,945 | 10.3% | 9.7 s | $0.00798 |
| 216 | 50 | 64% | 5.42 | 15.96 | 11.28 | 70.7% | 17,577 | 6.9% | 12.8 s | $0.01376 |

Success does not move monotonically, and the differences among the four crowded cells
remain too small to rank. Calls, wrong calls, schema use, and cost form the useful curve.
From 0 to 216 distractors, calls rose 4.9 times, wrong calls rose from 0.12 to 11.28 per
job, and the recorded cost rose 4.4 times.

The wrong-call counter is exact per invocation. The harness also stores a deduplicated
list of tool names touched during each job, which is useful for naming offenders and
should not be used as an invocation count.

## High-count controls

The controls hold the catalog at 216 distractors and change one factor at a time.

| request wording | distractor type | n | success | turns per job | calls per job | wrong calls per job | injected distractors called | median time | cost per job |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| specific | lookalike | 50 | 100% | 2.60 | 2.60 | 0 | 0 | 5.3 s | $0.00409 |
| vague | unrelated | 50 | 64% | 2.94 | 3.92 | 0.14 | 0 | 6.2 s | $0.00681 |
| vague | lookalike | 50 | 64% | 5.42 | 15.96 | 11.28 | many | 12.8 s | $0.01376 |

The seven off-task calls in the vague, unrelated control all used the real `get_ticket`
tool. The model never called any of the 216 injected tools. Specific wording also removed
the routing problem completely, even with 216 lookalikes and a 17,577-token tool block.

The interaction is the finding. Large catalogs always add schema cost. Large catalogs
create call bloat when the request is indirect and several tools sound like plausible
routes to the same work.

## The effect is concentrated by task

At 216 lookalike distractors:

| task | success | turns per job | calls per job | wrong calls per job |
| --- | ---: | ---: | ---: | ---: |
| triage on call | 2/10 | 5.8 | 25.1 | 13.4 |
| restock low stock | 10/10 | 3.0 | 4.0 | 0 |
| escalate top customer | 0/10 | 8.9 | 35.6 | 31.4 |
| count unassigned open | 10/10 | 2.0 | 1.0 | 0 |
| largest enterprise EMEA | 10/10 | 7.4 | 14.1 | 11.6 |

Two workflows stayed perfectly stable at every rung. The customer-escalation workflow
accounted for most of the bloat because account, case, customer, and ticket tools all
looked like possible entry points. Catalog size defines the failure surface. The request
and workflow determine whether the agent enters it.

## Limits

- These are repeated runs over five fixed tasks. Fifty jobs per cell stabilize behavior
  within those tasks and do not expand the number of independent workloads.
- `deepseek-chat` is a provider alias. The provider can update the model behind the alias,
  so this follow-up is a fresh measurement rather than a pooled continuation of the
  August 8 data.
- The pricing table marks DeepSeek rates as unverified. Dollar figures are the harness's
  recorded estimates under `bench/pricing.yaml` version `2026-08-07`. Token counts and
  call counts do not depend on that pricing assumption.
- One 18-distractor job had a 317-second provider-latency outlier. The table reports
  median time, while call, turn, token, and cost totals retain the full run.
- Temperature zero reduces variation and does not make hosted model calls deterministic.

## Raw files

- `agentic-20260819T154753Z.jsonl`: 0 near-miss distractors, vague requests
- `agentic-20260819T155355Z.jsonl`: 18 near-miss distractors, vague requests
- `agentic-20260819T161030Z.jsonl`: 54 near-miss distractors, vague requests
- `agentic-20260819T162231Z.jsonl`: 108 near-miss distractors, vague requests
- `agentic-20260819T163400Z.jsonl`: 216 near-miss distractors, vague requests
- `agentic-20260819T174718Z.jsonl`: 216 near-miss distractors, specific requests
- `agentic-20260819T175200Z.jsonl`: 216 unrelated distractors, vague requests

All seven files are in [`results-reference/`](../results-reference/). Together they
contain 350 trial records, no provider errors, and three turn-cap outcomes.
