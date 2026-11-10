"""Deterministic evidence/checking evaluations and real bounded PDF extraction."""

import json
import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql://unconfigured/unused")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from extraction import extract_pages
from processing import (
    Claims,
    Evidence,
    compare_claims,
    validate_claims,
    evidence_valid,
)


ROOT = Path(__file__).resolve().parents[1]


def pdf(text: str) -> bytes:
    """A small independently generated, text-based PDF; no third-party fixtures."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 20 750 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    start = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(result)


def claim(metric="revenue", value="100", **overrides):
    return {
        "metric": metric,
        "value": value,
        "entity": "Example Ltd",
        "period": "2025",
        "currency": "GBP",
        "scale": "units",
        "basis": "statutory",
        "evidence": {"page": 1, "quote": f"{metric} {value}"},
        **overrides,
    }


class ProcessingTests(unittest.TestCase):
    def test_labelled_numerical_cases(self):
        cases = json.loads((ROOT / "tests/evaluation_cases.json").read_text())[
            "numeric"
        ]
        for case in cases:
            with self.subTest(case=case["name"]):
                report = Claims(claims=[claim(**x) for x in case["report"]])
                summary = Claims(claims=[claim(**x) for x in case["summary"]])
                findings = compare_claims(report, summary)
                self.assertEqual(
                    [x["classification"] for x in findings], case["expected"]
                )

    def test_evidence_must_have_real_page_quote_and_value(self):
        valid = Claims(claims=[claim()])
        validate_claims(valid, ["revenue 100"])
        for pages in [["revenue 101"], []]:
            with self.assertRaises(ValueError):
                validate_claims(valid, pages)
        bad = Claims(
            claims=[claim(value="999", evidence={"page": 1, "quote": "revenue 100"})]
        )
        with self.assertRaises(ValueError):
            validate_claims(bad, ["revenue 100"])
        self.assertFalse(
            evidence_valid(Evidence(page=2, quote="revenue 100"), ["revenue 100"])
        )
        with self.assertRaises(ValueError):
            Claims.model_validate({"claims": [], "injected": "execute code"})

    def test_native_sdk_structured_output_contract(self):
        import httpx2
        from openai import OpenAI

        captured = []

        def handle(request):
            captured.append(json.loads(request.content))
            return httpx2.Response(
                200,
                json={
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "fixture-v1",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": '{"claims":[]}',
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )

        with OpenAI(
            base_url="https://fixture.invalid/v1/",
            api_key=lambda: "fixture",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handle)),
        ) as client:
            response = client.chat.completions.create(
                model="fixture",
                messages=[{"role": "user", "content": "fixture"}],
                max_completion_tokens=3000,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "Claims",
                        "strict": True,
                        "schema": Claims.model_json_schema(),
                    },
                },
            )
        self.assertEqual(
            Claims.model_validate_json(response.choices[0].message.content).claims, []
        )
        self.assertTrue(captured[0]["response_format"]["json_schema"]["strict"])
        self.assertEqual(response.usage.prompt_tokens, 10)

    def test_pdf_extraction_and_invalid_input(self):
        self.assertEqual(
            extract_pages(pdf("Example Ltd revenue 100 GBP")),
            ["Example Ltd revenue 100 GBP"],
        )
        for content in [b"not a pdf", b"%PDF-broken", pdf("")]:
            with self.assertRaises(ValueError):
                extract_pages(content)

    def test_arithmetic_uses_normalized_decimal_values(self):
        report = Claims(
            claims=[
                claim(metric=m, value=v)
                for m, v in [("revenue", "0.3"), ("cost", "0.1"), ("profit", "0.2")]
            ]
        )
        results = compare_claims(report, Claims(claims=[]))
        self.assertEqual(results[0]["classification"], "consistent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
