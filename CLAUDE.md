# HomelabArgoCD

## Purpose

GitOps source of truth for all application deployments on the homelab Kubernetes cluster. Ansible bootstraps the cluster infrastructure (Cilium, MetalLB, Longhorn, ArgoCD), then hands off to ArgoCD which manages everything in this repo, including Traefik.

## Architecture

Uses the **app-of-apps pattern**:
- `bootstrap/root-app.yaml` — Root Application applied once manually, watches the `apps/` directory
- `apps/` — Individual ArgoCD Application manifests (one per app)
- `values/` — Helm values files referenced by Application manifests

Adding a new app: create an Application manifest in `apps/` and (if Helm) a values file in `values/`. ArgoCD auto-syncs via the root app.

Raw Kubernetes manifests (IngressRoutes, etc.) go in `manifests/` and are managed by the `manifests` ArgoCD Application.

## Repo Structure

```
bootstrap/       # Root app-of-apps manifest (one-time kubectl apply)
apps/            # Child Application manifests (auto-discovered by root app)
values/          # Helm values files for chart-based applications
manifests/       # Raw K8s manifests (IngressRoutes, etc.), organized by subdirectory
```

## Cluster Infrastructure

**Managed by Ansible (not in this repo):**
- Cilium (CNI, kube-proxy replacement — kube-proxy itself is intentionally NOT installed; see note below)
- MetalLB (bare-metal load balancer)
- Longhorn (distributed storage)
- ArgoCD itself

**Managed by ArgoCD (this repo):**
- Traefik (ingress controller) — Helm chart + IngressRoutes
- All application workloads deployed on the cluster

**Cluster topology:** 3 control-plane + 3 worker nodes (one worker was removed 2026-07 — one worker VM per physical host, not one-worker-per-physical-host-times-N)

**kube-proxy is deliberately absent.** Cilium runs with `kubeProxyReplacement=true`. kube-proxy
shipped alongside it for the cluster's entire life (a leftover from kubeadm's default `addon all`
phase) until removed 2026-07-22. Running both simultaneously caused a permanent bind conflict on
every Service's `healthCheckNodePort` (Cilium's `loadbalancer-healthserver` vs kube-proxy fighting
over the same port, logged continuously on every node) and intermittent conntrack desyncs on
long-lived connections — this is what caused stream corruption on the Dispatcharr LoadBalancer
service (`externalTrafficPolicy: Local`). `HomelabAnsible/K8sCluster.yml` now installs only the
`coredns` addon subphase, not `addon all`, so kube-proxy won't reappear on a cluster rebuild. If
you ever see `kubectl -n kube-system get ds kube-proxy` return anything, that's a regression —
delete it and clean up its nftables table on each node.

**Networking note:** kubeadm's `ClusterConfiguration` declares `podSubnet: 10.244.0.0/16`, but
Cilium's actual pod IPAM is `cluster-pool-ipv4-cidr: 10.0.0.0/8` (Cilium's Helm install never set a
custom pool, so it fell back to its own default rather than using kubeadm's declared subnet) —
real pod IPs are `10.0.x.x`, not `10.244.x.x`. `serviceSubnet: 10.96.0.0/12` (kubeadm-config) is
correct and does match reality, since that's assigned by the API server, not Cilium. Verify with
`kubectl -n kube-system get configmap cilium-config -o yaml | grep -i cidr` if in doubt — don't
trust the kubeadm-config podSubnet value for anything Cilium-related.

**Storage classes:**
- `longhorn` — General purpose storage; use this as the default for most workloads
- `longhorn-nvme` — Requires `nvme` disk tag on nodes (not currently configured); do not use
- `longhorn-sata` — Bulk SATA-backed storage for non-latency-sensitive data
- `longhorn-static` — For manually provisioned volumes

## Deployed Applications

- **traefik** — Ingress controller (Helm chart) in `traefik` namespace
- **kube-prometheus-stack** — Monitoring (Prometheus, Grafana, Alertmanager, node-exporter, kube-state-metrics) in `monitoring` namespace
- **manifests** — Raw K8s manifests (IngressRoutes for all UIs)

## Dispatcharr VPN Egress

Dispatcharr's web pod runs a `gluetun` sidecar (Mullvad WireGuard) to route its own outbound
internet traffic through a VPN, independent of which node it's scheduled on (UniFi PBR isn't
usable here — pods have no stable MAC/L2 identity on the LAN). Configured via `SERVER_HOSTNAMES`
in `workloads/dispatcharr/deployment-web.yaml`:

- Default: `us-qas-wg-306` (Ashburn, VA — Tzulo, 20 Gbps)
- Alternate Ashburn: `us-qas-wg-203` (was the default from 2026-08-07 to 2026-08-08)
- Canada override (use for up to ~24h): `ca-tor-wg-203` (Toronto — Tzulo, 10 Gbps) — **currently active as of 2026-08-08**

Both Ashburn servers (`-306` and `-203`) were misbehaving on 2026-08-08, which is why Toronto is
active. When reverting, try `us-qas-wg-306` first and confirm Ashburn is actually healthy again —
swapping between the two Ashburn hosts did not fix it last time.

