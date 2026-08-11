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
- **hermes** — Hermes Agent (Nous Research) + Hermes WebUI in `hermes` namespace
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

**The gluetun image is pinned to `v3.41.3`.** Untagged (`image: qmcgaw/gluetun`) resolves to
`:latest`, which makes Kubernetes default `imagePullPolicy` to `Always` — so every pod restart
silently pulled a master build, invisible to both this repo and Renovate. The pod that ran from
2026-08-10 to 2026-08-11 logged `You are running on the bleeding edge of latest!`.

An earlier version of this note claimed a version tag might predate `HEALTH_RESTART_VPN` and told
you to pin by digest instead. **That was wrong.** `HEALTH_RESTART_VPN` shipped in **v3.41.0**
("New option `HEALTH_RESTART_VPN`: you should really leave it to `on`, unless you have trust
issues with the healthcheck"). A stable tag keeps the fix — pin by tag, and let Renovate move it.

**DNS does not go through gluetun.** `DOT=off` and `DNS_KEEP_NAMESERVER=on` are set deliberately.
gluetun's defaults enable a DNS-over-TLS proxy on `127.0.0.1:53` that resolves via Cloudflare
*through the tunnel*, and it rewrites `/etc/resolv.conf` to point at itself. Critically, **kubelet
generates one `resolv.conf` per Pod and bind-mounts it into every container**, so that rewrite
captured the `dispatcharr` container as well — confirmed 2026-08-11, both containers showed
`nameserver 127.0.0.1`.

Consequences, all of which were live for weeks:

- Every provider hostname lookup depended on tunnel state. A momentary tunnel stall became
  `Failed to resolve hostname ...: Temporary failure in name resolution` in ffmpeg, the stream
  died, Dispatcharr burned through all its alternates (which failed identically), and the viewer
  saw buffering. A failed lookup kills a stream exactly as dead as a saturated link, which is why
  every throughput measurement came back clean while streams kept dropping.
- The pod bypassed the network-level DNS policy completely. All DNS is supposed to go to
  `10.1.10.4` / `10.1.10.5` or the UniFi gateway; gluetun's DoT queries were encrypted inside
  WireGuard and exited at Mullvad, so those rules never saw them.
- gluetun's `Block malicious: yes` blocklist was being applied to provider hostnames.

Verified at the time of the fix: `10.1.10.4` and CoreDNS both resolved the provider hostname from
inside that pod in milliseconds, while gluetun's `127.0.0.1:53` timed out. Diagnose with
`kubectl -n dispatcharr exec <web-pod> -c dispatcharr -- cat /etc/resolv.conf` — anything other
than the CoreDNS service IP means gluetun has taken DNS over again.

Tradeoff: provider hostnames now resolve from home rather than from the VPN exit, so a CDN may
hand back an edge near the house while traffic egresses from the exit's region. That is a far
smaller problem than losing name resolution whenever the tunnel hiccups.

**Gotcha:** gluetun's own iptables killswitch firewall applies to the whole Pod, not just the
gluetun container, because Kubernetes Pods share one network namespace. Any port that needs to be
reachable — Dispatcharr's `9191`, and gluetun's own health-check port `9999` used by the
`startupProbe` — must be listed in `FIREWALL_INPUT_PORTS` on the gluetun container, or it gets
silently dropped. If a future port gets added to this pod, add it there too.

## Hermes Agent

Nous Research's self-hosted agent (`workloads/hermes/`), reached on **10.1.20.205:8787** — its own
MetalLB IP, deliberately *not* behind Traefik. LAN/VPN access only; no tunnel, no public exposure.

**One pod, two containers, one PVC.** This mirrors upstream's `docker-compose.two-container.yml`:

- `hermes-agent` (`gateway run`) — Discord bot, scheduled/cron jobs, agent API on `8642`. This is
  the half that keeps working while you're away; the WebUI alone cannot tick scheduled jobs.
- `hermes-webui` (port `8787`) — the browser UI, and the backend the **Hermex** iPhone app talks
  to. Hermex does *not* speak to the agent directly; it drives hermes-webui, a separate
  third-party project (`nesquena/hermes-webui`), which is why that container exists at all.

They share `HERMES_HOME` (PVC subPath `home`) — that shared directory is the only reason the two
see the same sessions, memory and skills. They are in one pod rather than two Deployments because
Longhorn is ReadWriteOnce: two pods cannot mount that PVC unless they land on the same node.

**Never scale past 1 replica.** Hermes is a strict single-writer over `HERMES_HOME`; a second
writer corrupts lock files and `gateway_state.json`. Strategy is `Recreate` so a rolling update
can't briefly run two pods.

**`config.yaml` is seeded once, not managed.** The `prepare` initContainer copies it from the
`hermes-config` ConfigMap only if it does not already exist on the PVC. Hermes rewrites its own
config at runtime (model switching from the WebUI, onboarding), so re-copying on every restart
would silently revert whatever you changed in the app. **Editing the ConfigMap does not affect a
running install** — change the model in the WebUI, or delete `home/config.yaml` off the PVC and
restart to force a reseed.

**Both images must start as root.** Their entrypoints do UID/GID alignment and mount prep, then
re-exec as uid 1000. So no `runAsNonRoot`/`readOnlyRootFilesystem` here — the WebUI additionally
`uv pip install`s the agent's dependencies into its own filesystem at boot. The `prepare`
initContainer chowns `home/` and `workspace/` to 1000 because `fsGroup` alone leaves root-created
directories at mode 755 (group r-x), which leaves the agent unable to write its own home.

**The agent source is an emptyDir, not a shared volume.** The WebUI installs the agent's Python
deps from a copy of `/opt/hermes`. Upstream shares it as a named Docker volume that only
initialises on first `up`, so it goes stale after an agent image bump and needs manual deletion.
Copying it into an emptyDir on every pod start means that upgrade footgun doesn't exist here.

**hermes-webui is pinned to an experimental tag on purpose.** Every stable release, up to
and including the newest (`v0.52.106`), installs the agent with
`uv pip install "$_stage_src[all]"` — not editable. That takes the PEP 517 `build_wheel`
path to `bdist_wheel`, and the agent's `setup.py` raises deliberately: *"Building wheels or
sdists for hermes-agent is not supported"* (a wheel would ship without its bundled locales,
skills, optional-mcps, `web_dist`, `tui_dist` and plugin manifests). The container then exits
1 with no useful message and crash-loops. `uv pip install -e` first appears in
`exp-v0.52.159`, so **no stable release currently works in a two-container setup**. Don't
"correct" the tag back to stable; re-check with
`curl -sS https://raw.githubusercontent.com/nesquena/hermes-webui/<tag>/docker_init.bash | grep 'uv pip install.*_stage_src'`
before moving it.

