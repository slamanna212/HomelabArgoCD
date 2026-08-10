# Dispatcharr Buffering — Suspect List & Test Runbook

Status: investigation, 2026-08-10. Providers ruled out by the operator; four Mullvad exit
datacenters tried with no improvement.

The key insight framing this document: **swapping VPN exit servers does not change most of the
things that could be broken about the VPN path.** MTU, CPU scheduling for the tunnel, DNS
geolocation, and single-flow behaviour are all identical regardless of which Mullvad host is
selected. "We changed datacenters four times" rules out *one bad datacenter*; it does not rule
out *the way the tunnel is wired into the pod*.

---

## Timeline — reconstructed from git and session history

This matters more than any single config finding, because it shows the datacenter swaps were
never a clean experiment.

| Date | Event | Source |
|---|---|---|
| 2026-07-16 | gluetun VPN sidecar added to the web pod; four commits that day iterating on the killswitch and firewall ports | `c8d27d4`, `2d4dd20`, `cd3a189`, `9473abe` |
| 2026-07-16 | "Configure Dispatcharr MAC address for UniFi routing" — the UniFi PBR approach that gluetun replaced | session |
| 2026-07-29 | Dispatcharr 0.28.2 | `f9f5e78` |
| 2026-08-06 | "Dispatcharr restart commands" — trouble already underway | session |
| **2026-08-07 13:55–15:22** | **"Debug Cilium agent iptables reconciliation errors"** — kube-proxy removed and **ip rules cleaned by hand on each Kubernetes host**; CLAUDE.md written the same minute the session opened | session `013G2Fqn`, commit `2f7cbfa` |
| 2026-08-07 | VPN swap #1 → `us-qas-wg-203` | `c8f7479` |
| 2026-08-08 | VPN swap #2 → `ca-tor-wg-203` | `da0511e` |
| 2026-08-10 | Still buffering | — |

**The Cilium/kube-proxy work and the VPN datacenter swaps happened in the same 24 hours.** The
swaps were performed while the cluster datapath was actively being changed, so "we tried four
datacenters and none helped" cannot distinguish between "the VPN is fine" and "something else
was breaking streams the whole time."

Two things about that 2026-08-07 session still matter:

1. Its only source repo was HomelabArgoCD, so it could not have touched Ansible — which is why
   the Ansible change its own note describes was never made (below).
2. No transcript is retained, only metadata, and the session was archived in a `need_input`
   state. The per-node ip-rule cleanup is confirmed by the operator, not by the record — so
   *which* nodes were touched and *what* was removed is not written down anywhere. That is worth
   fixing in CLAUDE.md regardless of tonight's outcome.

### The documentation and the repo disagree

`HomelabArgoCD/CLAUDE.md` states:

> `HomelabAnsible/K8sCluster.yml` now installs only the `coredns` addon subphase, not `addon all`,
> so kube-proxy won't reappear on a cluster rebuild.

That is **not true**. `HomelabAnsible/K8sCluster.yml:138` still reads:

```yaml
- name: Complete kubeadm init - addon
  ansible.builtin.command: kubeadm init phase addon all --config /etc/kubernetes/kubeadm-config.yaml
```

The Ansible repo's most recent commit of any kind is 2026-07-04, and `K8sCluster.yml` has not
been touched since 2026-05-07. The change was described as done, but never made. **This branch
now makes it** — see the companion commit on `HomelabAnsible`.

---

## 0. READ THIS BEFORE TESTING — ArgoCD will fight you

`apps/dispatcharr.yaml` has:

```yaml
syncPolicy:
  automated:
    prune: true
    selfHeal: true
```

Any live `kubectl edit` / `kubectl set env` / `kubectl scale` against Dispatcharr resources
**will be reverted by ArgoCD**, typically within ~3 minutes of the next reconcile. Every A/B test
below will silently undo itself and you will conclude "no change" from a test that never ran.

Disable auto-sync first:

```bash
kubectl -n argocd patch application dispatcharr --type merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'
```

Re-enable when finished:

```bash
kubectl -n argocd patch application dispatcharr --type merge \
  -p '{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true}}}}'
```

