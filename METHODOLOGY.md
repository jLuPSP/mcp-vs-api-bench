# Methodology

This document exists so a skeptical architect can decide how much to trust the numbers
before reading them. It states what is held constant, what is varied, how each metric is
computed, and where this design is weak.

---

## 1. What is held constant

The single most important property of this benchmark: **every arm talks to the same
backend process.**

- One FastAPI service (`bench/backend/app.py`), one dataset, one seed (`BENCH_SEED`).
- Backend latency is injected deterministically (`BACKEND_LATENCY_MS`, default 40 ms,
  jitter 0 by default). This is the control term. It appears identically in every arm and
  is subtracted out to isolate protocol overhead.
- The MCP servers are **thin wrappers** over that same HTTP backend. They contain no
  business logic, no caching, and no batching. If they did, the benchmark would be
  comparing two implementations rather than two access layers.
- Backend state is reset via `POST /_bench/reset` before every trial, so a mutating task
  in trial N cannot affect trial N+1.
- Model, temperature, seed, system prompt, and task text are identical across arms within
  a run.

## 2. What is varied

Exactly one thing per comparison:

| Comparison | Varied | Held constant |
|---|---|---|
| `direct` vs `mcp_*` | Tool access layer | Backend, model, tasks, tool *semantics* |
| `mcp_stdio` vs `mcp_sidecar` vs `mcp_remote` | Transport and network distance | Everything else, including the MCP server code |
| Distractor sweep | Number of irrelevant tools loaded | Arm, task, model |
| Cache experiment | `cache_control` on the tool block | Arm, task, model, tool set |
| Session reuse | Connection pooling on/off | Arm, task |
| Netem sweep | Injected one-way delay | Arm, task |

A caveat that cuts against the cleanliness above: `direct` and `mcp_*` do **not** have
byte-identical tool schemas. The direct arm uses hand-authored descriptions, because that
is what a team actually writes; the MCP arm uses whatever the server advertises. That
difference is part of what is being measured, not a confound, but it does mean the
comparison is "hand-tuned tools vs server-advertised tools", not "same tools, different
transport". `mcp_filtered` narrows this gap deliberately.

## 3. Run modes

### Microbench (no model)

Purpose: settle the latency claim with tight confidence intervals.

- N iterations (default 2000) of a single fixed tool call per arm.
- Warmup iterations (default 100) are discarded.
- Per-call decomposition:
  - `t_total` = wall clock around `call_tool`
  - `t_upstream` = backend's self-reported processing time, returned on the
    `X-Backend-Elapsed-Ms` header and propagated through the MCP layer
  - `t_overhead` = `t_total - t_upstream` (framing + serialization + transport)
- `t_session` is measured separately: connect, `initialize`, `notifications/initialized`,
  `tools/list`. Reported both as a one-time cost and amortized per call at several
  request volumes, because whether it matters depends entirely on connection pooling.
- Reported as p50 / p95 / p99 with a bootstrap 95% CI (10,000 resamples).

### Agentic (model in loop)

Purpose: measure what actually reaches a user.

- Each task runs `--repeats` times per arm (default 5).
- Temperature 0 where the provider supports it. **This does not make runs deterministic**
  and the harness does not claim it does; it reduces variance. Success rates are reported
  with Wilson score intervals so small-N differences are not over-read.
- A turn cap (`--max-turns`, default 12) prevents runaway loops. Hitting the cap is
  recorded as a distinct failure mode, not silently as a failure.
- Tool call errors are returned to the model as tool results (with an error flag) rather
  than aborting, because that is what a real harness does.

## 4. Metric definitions

**Tokens to completion.** Sum of prompt + completion tokens across every turn of a task,
including retries. Prompt tokens are counted per turn, not once, because the tool block is
re-sent on every turn unless cached.

**Tool schema tokens.** Measured, not estimated. For Anthropic, via
`client.messages.count_tokens` on the exact tool block. For OpenAI-compatible providers,
via the provider's reported prompt tokens on a fixed-content probe request with and
without the tool block, differenced. Never via `tiktoken`, which is the wrong tokenizer
for non-OpenAI models and is off by 15 to 20% on typical text.

**Tool schema utilization.** `tokens(tools actually called during the task) / tokens(all
tools loaded)`. The complement is the fraction of context spent describing tools that were
never used. This is the metric that makes gateway bloat legible to a finance-minded
audience.

**Wrong-tool rate.** Fraction of tool calls naming a tool outside the task's declared
relevant set. Requires each task to declare its relevant tools, which the suite does.

