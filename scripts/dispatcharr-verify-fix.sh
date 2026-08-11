#!/usr/bin/env bash
# Verify the 2026-08-10 gluetun buffering fix (e2bd1cc) is actually live and holding.
#
# Read-only. Makes no changes to the cluster.
#
# The fix set HEALTH_RESTART_VPN=off on the gluetun sidecar. There are five
# independent ways that can be true in git and false in the cluster, and each
# one is silent. This checks all five, then measures the outcome.
#
# Usage: ./dispatcharr-verify-fix.sh [hours-of-logs]   (default 24)

set -uo pipefail

NS=dispatcharr
HOURS="${1:-24}"
FIX_COMMIT_UTC="2026-08-10T23:24:00Z"   # merge of PR #65 to main

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
info() { printf '        %s\n' "$1"; }
hr()   { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

WEB=$(kubectl -n $NS get pod -l app=dispatcharr,component=web \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -z "$WEB" ]]; then
  echo "FATAL: no dispatcharr web pod found in namespace $NS"
  exit 1
fi
NODE=$(kubectl -n $NS get pod "$WEB" -o jsonpath='{.spec.nodeName}')
echo "web pod : $WEB"
echo "node    : $NODE"
echo "window  : last ${HOURS}h"

# ---------------------------------------------------------------------------
hr "1. ArgoCD is still driving this app"
# The buffering runbook (docs/dispatcharr-buffering-runbook.md section 0) tells
# you to DISABLE auto-sync before A/B testing. If that was never re-enabled,
# every commit since is sitting in git and nothing has been applied.
SYNCPOL=$(kubectl -n argocd get application dispatcharr -o jsonpath='{.spec.syncPolicy.automated}' 2>/dev/null)
SYNCSTAT=$(kubectl -n argocd get application dispatcharr -o jsonpath='{.status.sync.status}' 2>/dev/null)
SYNCREV=$(kubectl -n argocd get application dispatcharr -o jsonpath='{.status.sync.revision}' 2>/dev/null)
info "syncPolicy.automated : ${SYNCPOL:-<none>}"
info "sync status          : ${SYNCSTAT:-<unknown>}  revision ${SYNCREV:0:8}"
[[ -n "$SYNCPOL" ]] && pass "auto-sync is enabled" \
  || fail "auto-sync is OFF — re-enable it, the runbook told you to turn it off for testing"
[[ "$SYNCSTAT" == "Synced" ]] && pass "app reports Synced" \
  || fail "app is $SYNCSTAT — the manifest in git is not what is running"

# ---------------------------------------------------------------------------
hr "2. HEALTH_RESTART_VPN reached the live Deployment and the live Pod"
DEPLOY_VAL=$(kubectl -n $NS get deploy dispatcharr-web -o jsonpath='{range .spec.template.spec.initContainers[?(@.name=="gluetun")].env[?(@.name=="HEALTH_RESTART_VPN")]}{.value}{end}' 2>/dev/null)
POD_VAL=$(kubectl -n $NS get pod "$WEB" -o jsonpath='{range .spec.initContainers[?(@.name=="gluetun")].env[?(@.name=="HEALTH_RESTART_VPN")]}{.value}{end}' 2>/dev/null)
[[ "$DEPLOY_VAL" == "off" ]] && pass "Deployment spec has HEALTH_RESTART_VPN=off" \
  || fail "Deployment spec value is '${DEPLOY_VAL:-<unset>}' — ArgoCD has not applied the fix"
[[ "$POD_VAL" == "off" ]] && pass "running Pod has HEALTH_RESTART_VPN=off" \
  || fail "running Pod value is '${POD_VAL:-<unset>}' — pod predates the fix, it needs a restart"

# A pod older than the fix cannot have it, whatever the Deployment says.
POD_START=$(kubectl -n $NS get pod "$WEB" -o jsonpath='{.metadata.creationTimestamp}')
info "pod created : $POD_START   (fix merged $FIX_COMMIT_UTC)"
if [[ "$POD_START" > "$FIX_COMMIT_UTC" ]]; then
  pass "pod was created after the fix landed"
else
  fail "pod predates the fix — it is still running the old settings"