Verify it actually took before you start: `kubectl -n argocd get application dispatcharr -o jsonpath='{.spec.syncPolicy}'; echo`

---

## 1. Suspect list

Ranked by (likelihood x how well it explains "changing datacenters didn't help").

### T1-0 — Node datapath asymmetry after the manual per-node ip-rule cleanup

**Correction from an earlier draft of this document:** I initially ranked "incomplete kube-proxy
teardown" as a co-leading suspect on the theory that the per-node cleanup might never have
happened. The operator confirms it did — ip rules were cleaned by hand on each Kubernetes host
during the 2026-08-07 session (which ran ~90 minutes, consistent with that work). **That theory
is retired.** What replaces it is narrower and follows directly from *how* the fix was applied.

Note what was cleaned: **`ip rule` entries are policy routing, and kube-proxy does not create
them. Cilium does.** So the cleanup was operating on rules that plausibly belonged to Cilium — or
to an older Cilium configuration — on six hosts, by hand, one at a time.

The risk is not residue. It is **asymmetry**: five nodes ending in one state and one node in
another, either because the edit was applied slightly differently somewhere, or because Cilium
re-added what it needed on the nodes whose agent happened to restart afterward and not on the
others. A node missing a `CILIUM_*` chain or an fwmark-based lookup rule has a subtly degraded
datapath.

That produces buffering **that depends on which node the web pod is scheduled on** — which looks
exactly like a flaky provider or a bad VPN exit, and is invariant to both. Every rollout during
this investigation (image bumps, VPN host swaps, `set env`) rescheduled that pod, so the symptom
would appear to wander for no visible reason.

**Check:** `./scripts/node-datapath-compare.sh`. It does not hunt for residue — it collects
`ip rule`, routing tables, iptables chains, nft tables and link state from every node and **diffs
them against each other**. No SSH required: the Cilium agent runs hostNetwork + privileged, so
`kubectl exec` into it reads the host's own rules.

**Decisive evidence:** any node whose datapath differs from the others, any Cilium agent still
logging bind or reconcile errors, or a `healthCheckNodePort` health server not answering 200 on
the pod's node and 503 everywhere else.

**If all nodes are identical and error counts are zero,** this entire line of inquiry is closed —
including the original kube-proxy question — and the MTU test is where tonight should go.

### T1-A — MTU mismatch: WireGuard stacked inside Cilium VXLAN

**The single best explanation for the symptoms.**

`HomelabAnsible/K8sCluster.yml` installs Cilium with no `MTU` and no `routingMode` override:

```
helm upgrade --install cilium cilium/cilium
  --set kubeProxyReplacement=true
  --set k8sServiceHost=... --set k8sServicePort=6443
  --set nodePort.enableHealthCheckLoadBalancerIP=true
```

That means Cilium runs in its default **VXLAN tunnel** mode and auto-derives pod MTU from the
node NIC: 1500 − 50 (VXLAN overhead) = **1450** inside the pod.

`workloads/dispatcharr/deployment-web.yaml` sets no `WIREGUARD_MTU`, so gluetun uses its default
of **1400** on `tun0`. A full-size tunnel packet is then:

```
1400 (tun payload) + 32 (WireGuard) + 8 (UDP) + 20 (IPv4) = 1460 bytes on pod eth0
```

1460 > 1450. Every maximum-size packet crossing the tunnel needs fragmentation, and with DF set
(which is normal for TCP) it is **dropped**. Provider TCP connections then rely on Path MTU
Discovery working end to end through the Mullvad exit — which is exactly the kind of thing that
black-holes. The failure mode is not "no stream"; it is "stream establishes, small packets flow,
large segments stall and retransmit" — i.e. buffering.

Nothing in either repo clamps MSS anywhere, and there is no MTU setting in the entire Ansible
repo (`grep -rn mtu` returns nothing).

Datacenter swaps do not change any of this arithmetic. That is why they didn't help.

**Fix if confirmed:** `WIREGUARD_MTU: "1320"` on the gluetun container (1320 leaves comfortable
headroom for VXLAN + WG + any provider-side encapsulation).

