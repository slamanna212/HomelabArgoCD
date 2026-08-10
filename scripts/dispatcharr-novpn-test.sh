#!/usr/bin/env bash
# Test 2 — definitive "is it the VPN?" experiment.
#
# Stands up a PARALLEL copy of the Dispatcharr web deployment with the gluetun
# sidecar removed, on its own LoadBalancer IP, sharing the same Postgres, Redis
# and PVCs as production. Production is never modified.
#
# Point ONE player at the test IP, watch for 10 minutes, compare.
#   no buffering here  -> the tunnel is the cause (MTU / DNS / CPU starvation)
#   still buffering    -> the VPN is exonerated; move on to tests 3-6
#
# Usage:
#   ./dispatcharr-novpn-test.sh up
#   ./dispatcharr-novpn-test.sh status
#   ./dispatcharr-novpn-test.sh down      # ALWAYS run this when finished

set -euo pipefail

NS=dispatcharr
NAME=dispatcharr-web-novpn
TEST_IP="${TEST_IP:-10.1.20.204}"   # override if this address is in use

require() { command -v "$1" >/dev/null || { echo "FATAL: $1 required"; exit 1; }; }
require kubectl
require jq

up() {
  echo ">> Checking ArgoCD auto-sync is disabled..."
  POLICY=$(kubectl -n argocd get application dispatcharr -o jsonpath='{.spec.syncPolicy.automated}' 2>/dev/null || echo "")
  if [[ -n "$POLICY" ]]; then
    echo "!! WARNING: ArgoCD auto-sync is STILL ENABLED on the dispatcharr application."
    echo "!! selfHeal will revert live changes mid-test. Disable it first:"
    echo "!!   kubectl -n argocd patch application dispatcharr --type merge \\"
    echo "!!     -p '{\"spec\":{\"syncPolicy\":{\"automated\":null}}}'"
    echo "!! (This script only ADDS new resources, so it is safe to continue, but"
    echo "!!  tests 1/3/4 that mutate the live deployment are not.)"
    echo
  fi

  NODE=$(kubectl -n $NS get pod -l app=dispatcharr,component=web \
    -o jsonpath='{.items[0].spec.nodeName}')
  echo ">> Production web pod is on node: $NODE"
  echo ">> Pinning the test pod to the same node (the data/recordings PVCs are RWO,"
  echo "   and keeping the node constant isolates the VPN as the only variable)."

  echo ">> Building VPN-free copy of deployment/dispatcharr-web..."
  kubectl -n $NS get deployment dispatcharr-web -o json \
    | jq --arg name "$NAME" --arg node "$NODE" '
        # identity
        .metadata.name = $name
        | .metadata.labels.component = "web-novpn"
        | .spec.selector.matchLabels.component = "web-novpn"
        | .spec.template.metadata.labels.component = "web-novpn"

        # THE experiment: drop the gluetun sidecar, keep the wait-for-* initContainers
        | .spec.template.spec.initContainers =
            [ .spec.template.spec.initContainers[] | select(.name != "gluetun") ]

        # gluetun owned the tun device mount; nothing else needs it
        | .spec.template.spec.volumes =
            [ .spec.template.spec.volumes[] | select(.name != "dev-net-tun") ]

        # same node as production, so only the tunnel differs
        | .spec.template.spec.nodeSelector = {"kubernetes.io/hostname": $node}

        # strip server-populated fields so this is applyable
        | del(.metadata.uid, .metadata.resourceVersion, .metadata.generation,
              .metadata.creationTimestamp, .metadata.annotations, .metadata.ownerReferences,
              .status)
      ' \
    | kubectl apply -f -

  echo ">> Creating LoadBalancer service on $TEST_IP..."
  kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: $NAME
  namespace: $NS
  annotations:
    metallb.universe.tf/loadBalancerIPs: $TEST_IP
    kube-vip.io/ignore: "true"
spec:
  type: LoadBalancer
  externalTrafficPolicy: Local
  selector:
    app: dispatcharr
    component: web-novpn
  ports:
    - name: http
      port: 9191
      targetPort: 9191
      protocol: TCP
EOF

  echo ">> Waiting for rollout..."
  kubectl -n $NS rollout status "deployment/$NAME" --timeout=180s || true
  echo
  status
  cat <<EOF

------------------------------------------------------------------
TEST 2 IS LIVE.

  Production (via VPN) : http://10.1.20.202:9191
  Test      (no VPN)   : http://$TEST_IP:9191

Point ONE player at the test URL. Watch the SAME channel you used for the
baseline, for at least 10 minutes. Run the sampler alongside it.

WHEN FINISHED:  ./scripts/dispatcharr-novpn-test.sh down
------------------------------------------------------------------
EOF
}

status() {
  kubectl -n $NS get deployment "$NAME" 2>/dev/null || echo "(test deployment not present)"
  kubectl -n $NS get svc "$NAME" 2>/dev/null || true
  kubectl -n $NS get pod -l component=web-novpn -o wide 2>/dev/null || true
  echo
  POD=$(kubectl -n $NS get pod -l component=web-novpn -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -n "$POD" ]]; then
    echo "containers in test pod (gluetun must be ABSENT):"
    kubectl -n $NS get pod "$POD" -o jsonpath='{range .spec.containers[*]}  {.name}{"\n"}{end}'
    echo "public IP as seen by the test pod (should be your HOME IP, not Mullvad):"
    kubectl -n $NS exec "$POD" -c dispatcharr -- \
      python3 -c "import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('https://ipinfo.io/json',timeout=10).read().decode())" 2>/dev/null \
      || echo "  (could not check)"
  fi
}

down() {
  echo ">> Removing test resources..."
  kubectl -n $NS delete deployment "$NAME" --ignore-not-found
  kubectl -n $NS delete svc "$NAME" --ignore-not-found
  echo ">> Done. Production untouched."
  echo ">> Remember to re-enable ArgoCD auto-sync when all testing is complete:"
  echo "     kubectl -n argocd patch application dispatcharr --type merge \\"
  echo "       -p '{\"spec\":{\"syncPolicy\":{\"automated\":{\"prune\":true,\"selfHeal\":true}}}}'"
}

case "${1:-}" in
  up)     up ;;
  down)   down ;;
  status) status ;;
  *)      echo "Usage: $0 {up|status|down}"; exit 1 ;;
esac