fi

# ---------------------------------------------------------------------------
hr "3. The running gluetun image actually SUPPORTS the option"
# HEALTH_RESTART_VPN is newer than the newest semver tag (v3.41.x). It exists
# only in :latest / master builds. An image older than the feature will ignore
# the variable entirely and keep restarting the tunnel — which looks exactly
# like the fix not working.
kubectl -n $NS get pod "$WEB" \
  -o jsonpath='{range .status.initContainerStatuses[?(@.name=="gluetun")]}image:   {.image}{"\n"}imageID: {.imageID}{"\n"}{end}' 2>/dev/null
echo
echo "-- gluetun startup settings summary (Health section is what matters) --"
kubectl -n $NS logs "$WEB" -c gluetun --timestamps 2>/dev/null \
  | grep -iA4 'health' | grep -iE 'health|restart|vpn' | head -30
echo
echo "-- unknown / deprecated variable warnings (empty is good) --"
UNKNOWN=$(kubectl -n $NS logs "$WEB" -c gluetun 2>/dev/null \
  | grep -iE 'unknown environment variable|is unknown|deprecated' | head -20)
if [[ -z "$UNKNOWN" ]]; then
  pass "gluetun logged no unknown-variable warnings"
else
  echo "$UNKNOWN"
  fail "gluetun does not recognise one or more variables — check for HEALTH_RESTART_VPN above"
fi

# ---------------------------------------------------------------------------
hr "4. OUTCOME: has the tunnel restarted since the pod started?"
# This is the number that decides whether the fix worked. Before the fix this
# was 20 in a day. It should now be exactly 1 — the initial connect.
RESTARTS=$(kubectl -n $NS logs "$WEB" -c gluetun --since="${HOURS}h" --timestamps 2>/dev/null \
  | grep -cE '\[vpn\] (starting|stopping)')
STARTS=$(kubectl -n $NS logs "$WEB" -c gluetun --since="${HOURS}h" --timestamps 2>/dev/null \
  | grep -cE '\[vpn\] starting')
info "'[vpn] starting' lines in ${HOURS}h : $STARTS"
info "'[vpn] start/stop' lines in ${HOURS}h: $RESTARTS"
if [[ "$STARTS" -le 1 ]]; then
  pass "tunnel has not cycled — the fix is holding"
else
  fail "tunnel started $STARTS times — it is STILL cycling, the fix is not effective"
fi
echo
echo "-- timestamps of every tunnel start/stop, and any explicit restart reason --"
kubectl -n $NS logs "$WEB" -c gluetun --since="${HOURS}h" --timestamps 2>/dev/null \
  | grep -E '\[vpn\] (starting|stopping)|restarting VPN|healthy!|is unhealthy' \
  | awk '{print $1, substr($0, index($0,$2))}'

# ---------------------------------------------------------------------------
hr "5. How starved is the tunnel? (checks still run with the fix on)"
# With HEALTH_RESTART_VPN=off these still fire and still log; they just no
# longer tear the tunnel down. The count measures how congested the tunnel is.
TIMEOUTS=$(kubectl -n $NS logs "$WEB" -c gluetun --since="${HOURS}h" 2>/dev/null \
  | grep -ciE 'timed out|i/o timeout|context deadline exceeded')
info "health-check timeouts in ${HOURS}h : $TIMEOUTS"
if   [[ "$TIMEOUTS" -eq 0   ]]; then pass "no timeouts — tunnel is not congested"
elif [[ "$TIMEOUTS" -lt 50  ]]; then warn "some timeouts — check was oversensitive, tunnel mostly fine"
else fail "$TIMEOUTS timeouts — tunnel is genuinely congested; restarts were a symptom, not the cause"
fi
echo
echo "-- hourly histogram of health-check timeouts --"
kubectl -n $NS logs "$WEB" -c gluetun --since="${HOURS}h" --timestamps 2>/dev/null \
  | grep -iE 'timed out|i/o timeout' | cut -c1-13 | sort | uniq -c