### T1-B — gluetun has no CPU/memory reservation; the whole pod is capped at 2 cores

The gluetun container in `deployment-web.yaml` has **no `resources` block at all**. The
dispatcharr container has `limits.cpu: 2000m`. Two consequences:

1. The pod is QoS class **Burstable**, and gluetun is the container with zero guarantee. Under
   node CPU pressure, the process moving every byte of every stream is the first thing the
   scheduler starves. Dropped packets in a UDP tunnel read as jitter/buffering downstream.
2. There is history here: commit `7858791` is literally *"increase dispatcharr CPU limit to 2
   cores to resolve throttling"*. CFS throttling was a real problem before and the fix was to
   raise the ceiling, not remove it. Every concurrent client adds ffmpeg work. Once you cross
   2000m, the cgroup is hard-stopped for the remainder of each 100 ms period — which presents
   to viewers as periodic micro-stutter across *all* channels simultaneously.

**Watch:** `nr_throttled` / `throttled_usec` in the container's `cpu.stat` (script does this).

### T1-C — Celery is force-pinned to the same node as web, and shares its disks

`deployment-celery.yaml` has a **required** podAffinity onto `app=dispatcharr,component=web`.
It has to, because `dispatcharr-data` and `dispatcharr-recordings` are both `ReadWriteOnce` and
both deployments mount both. So by construction:

- M3U refresh and EPG parsing (CPU- and memory-hungry — celery's memory limit has already been
  raised twice, 2Gi then 4Gi) run on the exact node serving live video.
- Celery is allowed another 1000m CPU on top of web's 2000m.
- DVR recordings write to `longhorn-sata` with `numberOfReplicas: 2`, so every recorded byte is
  also a synchronous network write to a second node — from the same node that is streaming.

If buffering is **periodic** rather than constant, this is your prime suspect and the period will
match your M3U/EPG refresh interval. If buffering is **worse while recording**, same suspect.

### T1-D — Redis is undersized, unbounded, and unmonitored

`redis.yaml`: 512Mi limit, no `maxmemory`, no `maxmemory-policy`, no persistence, no liveness or
readiness probe, no PDB, and **no alert anywhere for Redis restarts**. Dispatcharr uses Redis for
channel/stream state and buffer coordination. With no `maxmemory` configured, Redis never evicts
— it grows until the kernel OOM-kills the container. Every client on every channel rebuffers at
that moment, Redis comes back empty, and the only trace is a restart counter nobody is watching.

**Watch:** `kube_pod_container_status_restarts_total{namespace="dispatcharr"}` and `redis-cli info memory`.

### T2-E — DNS geolocation mismatch (very plausible, frequently missed)

The `dispatcharr` container resolves through **CoreDNS**, not through gluetun. `/etc/resolv.conf`
is per-container, and only gluetun's own copy gets rewritten — the shared network namespace does
not share resolver config. gluetun's firewall explicitly permits `10.96.0.0/12` outbound, so
cluster DNS keeps working and nothing looks broken.

Result: provider hostnames resolve to whichever CDN edge is nearest **your house**, and then
Dispatcharr connects to that edge **from Toronto (or Ashburn)**. Every stream takes a
transcontinental hairpin to an edge node deliberately chosen to be close to a place the traffic
isn't coming from. Latency and jitter both go up, and — critically — **this is identical no
matter which exit you pick**, which again matches "four datacenters, no change."

**Test:** resolve a provider hostname from inside the pod, then resolve the same name from the
VPN's own resolver, and compare the answers and the RTT to each.

### T2-F — Single WireGuard tunnel = single flow for every stream

All provider traffic multiplexes into one UDP 4-tuple to one endpoint. One loss/reorder event
hits every channel at once, and any shaping applied by your ISP or UniFi to that flow applies to
the whole service. This is inherent to the design, not a misconfiguration — but it means "one
stream buffers" and "all streams buffer together" are very different signals, and you should
record which one you're seeing.

### T2-G — All client traffic funnels through one node's uplink

