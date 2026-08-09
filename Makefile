.PHONY: help install stack-up stack-down stack-logs micro micro-naive agentic sweep sweep-hard headline cache report validate pricing crossover netem-sweep test reproduce export-results clean

PY       ?= python
COMPOSE  ?= docker compose -f docker/docker-compose.yml
PROVIDER ?= deepseek
REPEATS  ?= 2
ARM      ?= mcp_sidecar
KIND     ?= cross_domain      # cross_domain | near_miss
PHRASING ?= explicit          # explicit | vague
COUNTS   ?= 0,18,54,108
DELAYS   ?= 0 10ms 25ms 50ms  # must match the delays in the published netem table

help:
	@echo "SETUP"
	@echo "  install        install pinned python deps"
	@echo "  stack-up       backend + mcp sidecar + mcp remote (no injected delay)"
	@echo "  stack-down     tear the stack down"
	@echo "  validate       check the seed yields solvable, non-degenerate tasks"
	@echo "  test           harness self-test, no API key and no spend"
	@echo ""
	@echo "REPRODUCE THE PUBLISHED RESULTS"
	@echo "  reproduce      everything that costs nothing (micro + netem + oracle)"
	@echo "  micro          protocol overhead, no model"
	@echo "  micro-naive    same, re-handshaking per call (the ~300x result)"
	@echo "  netem-sweep    the 2.0x RTT multiplier, at $(DELAYS)"
	@echo "  headline       the three-arm realistic-conditions table (COSTS MONEY)"
	@echo "  sweep-hard     vague + near-miss degradation sweep (COSTS MONEY)"
	@echo ""
	@echo "ANALYSIS"
	@echo "  crossover      N agents x M systems economics"
	@echo "  report         render RESULTS.md from results/"
	@echo "  export-results copy results/ -> results-reference/ for publishing"

install:
	$(PY) -m pip install -r requirements.txt

stack-up:
	$(COMPOSE) up -d --build
	@echo "waiting for backend..."
	@sleep 5
	@curl -fsS http://127.0.0.1:9110/healthz && echo ""

stack-down:
	$(COMPOSE) down -v --remove-orphans

stack-logs:
	$(COMPOSE) logs -f

validate:
	$(PY) -m bench.cli validate

# End-to-end harness self-test. No API key, no spend. Fails loudly if any arm cannot
# complete a task a perfect agent should always solve, localising the fault to the arm.
test: validate
	$(PY) -m bench.cli micro --arms direct,mcp_stdio --iterations 50 --warmup 10
	$(PY) -m bench.cli agentic --provider oracle --arms direct,mcp_stdio --repeats 1

# Everything that costs nothing. Run this first to confirm your environment matches.
reproduce: validate micro micro-naive netem-sweep
	$(PY) -m bench.cli agentic --provider oracle --arms direct,mcp_stdio,mcp_sidecar,mcp_filtered --repeats 2
	@echo ""
	@echo "Free reproduction complete. Compare against results-reference/."
	@echo "For the model-in-the-loop tables: make headline  (requires DEEPSEEK_API_KEY)"

micro:
	$(PY) -m bench.cli micro --iterations 200 --warmup 25

micro-naive:
	$(PY) -m bench.cli micro --arms mcp_stdio --no-session-reuse --iterations 40 --warmup 5

# The 2.0x-of-RTT result. Recreates mcp-remote at each delay, 100 iterations each.
netem-sweep:
	@for d in $(DELAYS); do \
		echo "=== netem delay $$d ==="; \
		NETEM_DELAY=$$d $(COMPOSE) up -d --force-recreate mcp-remote >/dev/null 2>&1; \
		sleep 4; \
		$(PY) -m bench.cli micro --arms mcp_remote --iterations 100 --warmup 15; \
	done
	@NETEM_DELAY=0 $(COMPOSE) up -d --force-recreate mcp-remote >/dev/null 2>&1

# The headline three-arm table. Needs BENCH_ENTERPRISE=1 on the backend, so it restarts
# the stack. Costs roughly $0.30 on deepseek-chat.
headline:
	BENCH_ENTERPRISE=1 $(COMPOSE) up -d --force-recreate backend >/dev/null
	@sleep 4
	$(PY) -m bench.cli agentic --provider $(PROVIDER) \
		--arms direct,mcp_sidecar,mcp_filtered \
		--phrasing vague --kind near_miss --distractors 54 --repeats $(REPEATS)

# The degradation sweep. Vague phrasing + near-miss tools is the condition that
# reproduces tool-selection failure; either alone will not.
sweep-hard:
	$(PY) -m bench.cli sweep-distractors --provider $(PROVIDER) --arm $(ARM) \
		--kind near_miss --phrasing vague --counts $(COUNTS) --repeats $(REPEATS)

sweep:
	$(PY) -m bench.cli sweep-distractors --provider $(PROVIDER) --arm $(ARM) \
		--kind $(KIND) --phrasing $(PHRASING) --counts $(COUNTS) --repeats $(REPEATS)

agentic:
	$(PY) -m bench.cli agentic --provider $(PROVIDER) --repeats $(REPEATS) \
		--phrasing $(PHRASING) --kind $(KIND)

cache:
	$(PY) -m bench.cli cache-experiment --provider anthropic --arm $(ARM)

crossover:
	$(PY) -m analysis.crossover --from-results results-reference/agentic-20260808T054605Z.jsonl

pricing:
	$(PY) -m bench.cli check-pricing

report:
	$(PY) -m bench.cli report

# Regenerate the committed RESULTS.md from the pinned reference runs.
export-results:
	$(PY) -m scripts.export_reference_results
	$(PY) -m bench.cli report \
		--results-dir results-reference \
		--micro-file results-reference/micro-20260808T011122Z.jsonl \
		--agentic-file results-reference/agentic-20260808T054605Z.jsonl

clean:
	rm -rf results/*.jsonl
