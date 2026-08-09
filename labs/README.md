# Optional real-remote arm

The local stack simulates the `mcp_remote` network hop with `tc netem`. That is
reproducible by anyone who clones this repo, which is the right default. It is also a
simulation, and a simulation you have never checked is a simulation you should not quote.

This directory deploys the same backend and MCP server to a second host so you can run
`mcp_remote` across a real network and compare the two. If the netem number and the real
number agree, the sweep is trustworthy and you can go back to using it. If they diverge,
you have learned something more interesting than the benchmark was designed to find.

## Prerequisites

A gitignored `unraid.env` at the repo root (copy `unraid.env.example`). Nothing in this
directory contains a hostname, an IP, or an appdata path; every value is read from there
at run time.

Before bringing it up, check the ports are free on the target host:

```bash
ssh "$UNRAID_SSH" "ss -ltn | grep -E ':911[34]' || echo free"
```

## Deploy, measure, tear down

```bash
make -C labs up

# Point the harness at the real remote instead of the netem container
MCP_REMOTE_URL=http://${UNRAID_HOST}:9114/mcp python -m bench.cli micro --arms mcp_remote

# Compare against the simulated hop
NETEM_DELAY=25ms docker compose -f docker/docker-compose.yml up -d --force-recreate mcp-remote
python -m bench.cli micro --arms mcp_remote

make -C labs down     # runs the verify block automatically
```

`make down` removes the compose project, then sweeps by label as a safety net in case the
compose file is gone, then deletes the lab directory, then verifies every category is
empty. Capture what you need locally first: teardown deletes everything on the box.

## Why the lab is ephemeral

This is a measurement rig, not a service. It exists for the length of one comparison and
then goes away. If you find yourself wanting it running permanently, that is a signal it
has become something else and belongs somewhere else.
