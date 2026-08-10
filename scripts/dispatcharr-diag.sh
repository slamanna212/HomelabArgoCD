#!/usr/bin/env bash
# One-shot diagnostic snapshot for Dispatcharr buffering investigation.
# Read-only: makes no changes to the cluster except an ephemeral debug container
# for the MTU probes (which disappears with the pod).
#
# Usage: ./dispatcharr-diag.sh [output-dir]

set -uo pipefail

NS=dispatcharr
OUT="${1:-diag-$(date +%Y%m%d-%H%M%S)}"
NETSHOOT_IMAGE="${NETSHOOT_IMAGE:-nicolaka/netshoot}"

mkdir -p "$OUT"
exec > >(tee "$OUT/summary.txt") 2>&1

hr() { printf '\n=== %s ===\n' "$1"; }

hr "context"
date -Is
kubectl config current-context

WEB_POD=$(kubectl -n $NS get pod -l app=dispatcharr,component=web \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -z "$WEB_POD" ]]; then
  echo "FATAL: no dispatcharr web pod found in namespace $NS"
  exit 1
fi
NODE=$(kubectl -n $NS get pod "$WEB_POD" -o jsonpath='{.spec.nodeName}')
POD_IP=$(kubectl -n $NS get pod "$WEB_POD" -o jsonpath='{.status.podIP}')
echo "web pod : $WEB_POD"
echo "node    : $NODE"
echo "pod IP  : $POD_IP"

hr "ArgoCD sync policy (auto-sync MUST be off before A/B testing)"
kubectl -n argocd get application dispatcharr \
  -o jsonpath='{.spec.syncPolicy}{"\n"}' 2>/dev/null || echo "(argocd app not readable)"

hr "pods, restarts, ages, placement"
kubectl -n $NS get pods -o wide
echo
kubectl -n $NS get pods \
  -o custom-columns='POD:.metadata.name,CONTAINER:.status.containerStatuses[*].name,RESTARTS:.status.containerStatuses[*].restartCount'

hr "recent restart reasons (last terminated state)"
for p in $(kubectl -n $NS get pods -o jsonpath='{.items[*].metadata.name}'); do
  kubectl -n $NS get pod "$p" -o json \
    | jq -r --arg p "$p" '
        .status.containerStatuses[]?
        | select(.lastState.terminated != null)
        | "\($p)/\(.name): exit=\(.lastState.terminated.exitCode) reason=\(.lastState.terminated.reason) at=\(.lastState.terminated.finishedAt)"' \
    2>/dev/null
done
echo "(empty above = nothing has crashed recently; OOMKilled here is a smoking gun)"

hr "events in namespace (last 1h)"
kubectl -n $NS get events --sort-by=.lastTimestamp 2>/dev/null | tail -30

# ---------------------------------------------------------------- MTU / network
hr "MTU chain + PMTU probes (T1-A)"
echo "Launching ephemeral netshoot in the web pod's network namespace..."
DEBUG_OUT=$(kubectl -n $NS debug "$WEB_POD" --image="$NETSHOOT_IMAGE" \
  --target=dispatcharr -q --attach=true -- sh -c '
    echo "--- interfaces ---"
    ip -o link show | sed "s/\\\\/ /"
    echo
    echo "--- routes ---"
    ip route show
    echo
    echo "--- DF-set probe to 1.1.1.1 (finds real usable MTU) ---"
    for sz in 1472 1420 1400 1372 1340 1300 1272 1200; do
      if ping -M do -s $sz -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
        echo "  payload $sz OK  (=> path MTU >= $((sz+28)))"
        break
      else
        echo "  payload $sz FAIL"
      fi
    done
    echo
    echo "--- TCP retransmit / fragmentation counters ---"
    nstat -az 2>/dev/null | grep -Ei "retrans|reasm|fragfail|OutNoRoutes|UdpRcvbufErrors|UdpInErrors" || netstat -s
    echo
    echo "--- resolver seen by the app container (T2-E) ---"
    cat /etc/resolv.conf
  ' 2>&1)
echo "$DEBUG_OUT"
echo "$DEBUG_OUT" > "$OUT/mtu-and-net.txt"

cat <<'EOF'

INTERPRETATION (T1-A):
  eth0 MTU 1450 (Cilium VXLAN) with tun0 MTU 1400 means a full tunnel packet is
  1400+32+8+20 = 1460 bytes -> larger than eth0 can carry -> large TCP segments
  are dropped. If you see that pair, set WIREGUARD_MTU=1320 and retest.
  Also compare the largest successful DF probe against tun0's MTU: if it is much
  smaller, PMTU is black-holing somewhere past the Mullvad exit.
EOF

# ---------------------------------------------------------------- CPU throttling
hr "CPU throttling — web container (T1-B)"
kubectl -n $NS exec "$WEB_POD" -c dispatcharr -- cat /sys/fs/cgroup/cpu.stat 2>/dev/null \
  || echo "(cgroup v2 cpu.stat unavailable in this container)"
echo
echo "nr_throttled / throttled_usec climbing between two runs of this script = CPU limit is biting."

hr "CPU throttling — celery container (T1-C)"
CEL_POD=$(kubectl -n $NS get pod -l app=dispatcharr,component=celery \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -n "$CEL_POD" ]]; then
  echo "celery pod: $CEL_POD on node $(kubectl -n $NS get pod "$CEL_POD" -o jsonpath='{.spec.nodeName}')"
  kubectl -n $NS exec "$CEL_POD" -c celery -- cat /sys/fs/cgroup/cpu.stat 2>/dev/null
  echo "NOTE: celery is pinned by required podAffinity to the SAME node as web."
