# mcp-vs-api-bench

A benchmark that answers, with numbers, whether an agent should use MCP or just call the
API directly, and if it uses MCP, where the server should sit. One backend, five
tool-access layers, 265 graded trials on a real model, under a dollar of spend. A further
170 trials drive a scripted stub instead of a model to check the harness itself; no
model-behaviour result below rests on those.

**Protocol choice is the second decision. Curating the tool list is the first.** MCP costs
2.2 to 4.1 ms per call and about twice the schema tokens. Both are small and both are
fixable. The thing that broke agents here was a large, overlapping tool surface, and it
hurt the direct integration worse than it hurt MCP.

![Decision flowchart. First gate, applying to both architectures: load only the tools the task needs. Then branch on agent times system count: under about six pairs use a direct API, six or more use MCP, and take a vendor-shipped MCP server at any count. Under MCP, co-locate as stdio or a sidecar when a task makes more than about ten calls over a WAN, otherwise a central gateway is fine.](docs/img/decision.svg)

Full writeup with the security threat model: [`docs/decision-brief.html`](docs/decision-brief.html).
Raw data behind every number: [`results-reference/`](results-reference/) and [`RESULTS.md`](RESULTS.md).

**On sample sizes, up front, because they constrain everything below.** Cells run 10 to 30
trials, and every cell is repeats over the same five tasks, so the independent evidence is
thinner than the trial counts suggest. Any success difference under roughly 25 points is
inside the noise here. The call-volume and wrong-tool columns separate much more cleanly,
and those are the ones the claims below lean on.

---

## Findings

### 1. Latency is a non-issue

No model in the loop, backend pinned at 40 ms, no injected network delay.

| arm | overhead p50 | over direct |
|---|---:|---:|
| `direct` | 1.15 ms | baseline |
| `mcp_stdio` | 3.35 ms | +2.20 |
| `mcp_sidecar` | 5.00 ms | +3.85 |
| `mcp_remote` | 5.28 ms | +4.13 |

Against a model turn of 800 to 5,000 ms this is noise.

Two numbers survive that. A streamable-HTTP tool call goes out and comes back, and both
legs cross the link, so a **remote gateway pays the one-way latency twice per call**, which
is one full round trip. Sweeping the injected one-way delay at 10, 25 and 50 ms gave
multipliers of 1.92, 2.01 and 2.00. A 30 ms one-way hop is 60 ms a call, invisible until
an agent makes forty of them.

The second: a client that re-handshakes per request measured **1015 ms per call against
3.35 ms pooled**, roughly 300x. That is a connection-pooling bug rather than a property of
MCP, and it is the first thing to check when someone reports that MCP was slow in
production.

```bash
make micro          # the table above
make micro-naive    # the ~300x unpooled result
make netem-sweep    # the round-trip multiplier
```

### 2. Tokens cost more than latency, and less than expected

Identical eleven capabilities over the identical backend:

| arm | tools | schema tokens | utilisation |
|---|---:|---:|---:|
| `direct` (hand-written) | 11 | 1,080 | 25.2% |
| `mcp_sidecar` (server-advertised) | 11 | 2,309 | 25.6% |
| `mcp_filtered` (per-task subset) | 1 to 4 | 1,246 | 87.8% |

2.1x, because a general-purpose server writes descriptions for a consumer it has never
met. Caching softens the rate and leaves the volume alone: DeepSeek's prefix cache hit 92
to 96% and cost still tripled as the tool list grew, since a hit rate cuts what you pay per
token while you still send six times as many. The 1,246 figure is the widest filtered
block, so it understates the saving on narrower tasks.

### 3. Tool-count degradation needs two conditions at once

Sweeping 0 to 150 irrelevant tools produced no degradation: 10/10 success, zero distractor
calls at every level. That held for same-domain near-miss tools (`list_incidents`,
`list_service_requests`) through 108 of them.