# ---------------------------------------------------------------------------
hr "6. Are the Loki alerts for this actually loaded?"
# manifests/dispatcharr/gluetun-loki-rules.yaml creates ConfigMap
# loki-gluetun-rules, but values/loki.yaml only mounts loki-vpn-rules into
# /rules/fake. Nothing watches the loki_rule label. If this section fails, the
# alerts added alongside the fix have never been able to fire.
kubectl -n monitoring get configmap loki-gluetun-rules >/dev/null 2>&1 \
  && pass "ConfigMap loki-gluetun-rules exists" \
  || fail "ConfigMap loki-gluetun-rules missing — manifests app has not synced"

LOKI=$(kubectl -n monitoring get pod -l app.kubernetes.io/name=loki \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -n "$LOKI" ]]; then
  info "loki pod: $LOKI"
  echo "-- rule files the ruler can actually see --"
  kubectl -n monitoring exec "$LOKI" -c loki -- sh -c 'ls -R /rules 2>/dev/null' 2>/dev/null
  echo "-- rule groups the ruler has LOADED --"
  LOADED=$(kubectl -n monitoring exec "$LOKI" -c loki -- \
    wget -qO- http://127.0.0.1:3100/loki/api/v1/rules 2>/dev/null)
  echo "${LOADED:-<ruler returned nothing>}"
  if grep -q 'dispatcharr-gluetun' <<<"${LOADED:-}"; then
    pass "gluetun alert group is loaded in the ruler"
  else
    fail "gluetun alert group is NOT loaded — values/loki.yaml never mounts loki-gluetun-rules"
  fi
else
  warn "loki pod not found; skipped ruler check"
fi

# ---------------------------------------------------------------------------
hr "7. Are gluetun's logs even reaching Loki?"
# gluetun is declared as an initContainer (native sidecar). Confirm Promtail is
# shipping it, otherwise every Loki-based alert on it is dead regardless of §6.
echo "Run this in Grafana Explore, or port-forward Loki and curl it:"
echo
echo "  kubectl -n monitoring port-forward svc/loki 3100:3100 &"
echo "  curl -sG http://127.0.0.1:3100/loki/api/v1/query_range \\"
echo "    --data-urlencode 'query={namespace=\"dispatcharr\",container=\"gluetun\"}' \\"
echo "    --data-urlencode \"start=\$(date -u -d '-${HOURS} hours' +%s)000000000\" \\"
echo "    --data-urlencode \"end=\$(date -u +%s)000000000\" \\"
echo "    --data-urlencode 'limit=5' | head -c 2000"
echo
echo "Empty result = Promtail is not scraping the sidecar; fix that before trusting any alert."

# ---------------------------------------------------------------------------
hr "8. If the tunnel is NOT cycling but viewers still buffer"
# Everything below is a different root cause. Collect it now so the next
# session starts with data instead of theories.
echo "-- MTU chain (runbook T1-A; the two prior commits disagree on these numbers) --"
kubectl -n $NS exec "$WEB" -c gluetun -- ip -o link show 2>/dev/null \
  | awk '{print $2, $NF==""?"":"", $0}' | grep -oE '^[0-9]+: [a-z0-9@]+|mtu [0-9]+' | paste - - 2>/dev/null \
  || kubectl -n $NS exec "$WEB" -c gluetun -- ip link show 2>/dev/null
info "eth0 mtu minus 60 must be >= tun0 mtu, or large TCP segments are dropped"

echo
echo "-- CPU throttling on the streaming pod (runbook T1-B) --"
kubectl -n $NS exec "$WEB" -c dispatcharr -- \
  sh -c 'cat /sys/fs/cgroup/cpu.stat 2>/dev/null || cat /sys/fs/cgroup/cpu/cpu.stat 2>/dev/null' 2>/dev/null
info "sample twice ~60s apart; a rising nr_throttled during playback is a real finding"

echo
echo "-- restarts across the namespace (Redis OOM is runbook T1-D) --"
kubectl -n $NS get pods -o wide
echo
kubectl -n $NS get events --sort-by=.lastTimestamp 2>/dev/null | tail -20

echo
echo "-- node CPU steal / saturation on the streaming node --"
kubectl top node "$NODE" 2>/dev/null || info "(metrics-server unavailable)"

hr "done"
