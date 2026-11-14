"""Submit four synthetic reviews to an explicitly selected disposable preview."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import statistics
import time
from uuid import uuid4

from deployed import pdf, request


REVIEWS = 4
PHASE_SECONDS = 300
DOCUMENTS = {
    "report": "Example Ltd. Year 2025. Statutory revenue 100 GBP. Margin declined.",
    "summary": "Example Ltd. Year 2025. Statutory revenue 110 GBP. Margin improved.",
}


def run(base, workers, pair_file):
    started = time.monotonic()
    reads = []

    def call(path, *args, expected=200, **kwargs):
        # The shared HTTP helper has a 30-second timeout.
        if time.monotonic() - started >= PHASE_SECONDS - 30:
            raise TimeoutError("Phase deadline approaching")
        before = time.monotonic()
        status, result = request(base, path, *args, **kwargs)
        if status != expected:
            raise RuntimeError(f"HTTP {status} for {path}")
        if path.startswith("/api/reviews/") and not path.endswith("/evidence"):
            reads.append(round((time.monotonic() - before) * 1000, 3))
        return result

    result = {"workers": workers, "reviews": []}
    try:
        call("/api/readyz")
        if workers == 1:
            with pair_file.open("x") as pair:
                body = {"check_profile_version": "financial-v1"}
                for kind, content in DOCUMENTS.items():
                    doc = call(
                        f"/api/documents/{kind}/{uuid4()}",
                        pdf(content),
                        method="PUT",
                        content_type="application/pdf",
                        expected=201,
                    )
                    if not doc["verified"]:
                        raise RuntimeError("Preview did not verify the upload")
                    body[f"{kind}_version_id"] = doc["id"]
                json.dump(body, pair)
        else:
            body = json.loads(pair_file.read_text())

        burst_start = time.monotonic()

        def submit(_):
            try:
                admitted = call("/api/reviews", body, key=uuid4().hex, expected=201)
                return {"id": admitted["id"], "status": admitted["status"]}
            except (RuntimeError, TimeoutError, OSError) as error:
                return {"status": "admission_error", "error": str(error)}

        with ThreadPoolExecutor(max_workers=REVIEWS) as executor:
            result["reviews"] = list(executor.map(submit, range(REVIEWS)))
        pending = [row for row in result["reviews"] if "id" in row]
        while pending:
            for row in pending[:]:
                review = call(f"/api/reviews/{row['id']}")
                row["status"] = review["status"]
                if review["status"] in {"completed", "failed"}:
                    row["observed_completion_seconds"] = round(
                        time.monotonic() - burst_start, 3
                    )
                    row["error_code"] = review.get("error_code")
                    pending.remove(row)
            if pending:
                time.sleep(1)

        result["burst_seconds"] = round(time.monotonic() - burst_start, 3)
        result["completed_reviews_per_second"] = round(
            sum(row["status"] == "completed" for row in result["reviews"])
            / result["burst_seconds"],
            3,
        )
        for row in result["reviews"]:
            if "id" not in row:
                continue
            evidence = call(f"/api/reviews/{row['id']}/evidence")
            row["run"] = evidence["run"]
            row["usage"] = evidence["usage"]
    except (RuntimeError, TimeoutError, OSError, ValueError, KeyError) as error:
        result["error"] = str(error)

    if reads:
        result["api_read_ms"] = {
            "samples": len(reads),
            "min": min(reads),
            "median": statistics.median(reads),
            "max": max(reads),
        }
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--workers", choices=(1, 2), type=int, required=True)
    parser.add_argument("--pair-file", type=Path, required=True)
    parser.add_argument("--live-model", action="store_true")
    args = parser.parse_args()
    if not args.live_model or not re.fullmatch(
        r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", args.base_url
    ):
        parser.error("Use an explicit IPv4 loopback preview tunnel and --live-model")

    result = run(args.base_url, args.workers, args.pair_file)
    print(json.dumps(result, indent=2))
    raise SystemExit(
        1
        if "error" in result
        or len(result["reviews"]) != REVIEWS
        or any(row["status"] != "completed" for row in result["reviews"])
        else 0
    )
