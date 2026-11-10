"""Map this application's verified artifact set to the shared chart's values."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

import yaml


def release_values(record, intent, context):
    repository, revision = context["GITHUB_REPOSITORY"], context["SOURCE_SHA"]
    event, ref = context["GITHUB_EVENT_NAME"], context["GITHUB_REF"]
    run, attempt = context["GITHUB_RUN_NUMBER"], context["GITHUB_RUN_ATTEMPT"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected an owner/repository identity")
    if not re.fullmatch(r"[a-f0-9]{40}", revision) or record["source"] != {
        "repository": repository,
        "revision": revision,
    }:
        raise ValueError("Artifact source does not match this publication")
    if any(not re.fullmatch(r"[1-9][0-9]*", number) for number in (run, attempt)):
        raise ValueError("Positive run number and attempt required")
    if record["workflow"] != {
        "runId": context["GITHUB_RUN_ID"],
        "attempt": attempt,
        "workflowRef": context["GITHUB_WORKFLOW_REF"],
        "event": event,
    }:
        raise ValueError("Artifact workflow attempt does not match this publication")

    if event == "pull_request":
        pr = context.get("PR_NUMBER", "")
        if not re.fullmatch(r"[1-9][0-9]{0,9}", pr) or ref != f"refs/pull/{pr}/merge":
            raise ValueError("Expected a PR identity")
        scope = f"pr-{pr}"
        tag = f"preview-pr-{pr}-{revision}"
    elif event in {"push", "workflow_dispatch"} and ref == "refs/heads/main":
        scope, tag = "main", f"commit-{revision}"
    else:
        raise ValueError("Only PR and main releases are supported")

    if (
        context["GITHUB_WORKFLOW_REF"]
        != f"{repository}/.github/workflows/publish.yml@{ref}"
    ):
        raise ValueError("Publication workflow reference mismatch")
    image = record["images"]["api"]
    image_repository = "localacrk8s.azurecr.io/portfolio/financial-document-consistency"
    if (
        not re.fullmatch(r"sha256:[a-f0-9]{64}", image["digest"])
        or image["reference"] != f"{image_repository}@{image['digest']}"
        or image["tag"] != f"{image_repository}:{tag}"
    ):
        raise ValueError("Expected a matching digest-pinned API image")
    migration = record["migrations"]
    checksum = hashlib.sha256(Path("migrations/atlas.sum").read_bytes()).hexdigest()
    if migration != {
        "repository": "financial-document-consistency",
        "tag": revision,
        "url": f"atlas://financial-document-consistency?tag={revision}",
        "atlasSumSha256": checksum,
    }:
        raise ValueError("Migration source or checksum mismatch")

    values = deepcopy(intent)
    values["components"]["api"]["image"] = image["reference"]
    values["components"]["worker"]["image"] = image["reference"]
    values["databases"]["reviews"]["migration"] = {
        "repository": migration["repository"],
        "tag": revision,
    }
    canonical = repository.lower()
    role_repository = f"financial-document-consistency-{hashlib.sha256(canonical.encode()).hexdigest()[:12]}-roles"
    platform = values.setdefault("platform", {})
    platform.setdefault("atlas", {})["rolePlanRepository"] = role_repository
    platform.setdefault("release", {})["expectedRevision"] = revision
    destination = f"oci://localacrk8s.azurecr.io/releases/{canonical}/{scope}"
    return values, destination, f"0.{run}.{attempt}-g{revision}", role_repository


if __name__ == "__main__":
    values, destination, version, roles = release_values(
        json.loads(Path("artifacts.json").read_text()),
        yaml.safe_load(Path("deploy/values.yaml").read_text()),
        os.environ,
    )
    output = Path(os.environ["RUNNER_TEMP"]) / "release-values.yaml"
    output.write_text(yaml.safe_dump(values, sort_keys=False))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(
            f"values={output}\ndestination={destination}\nversion={version}\nrole-repository={roles}\n"
        )
