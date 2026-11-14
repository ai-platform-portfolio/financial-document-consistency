"""Functional API acceptance through an explicitly selected loopback preview tunnel."""

import argparse
import json
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from pathlib import Path
from decimal import Decimal

from test_processing import pdf


def request(
    base, path, body=None, *, key=None, method=None, content_type="application/json"
):
    data = (
        json.dumps(body).encode()
        if body is not None and content_type == "application/json"
        else body
    )
    headers = {"Content-Type": content_type}
    if key:
        headers["Idempotency-Key"] = key
    req = Request(base + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def run(base, case=None):
    case = (
        case
        or json.loads((Path(__file__).parent / "evaluation_cases.json").read_text())[
            "documents"
        ][0]
    )
    assert request(base, "/api/healthz")[0] == 200
    assert request(base, "/api/readyz")[0] == 200
    docs = []
    for kind in ["report", "summary"]:
        identity = str(uuid4())
        content = pdf(case[kind])
        status, doc = request(
            base,
            f"/api/documents/{kind}/{identity}",
            content,
            method="PUT",
            content_type="application/pdf",
        )
        assert status == 201 and doc["verified"], (status, doc)
        assert request(
            base,
            f"/api/documents/{kind}/{identity}",
            content,
            method="PUT",
            content_type="application/pdf",
        ) == (status, doc)
        docs.append(doc)
    body = {
        "report_version_id": docs[0]["id"],
        "summary_version_id": docs[1]["id"],
        "check_profile_version": "financial-v1",
    }
    key = uuid4().hex
    status, review = request(base, "/api/reviews", body, key=key)
    assert status == 201 and review["status"] in ("queued", "running", "completed"), (
        status,
        review,
    )
    retry = request(base, "/api/reviews", body, key=key)
    assert retry[0] == 201 and retry[1]["id"] == review["id"]
    assert (
        request(
            base, "/api/reviews", {**body, "check_profile_version": "other"}, key=key
        )[0]
        == 409
    )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        status, result = request(base, "/api/reviews/" + review["id"])
        assert status == 200
        if result["status"] in ("completed", "failed"):
            break
        time.sleep(2)
    assert result["status"] == "completed", result
    numeric = [f for f in result["findings"] if f["check"] == "numeric"]
    assert any(f["classification"] == case["numeric"] for f in numeric), result
    narrative = [f for f in result["findings"] if f["check"] == "narrative"]
    assert (
        len(narrative) == 1 and narrative[0]["classification"] == case["narrative"]
    ), result
    status, evidence = request(base, "/api/reviews/" + review["id"] + "/evidence")
    assert status == 200 and len(evidence["steps"]) == 3
    assert evidence["run"]["model_calls"] <= 12
    true_positive = false_positive = false_negative = 0
    for step in evidence["steps"]:
        if step["step"] not in {"report", "summary"}:
            continue
        expected = case[step["step"] + "_revenue"]
        extracted = [
            str(
                Decimal(c["value"])
                * {"units": 1, "thousands": 1000, "millions": 1000000}[c["scale"]]
            )
            for c in step["output"]["claims"]
            if c["metric"] == "revenue"
        ]
        matches = (
            sum(Decimal(v) == Decimal(expected) for v in extracted)
            if expected is not None
            else 0
        )
        true_positive += min(matches, 1)
        false_positive += len(extracted) - min(matches, 1)
        false_negative += int(expected is not None and not matches)
    assert false_positive == false_negative == 0, {
        "case": case["name"],
        "extraction_false_positive": false_positive,
        "extraction_false_negative": false_negative,
    }
    return {
        "case": case["name"],
        "split": case["split"],
        "revenue_extraction_tp": true_positive,
        "revenue_extraction_fp": false_positive,
        "revenue_extraction_fn": false_negative,
        "numeric_label": case["numeric"],
        "narrative_label": case["narrative"],
        "review": review["id"],
        "status": result["status"],
        "checks": len(result["findings"]),
        "model": evidence["run"]["actual_model"],
        "config": evidence["run"]["config_hash"],
        "usage": evidence["usage"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--live-model",
        action="store_true",
        help="Acknowledge this submits a bounded real inference review",
    )
    parser.add_argument(
        "--cases",
        type=int,
        choices=[1, 2, 3],
        default=1,
        help="Bound the number of labelled real-model pairs",
    )
    args = parser.parse_args()
    if not args.live_model or urlparse(args.base_url).hostname not in {
        "127.0.0.1",
        "localhost",
    }:
        parser.error("Use an explicit loopback preview tunnel and --live-model")
    cases = json.loads((Path(__file__).parent / "evaluation_cases.json").read_text())[
        "documents"
    ][: args.cases]
    print(
        json.dumps([run(args.base_url.rstrip("/"), case) for case in cases], indent=2)
    )
