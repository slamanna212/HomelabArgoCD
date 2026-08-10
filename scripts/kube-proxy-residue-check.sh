#!/usr/bin/env bash
# Step 0.5 — verify the kube-proxy teardown actually completed on EVERY node.
#
# Deleting the kube-proxy DaemonSet stops it creating new rules. It does NOT:
#   * remove the nftables/iptables tables it already wrote on each node, and
#   * flush the conntrack entries those rules created.
# Both survive the delete and keep mangling long-lived connections — which is
# exactly the "stream corruption on the Dispatcharr LoadBalancer" mechanism
# already documented in HomelabArgoCD/CLAUDE.md.
#
# Cluster-wide checks run over kubectl. Per-node checks need SSH (or run the
# printed commands via Semaphore).
#
# Usage: ./kube-proxy-residue-check.sh [ssh-user]

set -uo pipefail
SSH_USER="${1:-ansible}"
NS=dispatcharr

hr() { printf '\n=== %s ===\n' "$1"; }

hr "1. Is the kube-proxy DaemonSet actually gone?"
if kubectl -n kube-system get ds kube-proxy >/dev/null 2>&1; then
  echo "REGRESSION: kube-proxy DaemonSet still exists."
  kubectl -n kube-system get ds kube-proxy
else
  echo "OK: no kube-proxy DaemonSet."
fi
echo "stray pods:"
kubectl -n kube-system get pods -l k8s-app=kube-proxy 2>&1 | tail -2
echo "stray configmap (harmless, but tells you whether cleanup was thorough):"
kubectl -n kube-system get cm kube-proxy 2>&1 | tail -2

hr "2. Is Cilium STILL logging bind failures, per node?"
echo "Any nonzero count means the port is still contended on that node."
for p in $(kubectl -n kube-system get pod -l k8s-app=cilium -o name 2>/dev/null); do
  NODE=$(kubectl -n kube-system get "${p#pod/}" -o jsonpath='{.spec.nodeName}' 2>/dev/null \
         || kubectl -n kube-system get "$p" -o jsonpath='{.spec.nodeName}')
  N=$(kubectl -n kube-system logs "$p" --since=2h 2>/dev/null \
      | grep -icE 'address already in use|failed to bind|bind: |healthCheckNodePort|reconcil.*fail')
  printf '  %-28s %-22s %s matching lines\n' "${p#pod/}" "$NODE" "$N"
done
echo
echo "sample of the actual messages (all cilium pods, last 2h):"
kubectl -n kube-system logs -l k8s-app=cilium --since=2h --prefix --tail=-1 2>/dev/null \
  | grep -iE 'address already in use|failed to bind|healthCheckNodePort' | tail -15

hr "3. Which port is contended?"
echo "Services with externalTrafficPolicy=Local get a healthCheckNodePort:"
kubectl get svc -A -o json 2>/dev/null | jq -r '
  .items[]
  | select(.spec.healthCheckNodePort != null)
  | "  \(.metadata.namespace)/\(.metadata.name)  healthCheckNodePort=\(.spec.healthCheckNodePort)  etp=\(.spec.externalTrafficPolicy)"'

hr "4. Per-node residue — RUN THESE ON EVERY NODE (workers AND control planes)"
NODES=$(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
echo "nodes: $(echo "$NODES" | tr '\n' ' ')"
cat <<'REMOTE'

--- commands to run on each node ---
  # stale nftables tables left behind by kube-proxy:
  sudo nft list tables | grep -iE 'kube.?proxy'

  # stale legacy/nft iptables rules:
  sudo iptables-save 2>/dev/null | grep -c 'KUBE-SVC\|KUBE-SEP\|KUBE-FW'
  sudo iptables-legacy-save 2>/dev/null | grep -c 'KUBE-SVC\|KUBE-SEP\|KUBE-FW'

  # who is holding the dispatcharr healthCheckNodePort (substitute the port from step 3):
  sudo ss -lntup | grep '<healthCheckNodePort>'

  # stale conntrack entries for the dispatcharr LoadBalancer IP:
  sudo conntrack -L 2>/dev/null | grep -c '10.1.20.202'

--- if residue is found, clean it on that node ---
  sudo nft delete table ip kube-proxy    2>/dev/null
  sudo nft delete table ip6 kube-proxy   2>/dev/null
  sudo conntrack -D -d 10.1.20.202       2>/dev/null   # drop stale DNAT entries
  sudo systemctl restart kubelet                        # optional, forces a clean resync
  # then restart the cilium agent on that node:
  #   kubectl -n kube-system delete pod -l k8s-app=cilium --field-selector spec.nodeName=<node>
REMOTE

echo
echo "attempting the per-node checks over SSH as '$SSH_USER' (ok to fail if no key here):"
for n in $NODES; do
  echo "--- $n ---"
  ssh -o BatchMode=yes -o ConnectTimeout=5 "$SSH_USER@$n" '
    printf "  nft kube-proxy tables : "; sudo nft list tables 2>/dev/null | grep -icE "kube.?proxy"
    printf "  KUBE- iptables rules  : "; sudo iptables-save 2>/dev/null | grep -c "KUBE-SVC\|KUBE-SEP\|KUBE-FW"
    printf "  conntrack -> .202     : "; sudo conntrack -L 2>/dev/null | grep -c "10.1.20.202"
  ' 2>/dev/null || echo "  (ssh unavailable from here — run the block above via Semaphore)"
done

hr "5. Where is the dispatcharr pod, and which node fronts the LoadBalancer?"
kubectl -n $NS get pod -l app=dispatcharr,component=web -o wide 2>/dev/null
echo
echo "With externalTrafficPolicy=Local + MetalLB L2, ALL client traffic lands on the"
echo "node above. If that node still has kube-proxy residue, every viewer sees it —"
echo "and it looks identical to a bad upstream provider or a bad VPN exit."

hr "done"
echo "A clean result here (no nft tables, zero bind errors on every node) is what"
echo "actually retires the kube-proxy theory. Anything else and this outranks the VPN."
