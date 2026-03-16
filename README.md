# HomelabArgoCD

GitOps source of truth for all application deployments on the homelab Kubernetes cluster, managed by ArgoCD using the app-of-apps pattern.

The [HomelabAnsible](https://github.com/slamanna212/HomelabAnsible) playbooks bootstrap the cluster and install ArgoCD. Once ArgoCD is running, this repo takes over all application management.

## Architecture

```
bootstrap/       # Root app-of-apps manifest (one-time kubectl apply)
apps/            # Child Application manifests (auto-discovered by root app)
values/          # Helm values files for chart-based applications
manifests/       # Raw K8s manifests (IngressRoutes, etc.), organized by subdirectory
cloudflared/     # Cloudflare tunnel deployment manifests
```

---

## First-Time Setup

### What Ansible Already Did

After running `K8sCluster.yml` from [HomelabAnsible](https://github.com/slamanna212/HomelabAnsible), the cluster is in this state:

| Component | Details |
|-----------|---------|
| **OS** | Debian 13 on all nodes |
| **Kubernetes** | v1.35, 3 control-plane + 4 worker nodes |
| **Container runtime** | containerd (SystemdCgroup) |
| **CNI** | Cilium (kube-proxy replacement mode) |
| **HA control plane** | kube-vip VIP at `10.90.90.10:6443` |
| **Load balancer** | MetalLB, IP pool `10.90.90.200–10.90.90.250` (L2 mode) |
| **Storage** | Longhorn with `longhorn-nvme` (default) and `longhorn-sata` storage classes |
| **Ingress** | Traefik (minimal install — ArgoCD will take over full config from this repo) |
| **GitOps** | ArgoCD installed via Helm, namespace `argocd`, running in insecure mode |

`kubectl` is configured on the control-plane nodes. Retrieve the kubeconfig from the first control node if you need it locally:

```bash
scp ansible@<control-node>:~/.kube/config ~/.kube/config
```

---

### Step 1: Connect ArgoCD to This Repo

This is a private repo. ArgoCD needs credentials before it can sync anything.

Get the initial ArgoCD admin password (Ansible prints it at the end of the playbook, or retrieve it manually):

```bash
kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath="{.data.password}" | base64 -d && echo
```

Add the repository credentials via the ArgoCD CLI:

```bash
# Log in (Traefik exposes ArgoCD at port 9443 on the default LoadBalancer IP)
argocd login <TRAEFIK_DEFAULT_IP>:9443 --username admin --password <ADMIN_PASSWORD> --insecure

# Add the private repo with a GitHub personal access token
argocd repo add https://github.com/slamanna212/HomelabArgoCD.git \
  --username <GITHUB_USERNAME> \
  --password <GITHUB_PAT>
```

Alternatively, via `kubectl`:

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

---

### Step 2: Configure External Secrets (Azure Key Vault)

Several apps pull secrets from Azure Key Vault via the External Secrets Operator. A `ClusterSecretStore` named `azure-keyvault` must exist before those apps will sync successfully. The External Secrets Operator itself is deployed by ArgoCD from this repo — but the `ClusterSecretStore` and its backing credentials must be created manually once.

**Create the Azure credentials secret:**

```bash
kubectl create namespace external-secrets

kubectl create secret generic azure-keyvault-credentials \
  --namespace external-secrets \
  --from-literal=ClientID=<AZURE_CLIENT_ID> \
  --from-literal=ClientSecret=<AZURE_CLIENT_SECRET>
```

**Apply the ClusterSecretStore:**

```yaml
# cluster-secret-store.yaml
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

> **Note:** Apply this after the External Secrets Operator is running (i.e., after Step 3 triggers its sync and the CRDs are installed). If you apply it beforehand the CRD won't exist yet.

**Required secrets in Azure Key Vault:**

| Secret Name               | Used By     | Description                   |
|---------------------------|-------------|-------------------------------|
| `cloudflare-tunnel-token` | Cloudflared | Cloudflare tunnel auth token  |

Add any additional secrets your apps need.

---

### Step 3: Bootstrap ArgoCD with the Root App

This is the only `kubectl apply` needed from this repo. Everything else is managed by ArgoCD from here on.

```bash
kubectl apply -f bootstrap/root-app.yaml
```

ArgoCD will discover all Application manifests in `apps/` and begin syncing them automatically, including installing the full Traefik config, External Secrets Operator, Longhorn storage classes, monitoring stack, and all other apps.

---

### Step 4: Apply the ClusterSecretStore (if not done in Step 2)

Once the External Secrets Operator pods are running:

```bash
kubectl apply -f cluster-secret-store.yaml
```

Apps that depend on ExternalSecrets (e.g., Cloudflared) will complete syncing once this is in place.

---

### Step 5: Verify

```bash
# Check all applications
kubectl get applications -n argocd

# Or via CLI
argocd app list
```

All apps should reach `Synced` / `Healthy` within a few minutes. Apps with ExternalSecret dependencies will finalize once the ClusterSecretStore is ready.

---

## Deployed Applications

| App                     | Namespace         | Type      | Description                                           |
|-------------------------|-------------------|-----------|-------------------------------------------------------|
| `traefik`               | `traefik`         | Helm      | Ingress controller (full config, replaces Ansible install) |
| `kube-prometheus-stack` | `monitoring`      | Helm      | Prometheus, Grafana, Alertmanager, node-exporter      |
| `loki`                  | `monitoring`      | Helm      | Log aggregation                                       |
| `promtail`              | `monitoring`      | Helm      | Log shipping agent                                    |
| `external-secrets`      | `external-secrets`| Helm      | Syncs secrets from Azure Key Vault                    |
| `k8up`                  | `k8up`            | Helm      | Kubernetes backup operator                            |
| `rancher`               | `cattle-system`   | Helm      | Cluster management UI                                 |
| `arc-controller`        | `arc-system`      | Helm      | GitHub Actions Runner Controller                      |
| `cloudflared`           | `cloudflared`     | Manifests | Cloudflare tunnel (3-replica, HA spread across nodes) |
| `manifests`             | various           | Raw K8s   | IngressRoutes and supporting manifests                |

---

## Traefik Entrypoints & UI Access

Traefik runs two LoadBalancer services, each receiving an IP from MetalLB's `10.90.90.200–10.90.90.250` pool.

**Default service (IP #1) — Infrastructure UIs:**

| Service  | Port | IngressRoute                                   |
|----------|------|------------------------------------------------|
| Traefik  | 8080 | Built-in dashboard                             |
| Longhorn | 8088 | `manifests/traefik/longhorn-ingressroute.yaml` |
| ArgoCD   | 9443 | `manifests/traefik/argocd-ingressroute.yaml`   |

**Monitoring service (IP #2) — Monitoring UIs:**

| Service      | Port | IngressRoute                                         |
|--------------|------|------------------------------------------------------|
| Grafana      | 3000 | `manifests/traefik/grafana-ingressroute.yaml`        |
| Prometheus   | 9090 | `manifests/traefik/prometheus-ingressroute.yaml`     |
| Alertmanager | 9093 | `manifests/traefik/alertmanager-ingressroute.yaml`   |

To expose a new service: add an entrypoint in `values/traefik.yaml` and an IngressRoute in `manifests/traefik/`.

---

## Storage Classes

Both storage classes are provisioned by Longhorn across the 4 worker nodes. Each worker has a 100GB NVMe OS disk and a dedicated 150GB SATA SSD mounted at `/var/lib/longhorn-sata`.

| Class           | Backing | Mount                    | Replicas | Use Case                    |
|-----------------|---------|--------------------------|----------|-----------------------------|
| `longhorn-nvme` | NVMe    | `/var/lib/longhorn`      | 2        | Default — latency-sensitive |
| `longhorn-sata` | SATA    | `/var/lib/longhorn-sata` | 2        | Bulk / capacity-focused     |

---

## Adding a New App

1. Create `apps/<app-name>.yaml` with an ArgoCD Application manifest
2. If using Helm, create `values/<app-name>.yaml` with chart values
3. If it needs an ingress, add an entrypoint in `values/traefik.yaml` and create `manifests/traefik/<app>-ingressroute.yaml`
4. Commit and push to `main` — the root app auto-syncs within minutes

**Example manifest (Helm chart with local values, multi-source pattern):**

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

## Cluster Topology

| Role          | Count | vCPU  | RAM     | Storage                               |
|---------------|-------|-------|---------|---------------------------------------|
| Control plane | 3     | 4     | 4 GB    | 30 GB NVMe                            |
| Worker        | 4     | 8–16  | 8–32 GB | 100 GB NVMe + 150 GB SATA SSD         |

HA control plane is managed by **kube-vip** at `10.90.90.10:6443`.

---

## Notes

- Secrets are **not** stored in this repo — all secrets are managed via External Secrets Operator backed by Azure Key Vault
- ArgoCD repo credentials are configured separately and not committed here
- Multi-source Applications (ArgoCD 2.6+) are used for all Helm charts so values files stay in this repo
- `syncPolicy.automated` with `prune: true` and `selfHeal: true` is set on all apps — removing a manifest from `apps/` will delete the corresponding workload from the cluster
- ArgoCD runs in insecure mode (TLS terminated by Traefik at the ingress level)
