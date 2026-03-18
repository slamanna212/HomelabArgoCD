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
- Cilium (CNI, replaces kube-proxy)
- MetalLB (bare-metal load balancer)
- Longhorn (distributed storage)
- ArgoCD itself

**Managed by ArgoCD (this repo):**
- Traefik (ingress controller) — Helm chart + IngressRoutes
- All application workloads deployed on the cluster

**Cluster topology:** 3 control-plane + 4 worker nodes

**Storage classes:**
- `longhorn-nvme` — Default, fast NVMe-backed storage
- `longhorn-sata` — Bulk SATA-backed storage for non-latency-sensitive data

## Deployed Applications

- **traefik** — Ingress controller (Helm chart) in `traefik` namespace
- **kube-prometheus-stack** — Monitoring (Prometheus, Grafana, Alertmanager, node-exporter, kube-state-metrics) in `monitoring` namespace
- **manifests** — Raw K8s manifests (IngressRoutes for all UIs)

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
