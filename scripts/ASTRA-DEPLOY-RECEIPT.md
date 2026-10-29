# Astra post-deployment receipt

Run `scripts/astra_deploy_receipt.py` **after** a GitOps merge/pod rollout. It is read-only and does not schedule a watcher, update Kubernetes, or create a production test conversation.

It checks the exact `sha-<40-character commit>` image pinned in `workloads/hermes/deployment.yaml` against the Astra source commit and successful **publishing** GitHub Actions run for that commit; it checks the Hermes ArgoCD Application's revision against HomelabArgoCD `main`, Synced/Healthy state, ready/available Deployment, the pod's observed Astra image tag and digest, and Astra's `/api/health` with `state_db.ok=true`. GitHub, ArgoCD, kube-state-metrics, and the app are separate observations. The registry manifest digest is *not* independently compared with the pod digest. A green `/api/health` is *not* proof that authenticated chat works.

Run from a pod or host that resolves cluster service DNS and has `gh`, `python3` and an installation token scoped to both repositories. In the Hermes WebUI/Astra lane, mint the Voyager token without printing it:

```bash
export GH_TOKEN="$(bash /workspace/github-voyager/voyager-token.sh 153007715)"
python3 scripts/astra_deploy_receipt.py --output /workspace/astra-deploy-receipt.json
```

The receipt contains no credential. Exit codes: `2` = a check failed / evidence could not be gathered; `3` = observed checks passed but the authenticated user flow remains unverified; `0` is reserved for a fully verified receipt (not produced by this read-only first version). Do not treat `3` as a green CI job. To complete the human gate, log into Astra independently and check a **clearly labelled harmless test chat** through the UI, then confirm the saved turn from the UI and delete the test chat. Do not paste cookies/passwords into receipt JSON or treat a login page as proof of a working session.

Run tests: `python3 -m unittest discover -s tests -v` from repository root. This tool is intentionally on-demand, not a periodic monitor: invoke it once per deployment and retain the JSON for that deployment. A post-merge trigger or an authenticated browser harness would be a separate decision; neither is silently implied by this script.