**Cost per task.** `sum(tokens x per-token rate)` from `bench/pricing.yaml`, with cached
and uncached input tokens billed at their separate rates. Cache writes and cache reads are
tracked separately because their rates differ (on Anthropic, roughly 1.25x base for a
5-minute-TTL write and 0.1x for a read).

**Blast radius.** Not a runtime metric. Counted by hand and recorded in
`analysis/crossover.py` inputs: how many files must change to add audit logging or PII
redaction across all tool calls, per architecture.

## 5. Statistical treatment

- Latency: p50 / p95 / p99, bootstrap 95% CI. Never mean alone; the distributions are
  right-skewed and a mean hides the tail that actually pages people.
- Success rate: Wilson score interval. With 5 repeats over 5 tasks (n=25), a 4 percentage
  point difference is not a finding. The report suppresses claims below the interval width.
- No p-values. The comparisons here are not hypothesis tests, and dressing them as such
  would overstate the rigor.

## 6. Threats to validity

Listed in rough order of how much they should worry you.

1. **Client implementation quality dominates.** MCP overhead is largely a property of the
   client library, not the protocol. A different client, or one with connection pooling
   configured differently, moves these numbers substantially. Treat the absolute
   milliseconds as characterizing this stack, and the *shape* of the comparison as the
   transferable result.

2. **Tool-selection results are model-specific.** The distractor sweep measures how one
   model degrades with tool count. Another model in the same family may differ, and
   another family almost certainly will. Rerun the sweep on the model you deploy. Do not
   quote this repo's distractor curve as a property of "LLMs".

3. **The lab network is not your network.** A local container-to-container hop has no
   mTLS, no API gateway, no service mesh sidecar, no cross-AZ traversal. The netem sweep
   exists precisely so you can substitute a measured RTT from your own environment rather
   than trusting the lab default.

   Two details about how the delay is injected, because they change what the number means:

   - The delay is applied to the remote gateway's **egress, with backend-destined traffic
     exempted** via a `tc prio` band (see `docker/netem-entrypoint.sh`). Without that
     exemption the gateway's own upstream call is delayed too, and a 25 ms setting shows
     up as roughly 82 ms rather than 57 ms. That is a legitimate topology (a central
     gateway far from both the agent and the backend) and you can select it deliberately
     with `NETEM_DELAY_ALL=1`, but it is not what "25 ms of network" implies, so it is not
     the default.
   - With the exemption in place the measured overhead scales at a flat **2.0x** the
     injected one-way delay, because a streamable-HTTP `tools/call` makes two
     client-facing trips. Verified at 10 / 25 / 50 ms (1.92x / 2.01x / 2.00x). This
     multiplier is a property of the transport and should transfer; the absolute
     milliseconds are a property of the lab and should not.

4. **Task suite size.** Five tasks is enough to detect a large effect and nowhere near
   enough to characterize a workload. It is a starting point you are expected to extend
   with tasks that look like your traffic.

5. **Cache economics depend on traffic shape, which is not simulated.** The cache
   experiment measures the per-request saving on a warm cache. It does **not** model
   whether your traffic keeps a cache warm. Bursty hourly traffic against a 5-minute TTL
   never hits, and no measurement here will tell you that.

6. **The direct arm is hand-tuned by construction.** Someone wrote those descriptions
   carefully. In a real N-agent organization, some of those hand-written integrations will
   be sloppy, and the direct arm's advantage will erode in ways this benchmark cannot see.
   The crossover model is the place that penalty gets accounted for, via the
   per-integration cost input.

7. **Single backend domain.** A synthetic ops API exercises CRUD and simple multi-hop
   reasoning. It does not exercise streaming, large binary payloads, long-running jobs, or
   pagination-heavy retrieval, all of which have different protocol overhead profiles.

8. **Distractor-invocation counts are bounded, not exact.** The harness stores
   `called_tools` deduplicated per trial (`bench/harness/agentic.py`), so a distractor
   called five times within one trial counts once. Any figure phrased as "distractor
   calls" is therefore a lower bound on invocations and an exact count of *distinct
   distractor tools touched*. The `wrong_tool_calls` counter is incremented per call and
   is exact; prefer it when the magnitude matters. This affects presentation only, not any
   pass/fail grade.

## 6a. What reproduced, what did not, and under which conditions

Recorded in the order it was discovered, including the part that was wrong, because a
benchmark that only documents its confirmations is a marketing asset rather than a
measurement.

**The headline: tool-count degradation reproduces, but requires two conditions
simultaneously.** Vague request phrasing and semantically overlapping tools. Either alone
produces nothing. That interaction is the finding; everything below is how it was reached.