**Image tags:** the community Helm chart for this app defaults to `nousresearch/hermes-agent:0.8.0`,
which **does not exist** on Docker Hub — real tags are calendar-versioned (`v2026.8.3`). We don't
use that chart, but don't trust `0.8.x` version numbers from its docs either. `hermes-webui` ships
hundreds of patch releases, so Renovate batches both images weekly as the `hermes agent` group.

**Access control is one password.** Anything on the LAN can hit 10.1.20.205:8787 directly, so
`HERMES_WEBUI_PASSWORD` is the only thing in front of an agent that has a shell, a browser, and
your OpenRouter key. Discord access is separately gated by `DISCORD_ALLOWED_USERS` with
`GATEWAY_ALLOW_ALL_USERS=false`; an empty allowlist locks everyone out rather than letting
everyone in. `browser.allow_private_urls` is `false` to keep the agent's browser off internal
admin UIs — this pod sits on the cluster network with reach into the rest of the LAN.

Secrets (`hermes-secrets`) come from Azure Key Vault: `hermes-openrouter-api-key`,
`hermes-discord-bot-token`, `hermes-webui-password`.

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

## Debugging Rule

**Verify before changing.** Diagnose from the actual failure output — container logs, the
upstream source of whatever is failing, the real image tags — not from a plausible-sounding
theory. A fix pushed on an unverified hypothesis costs a full sync cycle and buries the real
cause. If the evidence to confirm a cause isn't in hand, get that evidence first and say
plainly that the cause is still unknown. Assumptions stated as conclusions have repeatedly
made incidents in this repo take longer than they needed to.

## Notes

- Private repo; access token configured separately in ArgoCD
- No secrets stored in this repo
- Multi-source Applications (ArgoCD 2.6+) used for Helm charts with local values
