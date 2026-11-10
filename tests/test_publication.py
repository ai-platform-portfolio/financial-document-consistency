"""Application mapping boundaries and real shared-chart package acceptance."""

from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "release", ROOT / "scripts/write-release-values.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = "a" * 40
REPOSITORY = "ai-platform-portfolio/financial-document-consistency"
IMAGE = "localacrk8s.azurecr.io/portfolio/financial-document-consistency"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.intent = yaml.safe_load((ROOT / "deploy/values.yaml").read_text())
        self.context = {
            "GITHUB_REPOSITORY": REPOSITORY,
            "SOURCE_SHA": SHA,
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REF": "refs/pull/42/merge",
            "PR_NUMBER": "42",
            "GITHUB_RUN_ID": "1234",
            "GITHUB_RUN_NUMBER": "10",
            "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_WORKFLOW_REF": f"{REPOSITORY}/.github/workflows/publish.yml@refs/pull/42/merge",
        }
        self.record = {
            "source": {"repository": REPOSITORY, "revision": SHA},
            "workflow": {
                "runId": "1234",
                "attempt": "2",
                "workflowRef": self.context["GITHUB_WORKFLOW_REF"],
                "event": "pull_request",
            },
            "images": {
                "api": {
                    "tag": f"{IMAGE}:preview-pr-42-{SHA}",
                    "digest": "sha256:" + "b" * 64,
                    "reference": IMAGE + "@sha256:" + "b" * 64,
                }
            },
            "migrations": {
                "repository": "financial-document-consistency",
                "tag": SHA,
                "url": f"atlas://financial-document-consistency?tag={SHA}",
                "atlasSumSha256": hashlib.sha256(
                    (ROOT / "migrations/atlas.sum").read_bytes()
                ).hexdigest(),
            },
        }

    def build(self):
        return release.release_values(self.record, self.intent, self.context)

    def test_maps_only_release_references_without_mutating_intent(self):
        original = deepcopy(self.intent)
        values, destination, version, roles = self.build()
        self.assertEqual(self.intent, original)
        self.assertEqual(
            values["components"]["api"]["image"],
            self.record["images"]["api"]["reference"],
        )
        self.assertEqual(
            values["components"]["worker"]["image"],
            values["components"]["api"]["image"],
        )
        self.assertEqual(values["databases"]["reviews"]["migration"]["tag"], SHA)
        self.assertEqual(values["platform"]["release"]["expectedRevision"], SHA)
        self.assertEqual(values["platform"]["atlas"]["rolePlanRepository"], roles)
        self.assertTrue(destination.endswith(f"/{REPOSITORY}/pr-42"))
        self.assertEqual(version, f"0.10.2-g{SHA}")
        self.assertNotIn("hostname", values)
        self.assertNotIn("namespace", values)

    def test_rejects_mixed_source_image_workflow_and_migration(self):
        original = deepcopy(self.record)
        mutations = [
            lambda r: r["source"].update(revision="c" * 40),
            lambda r: r["source"].update(repository="other/application"),
            lambda r: r["workflow"].update(attempt="1"),
            lambda r: r["workflow"].update(runId="999"),
            lambda r: r["images"]["api"].update(digest=""),
            lambda r: r["images"]["api"].update(
                reference=IMAGE + "@sha256:" + "c" * 64
            ),
            lambda r: r["images"]["api"].update(tag=IMAGE + ":other-source"),
            lambda r: r["migrations"].update(tag="c" * 40),
            lambda r: r["migrations"].update(atlasSumSha256="c" * 64),
            lambda r: r["migrations"].update(repository="other-migrations"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.record = deepcopy(original)
                mutate(self.record)
                self.build()

    def test_main_and_dispatch_have_a_separate_release_destination(self):
        for event in ["push", "workflow_dispatch"]:
            self.context.update(
                GITHUB_EVENT_NAME=event,
                GITHUB_REF="refs/heads/main",
                GITHUB_WORKFLOW_REF=f"{REPOSITORY}/.github/workflows/publish.yml@refs/heads/main",
            )
            self.record["workflow"].update(
                event=event, workflowRef=self.context["GITHUB_WORKFLOW_REF"]
            )
            self.record["images"]["api"]["tag"] = f"{IMAGE}:commit-{SHA}"
            self.assertTrue(self.build()[1].endswith("/main"))

    def test_invalid_event_run_or_workflow_cannot_select_a_release(self):
        original = self.context.copy()
        for change in [
            {"PR_NUMBER": "0"},
            {"GITHUB_RUN_NUMBER": "0"},
            {"GITHUB_RUN_ATTEMPT": "bad"},
            {"GITHUB_EVENT_NAME": "pull_request_target"},
            {"GITHUB_REF": "refs/heads/feature"},
            {"GITHUB_REPOSITORY": "../application"},
            {"GITHUB_WORKFLOW_REF": "other-workflow"},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.context = {**original, **change}
                self.build()

    def test_role_repository_is_stable_across_prs_and_separate_across_repositories(
        self,
    ):
        roles = self.build()[3]
        self.context.update(
            PR_NUMBER="43",
            GITHUB_REF="refs/pull/43/merge",
            GITHUB_WORKFLOW_REF=f"{REPOSITORY}/.github/workflows/publish.yml@refs/pull/43/merge",
        )
        self.record["workflow"]["workflowRef"] = self.context["GITHUB_WORKFLOW_REF"]
        self.record["images"]["api"]["tag"] = f"{IMAGE}:preview-pr-43-{SHA}"
        self.assertEqual(self.build()[3], roles)
        other = "another-org/financial-document-consistency"
        self.context.update(
            GITHUB_REPOSITORY=other,
            GITHUB_WORKFLOW_REF=f"{other}/.github/workflows/publish.yml@refs/pull/43/merge",
        )
        self.record["source"]["repository"] = other
        self.record["workflow"]["workflowRef"] = self.context["GITHUB_WORKFLOW_REF"]
        self.assertNotEqual(self.build()[3], roles)

    def test_packaged_values_render_the_expected_image_migration_and_no_ingress(self):
        chart_source = Path(os.environ["APPLICATION_CHART"])
        helm = os.environ.get("HELM_BIN", "helm")
        values, _, version, _ = self.build()
        with tempfile.TemporaryDirectory(prefix="document-release-") as directory:
            tmp = Path(directory)
            chart = tmp / "chart"
            shutil.copytree(
                chart_source,
                chart,
                ignore=shutil.ignore_patterns(".git", ".github", "__pycache__"),
            )
            (chart / "release-values.yaml").write_text(yaml.safe_dump(values))
            subprocess.run(
                [
                    helm,
                    "package",
                    str(chart),
                    "--version",
                    version,
                    "--app-version",
                    SHA,
                    "--destination",
                    str(tmp),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            package = next(tmp.glob("application-*.tgz"))
            with tarfile.open(package) as archive:
                metadata = yaml.safe_load(
                    archive.extractfile("application/Chart.yaml").read()
                )
                packaged_values = yaml.safe_load(
                    archive.extractfile("application/release-values.yaml").read()
                )
            self.assertEqual(metadata["appVersion"], SHA)
            self.assertEqual(metadata["version"], version)
            self.assertEqual(packaged_values, values)
            platform = {
                "azure": {
                    "subscriptionId": "00000000-0000-0000-0000-000000000001",
                    "tenantId": "00000000-0000-0000-0000-000000000002",
                    "location": "uksouth",
                    "identityResourceGroup": "example-rg",
                    "oidcIssuerUrl": "https://issuer.example.test/",
                    "storageAccount": {
                        "name": "exampleblob",
                        "resourceGroup": "example-rg",
                    },
                    "blobRoleId": "/subscriptions/00000000-0000-0000-0000-000000000001/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000003",
                },
                "models": {
                    "chat": {
                        "endpoint": "https://model.example.test/openai/v1/",
                        "deployment": "chat",
                        "resourceGroup": "example-rg",
                        "account": "example-foundry",
                        "project": "management",
                    }
                },
            }
            result = subprocess.check_output(
                [
                    helm,
                    "template",
                    "internal-api",
                    str(package),
                    "-f",
                    str(chart / "release-values.yaml"),
                    "--set-json",
                    "platform.azure=" + json.dumps(platform["azure"]),
                    "--set-json",
                    "platform.models=" + json.dumps(platform["models"]),
                ],
                text=True,
            )
            docs = [d for d in yaml.safe_load_all(result) if d]
            self.assertFalse({d["kind"] for d in docs} & {"ListenerSet", "HTTPRoute"})
            for certificate in (d for d in docs if d["kind"] == "Certificate"):
                self.assertIn("client auth", certificate["spec"]["usages"])
            worker = next(
                d
                for d in docs
                if d["kind"] == "Deployment" and d["metadata"]["name"] == "worker"
            )
            self.assertEqual(
                worker["spec"]["template"]["spec"]["containers"][0]["command"],
                ["python", "worker.py"],
            )
            self.assertFalse(
                any(
                    d["kind"] == "Service" and d["metadata"]["name"] == "worker"
                    for d in docs
                )
            )
            deployment = next(
                d
                for d in docs
                if d["kind"] == "Deployment" and d["metadata"]["name"] == "api"
            )
            self.assertEqual(
                deployment["spec"]["template"]["spec"]["containers"][0]["image"],
                self.record["images"]["api"]["reference"],
            )
            self.assertTrue(
                any(
                    d["kind"] == "AtlasMigration"
                    and d["spec"].get("dir", {}).get("remote", {}).get("tag") == SHA
                    for d in docs
                )
            )


if __name__ == "__main__":
    unittest.main()