**Under explicit phrasing, "more tools degrade tool selection" did not reproduce.**
Sweeping 0 to 150 injected distractor tools against `deepseek-chat` produced **zero**
distractor invocations at every level and no change in success rate (10/10 throughout).
Cost rose 3.2x; correctness did not move, *under explicit phrasing*. The section below
shows correctness does move once the prompts stop naming their own domain.

Three readings were possible:

1. **The claim is weaker than commonly assumed**, at least for tool counts in the low
   hundreds and a model with competent function-calling.
2. **The distractors are too easy.** Drawn from clearly distinct domains (HR, finance,
   calendar, docs, marketing, infra, security), nothing in that set is semantically
   adjacent to `list_tickets` or `adjust_inventory`.
3. **The tasks are too well specified.** Each prompt names its domain unambiguously.

Reading (2) was testable, so it was tested. The `near_miss` distractor set
(`--kind near_miss`) is same-domain and semantically overlapping: `list_incidents`,
`list_service_requests`, `escalate_case`, `update_stock_level`, `list_parts`,
`adjust_part_count`, `get_rota`. At 0 / 18 / 54 / 108 near-miss distractors the result was
**again zero distractor invocations and 10/10 success at every level**, with cost rising
2.6x. So (2) is ruled out as a *sufficient* cause on its own. It later turns out to be
necessary but not sufficient, which is exactly the shape of an interaction.

Reading (3) was then tested too, via `--phrasing vague`, and **it is one necessary half of
the explanation.** Near-miss similarity is the other half. With both present the wrong-tool
rate rises from 3.3% to 60.7%, and at least 60 distinct distractor tools get invoked. That
distinct-tool count is a lower bound, not an invocation count: the harness stores
`called_tools` deduplicated per trial, so the exact per-call figure is the wrong-tool
counter, which reads 125 calls at 54 near-miss distractors.

Holding distractors at 54 and varying only the two factors isolates it:

| Phrasing | Distractors | n | Success | Wrong-tool |
|---|---|---:|---:|---:|
| explicit | none | 10 | 100% | 0.0% |
| explicit | near-miss | 10 | 100% | 0.0% |
| vague | none | 10 | 80% | 3.3% |
| vague | cross-domain | 10 | 60% | 6.7% |
| vague | near-miss | 30 | 60% | 46.8% |

Note the differing sample sizes: the bottom cell pools three runs (n=30) while the others
are single runs (n=10), so their Wilson intervals are not comparable.

**The effect is an interaction and neither factor alone produces it.** Similar tools with an
explicit prompt are harmless; a vague prompt with unrelated tools degrades success without
degrading tool choice. Both must be present.

The `vague / none` row is the one to keep in mind when reading the filtered arm elsewhere:
80% is the ceiling that request ambiguity alone imposes. Filtering the tool list returns
you to that ceiling, it does not exceed it.

The methodological lesson generalises beyond this repo: **a tool-selection benchmark whose
prompts name their own domain cannot detect tool-selection failure**, because keyword
overlap does the routing before the model has to discriminate. The first two sweeps here
were measuring keyword matching and reporting it as tool selection. Any benchmark claiming
"tool count does not matter" should be checked for this before it is believed, including
the earlier version of this one.

**"Caching solves the token problem" only half reproduced.** DeepSeek's automatic prefix
cache hit 92 to 96% across the sweep, and per-task cost still tripled. A high hit rate
reduces the marginal rate; it does not reduce the volume. Both facts are needed to reason
about this and quoting either alone is misleading.

## 7. Things that would change the conclusions

Stated up front, so they read as anticipated rather than as excuses later:

- **API-native tool search** (deferred tool loading, where the model searches a large tool
  catalog instead of having every schema in context) largely dissolves the distractor
  problem for the direct arm. If your provider supports it, the "MCP gateways bloat
  context" objection is answerable without abandoning MCP; you just need the gateway to
  expose tools as searchable rather than eagerly loaded.
- **Mid-conversation tool changes** (adding or removing tools between turns without
  invalidating the prompt cache) removes the "you cannot change tools without paying for
  the whole prefix again" penalty, which is one of the stronger structural arguments for
  a dynamic MCP layer over a static tool list.

Both are provider features, not MCP-vs-API features, and both shift the answer. The repo
notes where they apply rather than pretending the comparison is static.

## 8. Reproducing

```bash
make stack-up
make micro        # ~2 minutes, no cost
make agentic      # cost depends on provider and repeats
make report
```

Every result file records the git SHA, the resolved config, the model ID, the pricing
table version, and the seed. A result without those is not reproducible and the harness
refuses to write one.
