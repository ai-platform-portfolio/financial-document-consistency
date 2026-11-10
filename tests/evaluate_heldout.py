"""Evaluate labelled synthetic reports through a disposable document preview."""

import argparse
from collections import Counter
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from pypdf import PdfReader, PdfWriter

from deployed import pdf, request
from extraction import extract_pages
from processing import Claims, compare_claims, narrative_context, validate_claims


KINDS = ("report", "summary")
MAX_CASES = 8
REVIEW_TIMEOUT_SECONDS = 240
CORPUS = Path(__file__).with_name("evaluation_cases.json")


def render(pages):
    writer = PdfWriter()
    for page in pages:
        writer.add_page(PdfReader(BytesIO(pdf(page))).pages[0])
    target = BytesIO()
    writer.write(target)
    return target.getvalue()


def finding_label(finding):
    return (
        finding["check"],
        finding.get("metric", finding.get("source", "-")),
        finding["classification"],
    )


def labels(case):
    return [tuple(row) for row in case["expected"]] + [
        ("narrative", "-", case["narrative"])
    ]


def citations(finding, case):
    """Yield each citation and the hand-labelled spans that could support it."""
    check, key, _ = finding_label(finding)
    if check == "arithmetic":
        gold = [f["evidence"] for f in case["facts"].get(key, [])]
        for quote in finding.get("evidence", []):
            yield key, quote, gold
        return
    for kind in KINDS:
        quote = finding.get(f"{kind}_evidence")
        if quote is None:
            continue
        if check == "narrative":
            page, text = case["locations"][KINDS.index(kind)]
            gold = [{"page": page, "quote": text}]
        else:
            gold = [f["evidence"] for f in case["facts"][kind] if f["metric"] == key]
        yield kind, quote, gold


def score(case, pages, findings):
    expected = Counter(labels(case))
    actual = Counter(map(finding_label, findings))
    evidence = Counter()
    for finding in findings:
        for kind, quote, gold in citations(finding, case):
            evidence["cited"] += 1
            page, span = quote["page"], quote["quote"]
            if 1 <= page <= len(pages[kind]) and span in pages[kind][page - 1]:
                evidence["locatable"] += 1
                if any(item["page"] == page and item["quote"] in span for item in gold):
                    evidence["includes_labelled_span"] += 1
    return {
        "matched": list((expected & actual).elements()),
        "missing": list((expected - actual).elements()),
        "extra": list((actual - expected).elements()),
        "evidence": dict(evidence),
    }


def local_baseline(case, pages):
    claims = [Claims(claims=case["facts"][kind]) for kind in KINDS]
    for kind, extracted in zip(KINDS, claims):
        validate_claims(extracted, pages[kind])
    expected = Counter(tuple(row) for row in case["expected"] if row[0] != "coverage")
    assert Counter(map(finding_label, compare_claims(*claims))) == expected
    visible = {}
    for kind, (page, quote) in zip(KINDS, case["locations"]):
        assert quote in pages[kind][page - 1]
        visible[kind] = any(
            page == window["page"] and quote in window["text"]
            for window in narrative_context(pages[kind])
        )
    return {"case": case["name"], "labelled_claims_passed": True, "context": visible}


def evaluate(base, case, content, pages):
    record = {"case": case["name"], "status": "error", "findings": []}
    try:
        body = {"check_profile_version": "financial-v1"}
        for kind in KINDS:
            status, doc = request(
                base,
                f"/api/documents/{kind}/{uuid4()}",
                content[kind],
                method="PUT",
                content_type="application/pdf",
            )
            if status != 201 or not doc["verified"]:
                raise RuntimeError(f"Upload {kind} failed with HTTP {status}")
            body[f"{kind}_version_id"] = doc["id"]
        status, review = request(base, "/api/reviews", body, key=uuid4().hex)
        if status != 201:
            raise RuntimeError(f"Admission failed with HTTP {status}")
        record["review_id"] = review["id"]
        path = f"/api/reviews/{review['id']}"
        deadline = time.monotonic() + REVIEW_TIMEOUT_SECONDS
        while review["status"] not in {"completed", "failed"}:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Review did not finish before the evaluation deadline"
                )
            time.sleep(2)
            status, review = request(base, path)
            if status != 200:
                raise RuntimeError(f"Review read failed with HTTP {status}")
        record.update(
            status=review["status"],
            findings=review.get("findings") or [],
            error_code=review.get("error_code"),
        )
        status, evidence = request(base, path + "/evidence")
        if status != 200:
            raise RuntimeError(f"Evidence read failed with HTTP {status}")
        record.update(
            run=evidence["run"], usage=evidence["usage"], steps=evidence["steps"]
        )
    except (OSError, KeyError, ValueError, RuntimeError) as error:
        record["error"] = str(error)
    record.update(score(case, pages, record["findings"]))
    return record


def summarize(records):
    counts = Counter()
    evidence = Counter()
    for record in records:
        counts[f"reviews_{record['status']}"] += 1
        counts["output_errors"] += "error" in record
        for kind in ("matched", "missing", "extra"):
            counts[kind] += len(record[kind])
            for _, _, classification in record[kind]:
                counts[f"{kind}_{classification}"] += 1
        evidence.update(record["evidence"])
    true = counts["matched_issue"]
    predicted = true + counts["extra_issue"]
    expected = true + counts["missing_issue"]
    return {
        "counts": dict(counts),
        "issue_precision": true / predicted if predicted else None,
        "issue_recall": true / expected if expected else None,
        "evidence": dict(evidence),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--local", action="store_true")
    mode.add_argument("--base-url")
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.base_url and (
        not args.live_model
        or not args.output
        or not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", args.base_url)
    ):
        parser.error("Use an IPv4 loopback preview tunnel, --live-model and --output")

    cases = json.loads(CORPUS.read_text())["heldout_documents"]
    if not 1 <= len(cases) <= MAX_CASES:
        parser.error(f"Corpus must contain one to {MAX_CASES} pairs")
    inputs = []
    for case in cases:
        content = {kind: render(case["pages"][kind]) for kind in KINDS}
        pages = {kind: extract_pages(content[kind]) for kind in KINDS}
        inputs.append((case, content, pages))
    if args.local:
        print(json.dumps([local_baseline(c, p) for c, _, p in inputs], indent=2))
        return

    status, _ = request(args.base_url, "/api/readyz")
    if status != 200:
        raise SystemExit(f"Preview readiness failed with HTTP {status}")
    records = []
    with args.output.open("x") as output:
        for case, content, pages in inputs:
            record = evaluate(args.base_url, case, content, pages)
            records.append(record)
            print(json.dumps(record), file=output, flush=True)
            print(f"{record['case']}: {record['status']}", file=sys.stderr, flush=True)
    print(json.dumps(summarize(records), indent=2))


if __name__ == "__main__":
    main()