`service.yaml` uses `externalTrafficPolicy: Local` with MetalLB in **L2 mode**
(`K8sCluster.yml` creates an `L2Advertisement`) and one web replica. Every viewer's bytes,
inbound provider traffic, celery's work, Longhorn replication for both PVCs, and whatever else
landed on that worker all share one virtio NIC on one Proxmox VM. Worth ruling out with node
counters rather than assuming a 1G link is plenty.

Note also: with `Local` + one replica, a pod reschedule is a **hard cut** for every viewer, not a
graceful failover.

### T2-H — Cloudflare tunnel path, if any client uses it

`cloudflared/deployment.yaml` runs with `limits.cpu: 100m`. If any viewer reaches Dispatcharr via
the external hostname instead of `10.1.20.202`, they are pulling video through a proxy limited to
a tenth of a core. That will buffer regardless of everything else in this document. Tunnel
ingress rules live in the Cloudflare dashboard (token-based config), so this cannot be confirmed
from the repo — **check whether a dispatcharr hostname is mapped**, and check whether the people
reporting buffering are on the LAN or remote.

### T3 — lower probability, cheap to rule out

- **`appProtocol: kubernetes.io/ws`** on the LB port in `service.yaml`. Not a standard value;
  harmless in most paths but it is an unusual hint to give Cilium's datapath. Removing it is free.
- **0.28.2 upgrade landed 2026-07-29** (`f9f5e78`), close to the VPN work on 07-16. If buffering
  started in that window, an app regression is as plausible as an infra cause. Rollback to
  0.27.2 is a one-line test.
- **Longhorn recurring jobs** — `filesystem-trim-recurringjob.yaml` and
  `snapshot-cleanup-recurringjob.yaml`. Check their cron against the times buffering occurs.
- **`dataLocality: disabled`** on the SATA/NVMe storage classes means reads can come from a
  replica on another node.
- **Proxmox steal time** — workers are VMs. `node_cpu_seconds_total{mode="steal"}` is worth one
  glance before blaming anything in Kubernetes.
- **Stream profile choice** — if channels use an `ffmpeg` transcode profile rather than a
  proxy/remux profile, CPU cost per client is an order of magnitude higher and T1-B arrives much
  sooner. Confirm in the Dispatcharr UI.
- **kube-proxy regression** — already fixed 2026-07-22, but re-verify it hasn't returned:
  `kubectl -n kube-system get ds kube-proxy` must return NotFound.

---

## 2. Tonight's test framework

### Ground rules

1. **Disable ArgoCD auto-sync first** (section 0). Nothing below works without this.
2. **Change one variable at a time.** Revert it before the next test.
3. **Same channel, same client, every test.** Pick one channel that reliably buffers and one
   playback device. Write down which.
4. **10 minutes minimum per test.** Buffering is bursty; a 60-second look proves nothing.
5. **Run `dispatcharr-watch.sh` throughout every test** so you have numbers, not impressions.

### Step 0 — Baseline (15 min, do not skip)

```bash
./scripts/dispatcharr-diag.sh              # one-shot snapshot -> ./diag-<timestamp>/
./scripts/dispatcharr-watch.sh 600 | tee baseline.log   # 10 min sampler, while watching TV
```

Record: does buffering hit **one channel or all channels at once**? Is it **periodic or random**?
Are the affected viewers **on the LAN or remote**? These three answers alone eliminate about half
the suspect list.

### Step 0.5 — Confirm all six nodes are identical (5 min, do this first)

Cheap, fully automated, no SSH. Not because residue is likely — you cleaned it — but because the
cleanup was six manual per-host edits, and "did they all land the same way" is a question worth
five minutes before spending the night on the VPN.

```bash
./scripts/node-datapath-compare.sh
```

- **All nodes identical, zero bind/reconcile errors, health server answering correctly** → node
  state is ruled out. Go to Test 1 and don't look back.
- **Any node differs** → read the diff. A missing `CILIUM_*` chain or a missing fwmark lookup
  rule on one node means streams buffer whenever the web pod lands there. Restart that node's
  Cilium agent to let it re-reconcile, re-run the compare, and retest before touching the VPN.

