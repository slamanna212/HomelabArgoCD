# HomelabArgoCD

GitOps source of truth for all application deployments on the homelab Kubernetes cluster, managed by ArgoCD using the app-of-apps pattern.

## Overview

Ansible bootstraps cluster infrastructure (Cilium, MetalLB, Longhorn, ArgoCD), then hands off to ArgoCD which manages everything in this repo going forward.

```
bootstrap/       # Root app-of-apps manifest (one-time kubectl apply)
apps/            # Child Application manifests (auto-discovered by root app)
values/          # Helm values files for chart-based applications
manifests/       # Raw K8s manifests (IngressRoutes, etc.), organized by subdirectory
cloudflared/     # Cloudflare tunnel deployment manifests
```

---

## First-Time Setup

### Prerequisites

- A running Kubernetes cluster (3 control-plane + 4 worker nodes recommended)
- `kubectl` configured with cluster admin access
- ArgoCD installed on the cluster (see below)
- An Azure Key Vault instance (used as the External Secrets backend)

### Step 1: Bootstrap Cluster Infrastructure (Ansible)

The following components are managed by Ansible and must be installed before this repo takes over:

- **Cilium** — CNI, replaces kube-proxy
- **MetalLB** — bare-metal load balancer (assigns IPs to LoadBalancer services)
- **Longhorn** — distributed storage (`longhorn-nvme` and `longhorn-sata` storage classes)
- **ArgoCD** — GitOps controller

Run your Ansible playbooks to provision these before proceeding. This repo assumes they are already present.

### Step 2: Install ArgoCD (if not done via Ansible)

```bash
kubectl create namespace argocd
kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
```

Wait for ArgoCD to be ready:

```bash
kubectl wait --for=condition=available --timeout=300s deployment/argocd-server -n argocd
```

### Step 3: Connect ArgoCD to This Repo

This is a private repo. ArgoCD needs credentials to pull from it.

**Via ArgoCD CLI:**

```bash
# Log in to ArgoCD
argocd login <ARGOCD_SERVER> --username admin --password <ADMIN_PASSWORD>

# Add the repo with a personal access token
argocd repo add https://github.com/slamanna212/HomelabArgoCD.git \
  --username <GITHUB_USERNAME> \
  --password <GITHUB_PAT>
```

**Via kubectl (alternative):**

```bash
kubectl create secret generic repo-homalabargocd \
  --namespace argocd \
  --from-literal=type=git \
  --from-literal=url=https://github.com/slamanna212/HomelabArgoCD.git \
  --from-literal=username=<GITHUB_USERNAME> \
  --from-literal=password=<GITHUB_PAT>

kubectl label secret repo-homalabargocd \
  --namespace argocd \
  argocd.argoproj.io/secret-type=repository
```

### Step 4: Configure External Secrets (Azure Key Vault)

Several apps (including Cloudflared) pull secrets from Azure Key Vault via the External Secrets Operator. A `ClusterSecretStore` named `azure-keyvault` must exist before ArgoCD syncs those apps.

1. Install External Secrets Operator (ArgoCD will do this automatically after bootstrap, but the ClusterSecretStore must be created manually once):

```bash
# Create the namespace
kubectl create namespace external-secrets

# Create the Azure credentials secret
kubectl create secret generic azure-keyvault-credentials \
  --namespace external-secrets \
  --from-literal=ClientID=<AZURE_CLIENT_ID> \
  --from-literal=ClientSecret=<AZURE_CLIENT_SECRET>
```

2. Apply the `ClusterSecretStore`:

```yaml
# Save as cluster-secret-store.yaml and apply
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata:
  name: azure-keyvault
spec:
  provider:
    azurekv:
      tenantId: "<AZURE_TENANT_ID>"
      vaultUrl: "https://<YOUR_KEYVAULT_NAME>.vault.azure.net"
      authSecretRef:
        clientId:
          name: azure-keyvault-credentials
          namespace: external-secrets
          key: ClientID
        clientSecret:
          name: azure-keyvault-credentials
          namespace: external-secrets
          key: ClientSecret
```

```bash
kubectl apply -f cluster-secret-store.yaml
```

**Required secrets in Azure Key Vault:**

| Secret Name              | Used By    | Description                  |
|--------------------------|------------|------------------------------|
| `cloudflare-tunnel-token`| Cloudflared| Cloudflare tunnel auth token |

