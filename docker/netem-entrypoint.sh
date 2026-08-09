#!/bin/sh
# Inject a one-way egress delay so the `mcp_remote` arm crosses a real network hop,
# WITHOUT also delaying the gateway's own upstream call to the backend.
#
# Why the filter matters. A naive `tc qdisc add dev eth0 root netem delay 25ms` delays
# every packet leaving this container, which includes both the response to the client and
# the request to the backend. A streamable-HTTP tool call traverses that egress path
# several times, so a 25ms setting shows up as ~82ms of measured overhead. That is a real
# topology (a central gateway far from both the agent and the backend), but it is not the
# thing the arm claims to measure, and reporting it as "25ms of network" would be wrong.
#
# So: two priority bands. Backend-destined traffic goes to an undelayed band; everything
# else (the client) goes through netem. The delay then models exactly one thing, the
# distance between the agent and the gateway.
#
# Set NETEM_DELAY=0 to disable, which is the control for the sweep itself.
# Set NETEM_DELAY_ALL=1 to get the old delay-everything behaviour deliberately, when you
# want to model a gateway that is far from the backend too.

set -e

DELAY="${NETEM_DELAY:-0}"
IFACE="${NETEM_IFACE:-eth0}"

if [ "$DELAY" = "0" ] || [ -z "$DELAY" ]; then
  echo "netem: no delay configured (NETEM_DELAY=${DELAY})"
  exec "$@"
fi

# Resolve the backend so it can be exempted from the delay.
BACKEND_HOST=$(echo "${BACKEND_URL:-http://backend:9110}" | sed -e 's|^.*://||' -e 's|[:/].*$||')
BACKEND_IP=$(getent hosts "$BACKEND_HOST" 2>/dev/null | awk '{print $1}' | head -n1)

if [ "${NETEM_DELAY_ALL:-0}" = "1" ] || [ -z "$BACKEND_IP" ]; then
  if [ -z "$BACKEND_IP" ] && [ "${NETEM_DELAY_ALL:-0}" != "1" ]; then
    echo "netem: WARNING could not resolve backend host '${BACKEND_HOST}'." >&2
    echo "netem: falling back to delaying ALL egress, so the gateway's upstream call is" >&2
    echo "netem: delayed too and measured overhead will be a MULTIPLE of ${DELAY}." >&2
  fi
  if tc qdisc add dev "$IFACE" root netem delay $DELAY 2>/dev/null; then
    echo "netem: applied ${DELAY} to ALL egress on ${IFACE} (backend not exempt)"
  else
    echo "netem: FAILED to apply ${DELAY}. Is NET_ADMIN granted? Results from this" >&2
    echo "netem: container show NO delay and must not be reported as remote." >&2
    exit 1
  fi
  exec "$@"
fi

# Band 1:1 = undelayed (backend). Band 1:3 = netem (everything else, i.e. the client).
if ! tc qdisc add dev "$IFACE" root handle 1: prio bands 3 2>/dev/null; then
  echo "netem: FAILED to create the prio qdisc. Is NET_ADMIN granted? Results from this" >&2
  echo "netem: container show NO delay and must not be reported as remote." >&2
  exit 1
fi
tc qdisc add dev "$IFACE" parent 1:3 handle 30: netem delay $DELAY
tc filter add dev "$IFACE" protocol ip parent 1:0 prio 1 u32 \
  match ip dst "${BACKEND_IP}/32" flowid 1:1
tc filter add dev "$IFACE" protocol ip parent 1:0 prio 2 u32 \
  match ip dst 0.0.0.0/0 flowid 1:3

echo "netem: applied ${DELAY} egress delay on ${IFACE}, exempting backend ${BACKEND_HOST} (${BACKEND_IP})"
echo "netem: the delay now models agent-to-gateway distance only"

exec "$@"
