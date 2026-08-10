#!/usr/bin/env bash
# Step 0.5 — prove every node's datapath is IDENTICAL.
#
# Context: during the 2026-08-07 Cilium iptables session, ip rules were cleaned up
# by hand on each Kubernetes host. That very likely fixed the bind errors. It also
# means six hosts were each edited manually, and `ip rule` entries are Cilium's --
# kube-proxy never creates them. A per-node manual edit is the classic way to end up
# with five nodes in one state and one node in another.
#
# That asymmetry produces buffering that depends on WHERE THE POD IS SCHEDULED, which
# looks exactly like a flaky provider or a bad VPN exit and is invariant to both. Every
# rollout in this investigation rescheduled the web pod.
#
# So this script does not hunt for residue. It collects the datapath state of every
# node and DIFFS them against each other. No SSH needed: the Cilium agent runs
# hostNetwork + privileged, so `kubectl exec` into it sees the host's own rules.
#
# Usage: ./node-datapath-compare.sh [output-dir]

set -uo pipefail

OUT="${1:-datapath-$(date +%Y%m%d-%H%M%S)}"
NS=dispatcharr
mkdir -p "$OUT"

hr() { printf '\n=== %s ===\n' "$1"; }

# cilium agent pod -> node name
declare -A POD_OF_NODE
while read -r pod node; do
  [[ -n "$node" ]] && POD_OF_NODE["$node"]="$pod"
done < <(kubectl -n kube-system get pod -l k8s-app=cilium \
          -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.nodeName}{"\n"}{end}' 2>/dev/null)

if [[ ${#POD_OF_NODE[@]} -eq 0 ]]; then
  echo "FATAL: found no cilium agent pods (label k8s-app=cilium). Check the label and retry."
  exit 1
fi

NODES=$(printf '%s\n' "${!POD_OF_NODE[@]}" | sort)
echo "Cilium agents found on $(wc -l <<<"$NODES") nodes:"
printf '  %s\n' $NODES

cex() { # cex <node> <command...>
  kubectl -n kube-system exec "${POD_OF_NODE[$1]}" -c cilium-agent -- sh -c "$2" 2>/dev/null
}

# ------------------------------------------------------------------ collect
hr "collecting datapath state per node"
for n in $NODES; do
  echo "  -> $n"
  {
    echo "### ip rule"
    # strip the priority numbers' surrounding whitespace only; keep ordering
    cex "$n" 'ip rule show' | sed 's/[[:space:]]\+/ /g'
    echo
    echo "### routing tables in use"
    cex "$n" 'ip route show table all' | grep -oE 'table [a-z0-9_]+' | sort | uniq -c | sort -rn
    echo
    echo "### iptables chains"
    cex "$n" 'iptables-save 2>/dev/null | grep "^:" | cut -d" " -f1' | sort
    echo
    echo "### nft tables"
    cex "$n" 'nft list tables 2>/dev/null' | sort
    echo
    echo "### cilium-managed interfaces"
    cex "$n" 'ip -o link show' | awk -F': ' '{print $2}' | sed 's/@.*//' | sort
  } > "$OUT/$n.txt"
done

# ------------------------------------------------------------------ diff
REF=$(head -1 <<<"$NODES")
hr "DIFF — every node against reference node: $REF"
DIFFS=0
for n in $NODES; do
  [[ "$n" == "$REF" ]] && continue
  if diff -q "$OUT/$REF.txt" "$OUT/$n.txt" >/dev/null; then
    printf '  %-30s IDENTICAL\n' "$n"
  else
    printf '  %-30s *** DIFFERS ***\n' "$n"
    DIFFS=$((DIFFS+1))
    diff -u "$OUT/$REF.txt" "$OUT/$n.txt" | sed 's/^/      /' | head -50
    echo
  fi
done

if [[ $DIFFS -eq 0 ]]; then
  echo
  echo "  All nodes identical. The manual ip-rule cleanup landed consistently, and"
  echo "  'which node the pod is on' is NOT a variable. Move on to the MTU test."
else
  echo
  echo "  $DIFFS node(s) differ. Read the diffs above: a missing CILIUM_* chain or a"
  echo "  missing 'from all fwmark ... lookup 2004/2005' ip rule on one node means that"
  echo "  node's datapath is degraded, and streams buffer whenever the web pod lands"
  echo "  there. That is invariant to provider and to VPN exit."
fi

# ------------------------------------------------------------------ cilium health
hr "Cilium agent health + current bind errors per node"
for n in $NODES; do
  ERR=$(kubectl -n kube-system logs "${POD_OF_NODE[$n]}" -c cilium-agent --since=2h 2>/dev/null \
        | grep -icE 'address already in use|failed to bind|bind: |reconcil.*(fail|error)')
  ST=$(cex "$n" 'cilium-dbg status --brief 2>/dev/null || cilium status --brief 2>/dev/null' | head -1)
  printf '  %-30s bind/reconcile errors(2h)=%-5s status=%s\n' "$n" "$ERR" "${ST:-unknown}"
done
echo
echo "Nonzero error counts mean the problem the 08-07 session was chasing is NOT resolved."
echo "All zeros means the cleanup held, and this whole line of inquiry is closed."

hr "kube-proxy — confirm it stayed gone"
kubectl -n kube-system get ds kube-proxy >/dev/null 2>&1 \
  && echo "REGRESSION: kube-proxy DaemonSet is back." \
  || echo "OK: no kube-proxy DaemonSet."
for n in $NODES; do
  C=$(cex "$n" 'nft list tables 2>/dev/null' | grep -ic 'kube.\?proxy')
  printf '  %-30s kube-proxy nft tables=%s\n' "$n" "$C"
done

# ------------------------------------------------------------------ etp:Local plumbing
hr "externalTrafficPolicy=Local plumbing for dispatcharr"
HCP=$(kubectl -n $NS get svc dispatcharr -o jsonpath='{.spec.healthCheckNodePort}' 2>/dev/null)
echo "dispatcharr healthCheckNodePort: ${HCP:-<none>}"
POD_NODE=$(kubectl -n $NS get pod -l app=dispatcharr,component=web \
           -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null)
echo "web pod currently on         : ${POD_NODE:-unknown}"
echo
echo "Health server must answer 200 on the pod's node and 503 everywhere else."
echo "Anything else and MetalLB may be sending traffic to a node with no local endpoint:"
for n in $NODES; do
  [[ -z "$HCP" ]] && break
  CODE=$(cex "$n" "wget -qO- -S -T 3 http://127.0.0.1:$HCP/ 2>&1 | grep -oE 'HTTP/1.[01] [0-9]+' | head -1")
  EXP="503 (no local endpoint)"; [[ "$n" == "$POD_NODE" ]] && EXP="200 (has the pod)"
  printf '  %-30s got=%-22s expected=%s\n' "$n" "${CODE:-no-response}" "$EXP"
done

hr "done"
echo "Snapshot + per-node state written to: $OUT/"
echo
echo "Bottom line: if every node is identical, error counts are zero, and the health"
echo "server answers correctly, then node-level datapath state is fully ruled out and"
echo "the MTU test (Test 1) is where tonight should go."