### Test 1 — MTU (highest value once step 0.5 is clean)

The snapshot script prints the MTU chain and runs DF-set probes. Interpret:

- pod `eth0` MTU 1450 and `tun0` MTU 1400 → **confirmed overcommit**, proceed to the fix.
- largest successful DF probe well below `tun0` MTU − 28 → PMTU black hole on the path.

Apply:

```bash
kubectl -n dispatcharr set env deployment/dispatcharr-web -c gluetun WIREGUARD_MTU=1320
kubectl -n dispatcharr rollout status deployment/dispatcharr-web
```

Watch 10 minutes. **If buffering improves, you are done** — commit `WIREGUARD_MTU: "1320"` into
`deployment-web.yaml` and re-enable auto-sync.

Revert if no change: `kubectl -n dispatcharr set env deployment/dispatcharr-web -c gluetun WIREGUARD_MTU-`

### Test 2 — Is it the VPN at all? (definitive)

This is the test that actually answers the question the datacenter swaps were trying to answer.
It does **not** touch the production deployment — it stands up a parallel VPN-free copy:

```bash
./scripts/dispatcharr-novpn-test.sh up      # creates dispatcharr-web-novpn + svc on 10.1.20.204
# point ONE player at http://10.1.20.204:9191, watch 10 min
./scripts/dispatcharr-novpn-test.sh down    # removes it
```

Both pods share the same Postgres, Redis and PVCs, so behaviour is directly comparable.

- **No buffering without the VPN** → the tunnel is the cause (and Test 1 / T2-E / T1-B are where
  to look).
- **Still buffers without the VPN** → the VPN is exonerated entirely. Skip to Tests 3–6.

> Note: the recordings/data PVCs are RWO, so the test pod will schedule onto the same node as the
> production pod. That is intentional — it keeps the node variable constant.

### Test 3 — CPU throttling

```bash
kubectl -n dispatcharr exec deploy/dispatcharr-web -c dispatcharr -- cat /sys/fs/cgroup/cpu.stat
```

Note `nr_throttled` and `throttled_usec`, watch 10 minutes, read again. **Any meaningful increase
while buffering is occurring implicates T1-B.** Then remove the ceiling and retest:

```bash
kubectl -n dispatcharr patch deployment dispatcharr-web --type json \
  -p '[{"op":"remove","path":"/spec/template/spec/containers/0/resources/limits/cpu"}]'
```

If that fixes it, the durable fix is to raise the limit *and* give gluetun its own
`requests.cpu` so the tunnel is never the thing being starved.

### Test 4 — Celery quiesce

```bash
kubectl -n dispatcharr scale deployment dispatcharr-celery --replicas=0
```

Watch 20 minutes (long enough to span a refresh interval). Restore with `--replicas=1`.
Improvement implicates T1-C, and the fix is either scheduling celery elsewhere (which requires
breaking the RWO PVC sharing) or throttling its concurrency.

### Test 5 — Bypass the LoadBalancer

Rules out MetalLB, `externalTrafficPolicy: Local`, and the node uplink in one shot. Connect a
player directly to the pod IP:

```bash
kubectl -n dispatcharr get pod -l component=web -o wide   # note pod IP (10.0.x.x)
```

From a LAN machine that can route to the pod CIDR, or via `kubectl port-forward`:

```bash
kubectl -n dispatcharr port-forward deploy/dispatcharr-web 9191:9191
# play from http://127.0.0.1:9191 on that machine
```

Clean playback here but buffering via `10.1.20.202` implicates T2-G.

### Test 6 — Redis

```bash
kubectl -n dispatcharr exec deploy/dispatcharr-redis -- redis-cli info memory | grep -E 'used_memory_human|maxmemory'
kubectl -n dispatcharr get pod -l component=redis -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}'; echo
```

`used_memory` climbing toward 512Mi, or any nonzero restart count, implicates T1-D. Immediate
mitigation is to raise the limit and set an explicit `maxmemory` + `allkeys-lru` policy so it
evicts instead of dying.

### Test 7 — DNS geolocation