fi

hr "live resource usage"
kubectl -n $NS top pod --containers 2>/dev/null || echo "(metrics-server unavailable)"
echo
kubectl top node 2>/dev/null

# ---------------------------------------------------------------- redis
hr "Redis state (T1-D)"
RED_POD=$(kubectl -n $NS get pod -l app=dispatcharr,component=redis \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
if [[ -n "$RED_POD" ]]; then
  kubectl -n $NS exec "$RED_POD" -- redis-cli info memory 2>/dev/null \
    | grep -E 'used_memory_human|used_memory_peak_human|maxmemory_human|maxmemory_policy'
  kubectl -n $NS exec "$RED_POD" -- redis-cli info stats 2>/dev/null \
    | grep -E 'evicted_keys|rejected_connections'
  echo "restarts: $(kubectl -n $NS get pod "$RED_POD" -o jsonpath='{.status.containerStatuses[0].restartCount}')"
  echo "limit   : $(kubectl -n $NS get pod "$RED_POD" -o jsonpath='{.spec.containers[0].resources.limits.memory}')"
  echo "NOTE: maxmemory 0 + a 512Mi cgroup limit means Redis never evicts, it gets OOM-killed."
fi

# ---------------------------------------------------------------- gluetun
hr "gluetun tunnel state"
kubectl -n $NS logs "$WEB_POD" -c gluetun --tail=40 2>/dev/null
echo
echo "--- public IP as seen through the tunnel ---"
kubectl -n $NS exec "$WEB_POD" -c gluetun -- wget -qO- -T 10 https://ipinfo.io/json 2>/dev/null \
  || echo "(could not reach ipinfo)"

hr "gluetun restarts / tunnel flaps in the last hour"
kubectl -n $NS logs "$WEB_POD" -c gluetun --since=1h 2>/dev/null \
  | grep -Eic 'reconnect|timeout|failed|error' \
  | xargs -I{} echo "error-ish log lines in last hour: {}"

# ---------------------------------------------------------------- app metrics
hr "Dispatcharr stream metrics (ground truth)"
kubectl -n $NS exec "$WEB_POD" -c gluetun -- \
  wget -qO- -T 10 http://127.0.0.1:9192/metrics 2>/dev/null \
  | grep -E '^dispatcharr_(active_clients|stream_buffering_speed|stream_index|client_current_transfer_rate_bps|profile_connection|stream_uptime)' \
  | head -40 \
  || echo "(metrics endpoint unreachable on :9192)"

# ---------------------------------------------------------------- node level
hr "node conditions + capacity for $NODE"
kubectl describe node "$NODE" 2>/dev/null \
  | sed -n '/Conditions:/,/Addresses:/p;/Allocated resources:/,/Events:/p'

hr "kube-proxy regression check (must be NotFound)"
kubectl -n kube-system get ds kube-proxy 2>&1 | tail -2

hr "Cilium config — MTU + routing mode (T1-A root cause)"
kubectl -n kube-system get configmap cilium-config -o yaml 2>/dev/null \
  | grep -Ei '  (mtu|routing-mode|tunnel|enable-ipv4-masquerade|cluster-pool-ipv4-cidr)' \
  || echo "(cilium-config not readable)"

hr "Longhorn volumes for dispatcharr"
kubectl -n longhorn-system get volumes.longhorn.io 2>/dev/null \
  | grep -Ei 'dispatcharr|NAME' || kubectl -n $NS get pvc

hr "done"
echo "Snapshot written to: $OUT/"
echo
echo "NEXT: run ./scripts/dispatcharr-watch.sh 600 | tee $OUT/watch.log while actively watching TV."
