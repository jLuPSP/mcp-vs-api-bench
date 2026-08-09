# mcp-vs-api-bench

A benchmark that answers, with numbers, whether an agent should use MCP or just call the
API directly. One backend, five tool-access layers, 435 graded trials, under a dollar.

**The short answer: protocol choice is the second most important decision, and curating
the tool list is the first.** MCP costs 2 to 4 ms per call and about twice the schema
tokens. Both are small and both are fixable. What actually breaks agents is loading a
large, overlapping tool surface, and that hurts a direct integration just as badly.

Full writeup with the security threat model: [`docs/decision-brief.html`](docs/decision-brief.html).
Raw data behind every number: [`results-reference/`](results-reference/) and [`RESULTS.md`](RESULTS.md).

---

## Findings

### 1. Latency is a non-issue

No model in the loop, backend pinned at 40 ms, no injected network delay.

| arm | overhead p50 |
|---|---:|
| `direct` | 1.15 ms |
| `mcp_stdio` | 3.35 ms |
| `mcp_sidecar` | 5.00 ms |
| `mcp_remote` | 5.28 ms |

Against a model turn of 800 to 5,000 ms this is noise.

Two numbers worth keeping. A **remote gateway costs 2.0x the one-way RTT per call**, not
1x, because a streamable-HTTP tool call makes two client-facing trips (measured 1.92x /
2.01x / 2.00x at 10 / 25 / 50 ms). And a client that re-handshakes per request measured
**1015 ms per call against 3.35 ms pooled**, roughly 300x. That is a connection-pooling
bug, not a property of MCP, and it is the first thing to check if someone tells you MCP
was slow in production.

```bash
make micro          # the table above
make micro-naive    # the ~300x unpooled result
make netem-sweep    # the 2.0x multiplier
```

### 2. Tokens are the real cost, and smaller than expected

Identical eleven capabilities over the identical backend:

| arm | tools | schema tokens | utilisation |
|---|---:|---:|---:|
| `direct` (hand-written) | 11 | 1,080 | 24.6% |
| `mcp_sidecar` (server-advertised) | 11 | 2,309 | 25.6% |
| `mcp_filtered` (per-task subset) | 4 | 1,246 | 87.8% |

2.1x, because a general-purpose server writes descriptions for a consumer it has never
met. Caching does not rescue it: DeepSeek's prefix cache hit 92 to 96% and cost still
tripled as the tool list grew, since a hit rate cuts the rate you pay and does nothing
about sending six times the tokens.

### 3. Tool-count degradation is real, but only under two conditions at once

This is the result worth reading carefully, because the first version of it was wrong.

Sweeping 0 to 150 irrelevant tools produced **no degradation at all**: 100% success, zero
distractor calls. That held even for same-domain near-miss tools (`list_incidents`,
`list_service_requests`) through 108 of them.