To swap: edit `SERVER_HOSTNAMES`, commit, push. Revert the same way when done. Credentials
(`dispatcharr-vpn-secret`) come from Azure Key Vault via
`workloads/dispatcharr/external-secrets.yaml` — the WireGuard `Address` is tied to the Mullvad
account/key, not the server, so it's the same value regardless of which server is selected.

**`HEALTH_RESTART_VPN` is deliberately `off`.** This was the root cause of months of Dispatcharr
buffering, found 2026-08-10. gluetun health-checks the tunnel two ways — a TCP+TLS dial to
`cloudflare.com:443`/`github.com:443` every 5 minutes, and an ICMP ping to `1.1.1.1`/`8.8.8.8`
every minute — and by default *restarts the WireGuard tunnel* when they fail. Under live
streaming load those checks get starved and time out, so gluetun tears down the tunnel, which
severs every in-flight provider connection, which is the buffering. The streams then reconnect,
re-saturate the link, and the cycle repeats. On 2026-08-10 the tunnel restarted 20 times, every
5–10 minutes during viewing hours, with a 9.5-hour gap midday when nobody was watching.

This is invisible to normal monitoring: gluetun restarts the tunnel *internally*, so the
container never dies, the pod shows `RESTARTS 0`, and no Kubernetes event fires. It also survives
changing Mullvad exit servers, which is why four datacenter swaps (2026-08-07/08) changed nothing
— the fault is in gluetun's health logic, not the exit. Diagnose with
`kubectl -n dispatcharr logs <web-pod> -c gluetun --since=12h | grep '\[vpn\] starting'`, not with
pod status. `manifests/dispatcharr/gluetun-loki-rules.yaml` now alerts on it.

With the restart disabled the checks still run and still log; only the destructive remedy is
gone. Tradeoff: a genuinely dead tunnel will now stay dead rather than self-heal. The killswitch
means that fails closed (no leak) — Dispatcharr simply loses its providers.

**Pin the gluetun image.** `image: qmcgaw/gluetun` with no tag resolves to `:latest`, which makes
Kubernetes default `imagePullPolicy` to `Always` — so every pod restart silently pulls a new
gluetun build, invisible to both this repo and Renovate. Note that `latest` runs *ahead* of the
newest semver tag (the 2026-08-07 build post-dates `v3.41.3`), so pin by **digest**, not by
version tag: a version tag old enough to exist may predate `HEALTH_RESTART_VPN` and silently
undo the fix above.

**Gotcha:** gluetun's own iptables killswitch firewall applies to the whole Pod, not just the
gluetun container, because Kubernetes Pods share one network namespace. Any port that needs to be
reachable — Dispatcharr's `9191`, and gluetun's own health-check port `9999` used by the
`startupProbe` — must be listed in `FIREWALL_INPUT_PORTS` on the gluetun container, or it gets
silently dropped. If a future port gets added to this pod, add it there too.

## Traefik Entrypoints & UI Access

Traefik runs two LoadBalancer services with separate IPs (assigned by MetalLB). Each port's `expose` map in `values/traefik.yaml` controls which service it appears on.

**Default service (IP #1) — Infrastructure UIs:**

| Service      | Port  | IngressRoute Location                          |
|-------------|-------|-------------------------------------------------|
| Traefik     | 8080  | Built-in dashboard                              |
| Longhorn    | 8088  | `manifests/traefik/longhorn-ingressroute.yaml`  |
| ArgoCD      | 9443  | `manifests/traefik/argocd-ingressroute.yaml`    |

**Monitoring service (IP #2) — Monitoring UIs:**

| Service      | Port  | IngressRoute Location                          |
|-------------|-------|-------------------------------------------------|
| Grafana     | 3000  | `manifests/traefik/grafana-ingressroute.yaml`   |
| Prometheus  | 9090  | `manifests/traefik/prometheus-ingressroute.yaml` |
| Alertmanager| 9093  | `manifests/traefik/alertmanager-ingressroute.yaml`|

To expose a new service: add an entrypoint in `values/traefik.yaml` (with the appropriate `expose` map for default vs monitoring service) and an IngressRoute in `manifests/traefik/`.

## Bootstrap Process

```bash
kubectl apply -f bootstrap/root-app.yaml
```

This creates the root Application in ArgoCD, which discovers and syncs all apps in `apps/`.

## Adding a New App

1. Create `apps/<app-name>.yaml` with an ArgoCD Application manifest
2. If using Helm, create `values/<app-name>.yaml` with chart values
3. If it needs an IngressRoute, add an entrypoint in `values/traefik.yaml` and create `manifests/traefik/<app>-ingressroute.yaml`
4. **If the app uses a PVC**, create a k8up `Schedule` manifest in `manifests/k8up/<app-name>-backup.yaml` to configure backups
5. **Always** add the new Helm chart to `renovate.json` `packageRules` if it should be grouped, or verify the ArgoCD manager in `renovate.json` will auto-detect it (charts referenced in `apps/` are auto-discovered)
6. Commit and push to `main` — ArgoCD auto-syncs

## Notes

- Private repo; access token configured separately in ArgoCD
- No secrets stored in this repo
- Multi-source Applications (ArgoCD 2.6+) used for Helm charts with local values