```bash
kubectl -n dispatcharr debug -it deploy/dispatcharr-web --image=nicolaka/netshoot --target=dispatcharr -- bash
# inside:
dig +short <provider-hostname>              # what CoreDNS gives you (your-house edge)
dig +short @9.9.9.9 <provider-hostname>     # via the tunnel's exit
curl -o /dev/null -s -w '%{time_connect}\n' http://<provider-host>/...
```

Materially different answers, or a connect time that looks transcontinental, implicates T2-E.

### Test 8 — Version rollback (only if 1–7 are all clean)

```bash
kubectl -n dispatcharr set image deployment/dispatcharr-web dispatcharr=ghcr.io/dispatcharr/dispatcharr:0.27.2
```

---

## 3. Grafana / PromQL for the session

Your exporter already provides everything needed; no new instrumentation required.

```promql
# Buffering behind real-time — the ground truth signal. <1.0 means falling behind.
dispatcharr_stream_buffering_speed{type="live"}

# Per-client delivery rate; a cliff here at the same instant across clients = server-side cause
dispatcharr_client_current_transfer_rate_bps

# CPU throttling on the web pod (T1-B)
rate(container_cpu_cfs_throttled_seconds_total{namespace="dispatcharr",container="dispatcharr"}[5m])

# gluetun CPU — has no limit, so look for it being squeezed rather than throttled
rate(container_cpu_usage_seconds_total{namespace="dispatcharr",container="gluetun"}[5m])

# Celery CPU — overlay on buffering_speed to test T1-C correlation
rate(container_cpu_usage_seconds_total{namespace="dispatcharr",container="celery"}[5m])

# Restarts anywhere in the namespace (catches the silent Redis OOM, T1-D)
increase(kube_pod_container_status_restarts_total{namespace="dispatcharr"}[1h])

# Node NIC drops on the streaming node (T2-G)
rate(node_network_receive_drop_total[5m]) + rate(node_network_transmit_drop_total[5m])

# Proxmox steal time
rate(node_cpu_seconds_total{mode="steal"}[5m])

# Fallback churn — if the stream index is moving, the upstream really is failing
changes(dispatcharr_stream_index[1h])
```

The **decisive overlay**: put `dispatcharr_stream_buffering_speed` on the same panel as the web
container's throttle rate and celery's CPU. If the dips line up with either, you have your answer
without running a single A/B test.

---

## 4. Recommended durable changes (after diagnosis, not before)

Do not apply these blind — they are what to commit once a test confirms the cause.

| Suspect | Change | File |
|---|---|---|
| T1-0 | `kubeadm init phase addon coredns` instead of `addon all` — **done on this branch**; stops kube-proxy returning on a rebuild | `HomelabAnsible/K8sCluster.yml` |
| T1-0 | Record in CLAUDE.md exactly which nodes had ip rules cleaned on 2026-08-07 and what was removed, so the next incident doesn't have to re-derive it | `CLAUDE.md` |
| T1-A | `WIREGUARD_MTU: "1320"` on gluetun | `deployment-web.yaml` |
| T1-B | `resources.requests` on gluetun (e.g. 200m/128Mi); raise or drop web `limits.cpu` | `deployment-web.yaml` |
| T1-C | Cap celery concurrency; longer M3U/EPG refresh interval | Dispatcharr UI / celery env |
| T1-D | Redis `--maxmemory 400mb --maxmemory-policy allkeys-lru`, raise limit, add probes | `redis.yaml` |
| T1-D | Alert on `kube_pod_container_status_restarts_total{namespace="dispatcharr"}` | `prometheusrule.yaml` |
| T2-E | Point the app container at gluetun's resolver, or enable `DOT` and set `dnsConfig` | `deployment-web.yaml` |
| T3 | Drop `appProtocol: kubernetes.io/ws` | `service.yaml` |

There is also a **monitoring gap worth closing regardless of outcome**: nothing alerts on pod
restarts, CPU throttling, or gluetun tunnel state. `DispatcharrStreamBufferingBehind` fires on the
symptom but nothing tells you which of the causes above produced it.