The prompts were doing the work. They named their own domain ("every support ticket that is
open and priority P1"), so keyword overlap was enough to route on and the model never had
to discriminate. Rewriting the same tasks the way a person actually asks, with identical
checkers:

| request | tools loaded | n | success | wrong-tool calls per task |
|---|---|---:|---:|---:|
| explicit | none | 40 | 100% | 0.2 |
| explicit | 54 near-miss | 20 | 100% | 0 |
| explicit | 108 near-miss | 10 | 100% | 0.1 |
| vague | none | 10 | 80% | 0.1 |
| vague | 54 cross-domain | 10 | 60% | 0.3 |
| vague | 54 near-miss | 30 | 60% | 4.2 |

![Two by two grid of request phrasing against tool similarity. Explicit requests hold 100 percent success against both unrelated and lookalike tools. Vague requests drop to 60 percent in both columns, but only the vague plus lookalike cell produces a mass of wrong tool calls, 4.2 per task against 0.3.](docs/img/interaction.svg)

The two variables do different jobs. Vague phrasing is what costs the success, and it costs
the same 20 points whether the extra tools are unrelated or lookalikes. Lookalikes are what
convert that into wrong calls, 4.2 per task against 0.3, a 14x separation that clears the
noise floor comfortably. The success drop sits at 20 points, under the floor, so it rests
on being consistent across cells rather than on any single cell.

Both conditions are normal in a real company.

The vague/cross-domain row is worth one note: those 0.3 calls per task were off-task calls
into the *relevant* tool set, and none of the 54 loaded distractors was ever invoked.

> A tool-selection benchmark whose prompts name their own domain measures keyword matching.
> Worth checking against your own evals.

```bash
make sweep-hard     # vague + near-miss, the degradation above
```

### 4. Under realistic conditions, curation beats protocol

Vague requests, 54 near-miss tools, enterprise-weight payloads:

| arm | n | success | calls/task | wrong-tool calls/task | $/task |
|---|---:|---:|---:|---:|---:|
| `direct` | 20 | 55% | 23.8 | 18.9 | $0.0134 |
| `mcp_sidecar` | 30 | 60% | 8.9 | 4.2 | $0.0065 |
| `mcp_filtered` | 10 | 80% | 3.1 | 0 | $0.0030 |

`direct` came out worst. The 55 against 60 is inside the noise and carries nothing; the
weight is on 23.8 calls per task against 8.9, and 18.9 wrong-tool calls against 4.2, which
clear the floor by a wide margin. My reading is that terse hand-written descriptions are
cheaper per token and more confusable on a crowded surface, where the MCP server's
verbosity gives the model something to discriminate on. That is interpretation rather than
measurement, since the benchmark varied the description style and the protocol together.
Part of the 2.1x token premium plausibly buys legibility.

The filtered arm's 80% is exactly the vague baseline with no distractors, so filtering
removes the distractor problem and leaves comprehension where it was. The remaining 20% is
the phrasing, and no protocol choice touches it.

Cost per task falls by half against the sidecar and 4x against direct. Call volume dropping
from 8.9 to 3.1 drives most of that, with the smaller schema block second.

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

Symmetric, so one system needs about 8 agents before MCP pays, the same as one agent
needing 8 systems. This is arithmetic over assumed labour rates. The benchmark never varied
agent or system count, and at 5x6 the measured token term is 4.9% of the MCP bill, so
**the labour rates decide this outcome**. Replace them with yours.

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
`python-dotenv` then reads your first key as `﻿DEEPSEEK_API_KEY`, so it silently never
loads. The symptom is a 401 naming a key you never set. Use an editor, or
`[System.IO.File]::WriteAllText($p, $t, (New-Object System.Text.UTF8Encoding($false)))`.

## How it is built

Five arms all hit the **same** FastAPI process, with the MCP servers as thin pass-throughs
holding no logic of their own, so what gets measured is the access layer rather than two
implementations. Backend latency is injected deterministically and subtracted out via an
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

The `oracle` provider solves every task by construction. It is a scripted stub rather than
a model and never a baseline; it exists so the harness can be tested end to end with no API
key, and so a failure can be blamed on the arm rather than on the model.

## Limits

One model (`deepseek-chat`). Ten to 30 trials per cell over five tasks, so success
differences under about 25 points are inside the Wilson interval, as flagged at the top.
One synthetic domain, no pagination or partial failures. Lab network, so the round-trip
multiplier should transfer while the absolute milliseconds should not. Cost-model labour
inputs are assumed. Description style and protocol vary together across arms, so finding 4
cannot separate them. `called_tools` is deduplicated per trial, so distractor *invocation*
counts are bounded while wrong-tool counts are exact.

Full treatment in [`METHODOLOGY.md`](METHODOLOGY.md), including which results reproduced
under which conditions.

## License

MIT.
