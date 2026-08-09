.PHONY: up down verify logs
# Optional real-remote arm. Ephemeral: build, run, capture, tear down. Nothing stays.
# Real host values live in ../unraid.env, which is gitignored and never referenced by value.

LAB := mcpbench

up:
	@set -a; . ../unraid.env; set +a; \
	ssh "$$UNRAID_SSH" "mkdir -p $$APPDATA/labs/$(LAB)"; \
	tar czf - -C .. bench requirements.txt | ssh "$$UNRAID_SSH" "tar xzf - -C $$APPDATA/labs/$(LAB)"; \
	scp docker-compose.yml Dockerfile "$$UNRAID_SSH:$$APPDATA/labs/$(LAB)/"; \
	ssh "$$UNRAID_SSH" "cd $$APPDATA/labs/$(LAB) && docker compose up -d --build"; \
	ssh "$$UNRAID_SSH" "docker ps --filter label=com.jlu.lab=$(LAB)"; \
	echo ""; \
	echo "Point the harness at it:"; \
	echo "  MCP_REMOTE_URL=http://\$$UNRAID_HOST:9114/mcp python -m bench.cli micro --arms mcp_remote"

logs:
	@set -a; . ../unraid.env; set +a; \
	ssh "$$UNRAID_SSH" "cd $$APPDATA/labs/$(LAB) && docker compose logs --tail 100"

down:
	@set -a; . ../unraid.env; set +a; \
	ssh "$$UNRAID_SSH" " \
	  cd $$APPDATA/labs/$(LAB) 2>/dev/null && \
	    docker compose down -v --remove-orphans --rmi local; \
	  docker ps -aq        --filter label=com.jlu.lab=$(LAB) | xargs -r docker rm -f; \
	  docker network ls -q --filter label=com.jlu.lab=$(LAB) | xargs -r docker network rm; \
	  docker volume ls -q  --filter label=com.jlu.lab=$(LAB) | xargs -r docker volume rm; \
	  rm -rf $$APPDATA/labs/$(LAB) \
	"
	@$(MAKE) verify

verify:
	@set -a; . ../unraid.env; set +a; \
	ssh "$$UNRAID_SSH" " \
	  echo '== containers =='; docker ps -a --filter label=com.jlu.lab=$(LAB); \
	  echo '== networks ==';   docker network ls --filter label=com.jlu.lab=$(LAB); \
	  echo '== volumes ==';    docker volume ls --filter label=com.jlu.lab=$(LAB); \
	  echo '== images ==';     docker images --filter label=com.jlu.lab=$(LAB); \
	  echo '== dir ==';        ls -d $$APPDATA/labs/$(LAB) 2>/dev/null || echo gone; \
	  echo '== lab ports =='; ss -ltn | grep -E ':91[0-9][0-9]' || echo 'no lab ports listening' \
	"
