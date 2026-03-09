# HomelabArgoCD

GitOps repo for homelab Kubernetes cluster application deployments, managed by ArgoCD using the app-of-apps pattern.

## Quickstart

```bash
# Bootstrap ArgoCD (one-time)
kubectl apply -f bootstrap/root-app.yaml
```

ArgoCD will auto-discover and sync all applications defined in `apps/`.

## Adding an App

1. Create an Application manifest in `apps/<app-name>.yaml`
2. For Helm charts, add values in `values/<app-name>.yaml`
3. Push to `main` — ArgoCD handles the rest

See [CLAUDE.md](CLAUDE.md) for full documentation.