The cause was my own prompts. They named their domain ("every support ticket that is open
and priority P1"), so the model routed on keyword overlap and never had to discriminate.
Rewriting the same tasks the way a person asks, with identical checkers:

| phrasing | extra tools | n | success | wrong-tool calls |
|---|---|---:|---:|---:|
| explicit | 108 near-miss | 10 | 100% | 0 |
| vague | none | 10 | 80% | 3 |
| vague | 54 cross-domain | 10 | 60% | 0 distractors |
| vague | 54 near-miss | 30 | 60% | 125 |

**It is an interaction.** Similar tools with a clear request are harmless. A vague request
with unrelated tools hurts success without causing a single wrong tool call, so that
failure is comprehension, not selection. You need both, and both are normal in a real
company.

> A tool-selection benchmark whose prompts name their own domain cannot detect
> tool-selection failure. Mine was measuring keyword matching and reporting it as tool
> selection. Worth checking in your own evals.

```bash
make sweep-hard     # vague + near-miss, the degradation above
```

### 4. Under realistic conditions, curation beats protocol

Vague requests, 54 near-miss tools, enterprise-weight payloads:

| arm | n | success | calls/task | wrong-tool calls | $/task |
|---|---:|---:|---:|---:|---:|
| `direct` | 20 | 55% | 23.8 | 377 | $0.0134 |
| `mcp_sidecar` | 30 | 60% | 8.9 | 125 | $0.0065 |
| `mcp_filtered` | 10 | 80% | 2.0 | 0 | $0.0030 |

`direct` came out worst. Its terse hand-written descriptions are cheaper per token and
more *confusable* on a crowded surface, where the MCP server's verbosity gives the model
something to discriminate on. Part of that 2.1x premium buys legibility.

The filtered arm's 80% is exactly the vague baseline with no distractors, so filtering
removes the distractor problem rather than beating it; the remaining 20% is comprehension.

```bash
make headline       # costs ~$0.30 on deepseek-chat
```

### 5. The economics flip at about six integrations

Direct is `A x S` integrations, MCP is `A + S` plus a gateway.

| agents x systems | integrations | winner | annual delta |
|---|---:|---|---:|
| 2 x 2 | 4 vs 4 | direct | $2,951 |
| **2 x 3** | 6 vs 5 | **MCP** | $3,209 |
| 5 x 6 | 30 vs 11 | MCP | $93,929 |
| 8 x 12 | 96 vs 20 | MCP | $364,409 |

Symmetric, so one system needs about 8 agents before MCP pays, same as one agent needing 8
systems. At 5x6 the measured token term is 4.9% of the MCP bill, so the **assumed labour
rates decide this, not the benchmark**. Replace them with yours.

```bash
make crossover
python -m analysis.crossover --agents 4 --systems 9 --loaded-hourly-rate 95
```

---

## Reproducing

**Prerequisites.** Python 3.11+ (tested on 3.13), Docker Engine with Compose v2, GNU make,
curl. The `netem-sweep` target needs a kernel with `sch_netem` and grants the remote
container `NET_ADMIN`; it fails loudly rather than silently reporting zero delay.

```bash
python -m venv .venv
.venv/bin/activate                  # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt     # pinned to the versions that produced results-reference/
cp .env.example .env                # add DEEPSEEK_API_KEY for the model-in-loop tables

make stack-up
make reproduce                      # everything that costs nothing
```

`make reproduce` runs validate, the latency tables, the unpooled result, the netem sweep,
and an oracle pass over all arms. Compare its output against [`results-reference/`](results-reference/),
which holds the exact JSONL behind every published number, with the config fingerprint on
line 1 of each file.

The model-in-the-loop tables cost money: `make headline` is roughly $0.30 and
`make sweep-hard` roughly $0.40 on `deepseek-chat`.

**Windows gotcha.** PowerShell's `Out-File -Encoding utf8` writes a BOM, and
`python-dotenv` then reads your first key as `﻿DEEPSEEK_API_KEY` so it silently never
loads. Symptom is a 401 naming a key you never set. Use an editor, or
`[System.IO.File]::WriteAllText($p, $t, (New-Object System.Text.UTF8Encoding($false)))`.

## How it is built

Five arms all hit the **same** FastAPI process, with the MCP servers as thin pass-throughs
holding no logic of their own, so what is measured is the access layer and not two
implementations. Backend latency is injected deterministically and subtracted out via a
`X-Backend-Elapsed-Ms` header, leaving protocol overhead. Tasks are graded on backend
state, never on what the model claimed. State is reset before every trial.

```
bench/backend/     one instrumented FastAPI service, the control
bench/mcpserver/   thin MCP wrapper over it (stdio + streamable HTTP)
bench/arms/        direct, mcp_stdio, mcp_sidecar, mcp_remote, mcp_filtered
bench/providers/   deepseek, ollama, anthropic, plus an oracle for free self-tests
bench/workload/    5 graded tasks (explicit + vague phrasings), distractor generators
analysis/          crossover economics, sweep breakdown, spend
```

There is an `oracle` provider that solves every task by construction. It is not a model
and never a baseline; it exists so the harness can be tested end to end with no API key,
and so a failure can be blamed on the arm rather than the model.

## Limits

One model (`deepseek-chat`). Samples of 10 to 30 per cell, so success differences under
about 25 points are inside the Wilson interval; the wrong-tool differences are far larger
and safe to rely on. Five tasks, one synthetic domain, no pagination or partial failures.
Lab network, so the 2.0x netem multiplier should transfer but the absolute milliseconds
should not. Cost-model labour inputs are assumed, not measured. `called_tools` is
deduplicated per trial, so distractor *invocation* counts are bounded while wrong-tool
counts are exact.

Full treatment in [`METHODOLOGY.md`](METHODOLOGY.md), including the results that did not
reproduce and why.

## License

MIT.