Add any additional secrets your apps need before syncing.

### Step 5: Bootstrap ArgoCD with the Root App

Apply the root Application manifest once. This is the only manual `kubectl apply` needed — ArgoCD handles everything else from here.

```bash
kubectl apply -f bootstrap/root-app.yaml
```

ArgoCD will discover all Application manifests in `apps/` and begin syncing them automatically.

### Step 6: Verify

```bash
# Check all applications are syncing
argocd app list

# Or via kubectl
kubectl get applications -n argocd
```

All apps should reach `Synced` / `Healthy` status within a few minutes. Apps with ExternalSecret dependencies will only sync correctly after the `ClusterSecretStore` is configured (Step 4).

---

## Deployed Applications

| App                    | Namespace    | Type       | Description                                      |
|------------------------|--------------|------------|--------------------------------------------------|
| `traefik`              | `traefik`    | Helm       | Ingress controller                               |
| `kube-prometheus-stack`| `monitoring` | Helm       | Prometheus, Grafana, Alertmanager, node-exporter |
| `loki`                 | `monitoring` | Helm       | Log aggregation                                  |
| `promtail`             | `monitoring` | Helm       | Log shipping agent                               |
| `external-secrets`     | `external-secrets` | Helm | Syncs secrets from Azure Key Vault             |
| `k8up`                 | `k8up`       | Helm       | Kubernetes backup operator                       |
| `rancher`              | `cattle-system` | Helm    | Cluster management UI                            |
| `arc-controller`       | `arc-system` | Helm       | GitHub Actions Runner Controller                 |
| `cloudflared`          | `cloudflared`| Manifests  | Cloudflare tunnel (3-replica deployment)         |
| `manifests`            | various      | Raw K8s    | IngressRoutes and other raw manifests            |

---

## Traefik Entrypoints & UI Access

Traefik runs two LoadBalancer services with IPs assigned by MetalLB.

**Default service (IP #1) — Infrastructure UIs:**

| Service      | Port  | IngressRoute                                    |
|--------------|-------|-------------------------------------------------|
| Traefik      | 8080  | Built-in dashboard                              |
| Longhorn     | 8088  | `manifests/traefik/longhorn-ingressroute.yaml`  |
| ArgoCD       | 9443  | `manifests/traefik/argocd-ingressroute.yaml`    |

**Monitoring service (IP #2) — Monitoring UIs:**

| Service      | Port  | IngressRoute                                        |
|--------------|-------|-----------------------------------------------------|
| Grafana      | 3000  | `manifests/traefik/grafana-ingressroute.yaml`       |
| Prometheus   | 9090  | `manifests/traefik/prometheus-ingressroute.yaml`    |
| Alertmanager | 9093  | `manifests/traefik/alertmanager-ingressroute.yaml`  |

---

## Adding a New App

1. Create `apps/<app-name>.yaml` with an ArgoCD Application manifest
2. If using Helm, create `values/<app-name>.yaml` with chart values
3. If it needs an ingress, add an entrypoint in `values/traefik.yaml` and create `manifests/traefik/<app>-ingressroute.yaml`
4. Commit and push to `main` — ArgoCD auto-syncs via the root app

**Example Application manifest (Helm chart with local values):**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: my-app
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  sources:
    - repoURL: https://charts.example.com
      chart: my-chart
      targetRevision: "*"
      helm:
        valueFiles:
          - $values/values/my-app.yaml
    - repoURL: https://github.com/slamanna212/HomelabArgoCD.git
      targetRevision: main
      ref: values
  destination:
    server: https://kubernetes.default.svc
    namespace: my-app
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

---

## Storage Classes

| Class           | Backing | Use Case                        |
|-----------------|---------|---------------------------------|
| `longhorn-nvme` | NVMe    | Default — latency-sensitive     |
| `longhorn-sata` | SATA    | Bulk/non-latency-sensitive data |

---

## Notes

- Secrets are **not** stored in this repo — all secrets are managed via External Secrets Operator backed by Azure Key Vault
- The repo access token for ArgoCD is configured separately and not committed here
- Multi-source Applications (ArgoCD 2.6+) are used for Helm charts with local values files
- `syncPolicy.automated` with `prune: true` and `selfHeal: true` is set on all apps — removing an `apps/` manifest will delete the corresponding workload
