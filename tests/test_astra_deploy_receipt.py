import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from astra_deploy_receipt import evaluate, image_sha

SHA = "d56b6e3088d7c41e42d4d69e21122e7173e4093e"
IMAGE = "ghcr.io/slamanna212/astra:sha-" + SHA


def inputs():
    return {
        "gitops_head": "f20f2435e960aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "manifest_image": IMAGE,
        "argo": {"name": "hermes", "sync": "Synced", "health": "Healthy", "revision": "f20f2435e960"},
        "workload": {"desired": 1, "ready": 1, "available": 1, "restarts": 0},
        "container": {"image": IMAGE, "image_id": "ghcr.io/slamanna212/astra@sha256:" + "a" * 64, "pod": "hermes-123"},
        "commit": {"sha": SHA},
        "build_runs": [{"head_sha": SHA, "event": "push", "status": "completed", "conclusion": "success", "html_url": "https://github.com/slamanna212/Astra/actions/runs/1"}],
        "health": {"status": "ok", "state_db": {"ok": True}},
    }


class ReceiptTests(unittest.TestCase):
    def test_matching_chain_has_no_failed_checks_but_auth_is_unverified(self):
        result = evaluate(inputs())
        self.assertEqual(result["verdict"], "partial")
        self.assertEqual(result["source_sha"], SHA)
        self.assertEqual([c["state"] for c in result["checks"]].count("fail"), 0)
        self.assertEqual(result["checks"][-1]["state"], "unverified")

    def test_image_tag_comes_from_manifest_not_latest_source_head(self):
        data = inputs()
        data["commit"] = {"sha": SHA}
        self.assertEqual(image_sha(data["manifest_image"]), SHA)
        self.assertEqual(evaluate(data)["source_sha"], SHA)

    def test_wrong_runtime_image_fails_even_if_pod_ready(self):
        data = inputs()
        data["container"]["image"] = "ghcr.io/slamanna212/astra:latest"
        result = evaluate(data)
        self.assertEqual(result["verdict"], "fail")
        self.assertIn("runtime_image", [c["name"] for c in result["checks"] if c["state"] == "fail"])

    def test_stale_argocd_revision_fails_even_when_synced(self):
        data = inputs()
        data["argo"]["revision"] = "deadbeef"
        self.assertEqual(evaluate(data)["verdict"], "fail")

    def test_successful_pr_build_is_not_a_published_image(self):
        data = inputs()
        data["build_runs"][0]["event"] = "pull_request"
        self.assertIn("image_build", [c["name"] for c in evaluate(data)["checks"] if c["state"] == "fail"])

    def test_no_container_metric_is_not_a_pass(self):
        data = inputs()
        data["container"] = None
        self.assertEqual(evaluate(data)["verdict"], "fail")

    def test_health_200_with_db_failure_is_fail(self):
        data = inputs()
        data["health"]["state_db"]["ok"] = False
        self.assertIn("health", [c["name"] for c in evaluate(data)["checks"] if c["state"] == "fail"])


if __name__ == "__main__":
    unittest.main()
