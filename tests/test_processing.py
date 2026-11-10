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
    Narrative,
    UnsupportedClaimEvidence,
    narrative_context,
    narrative_inputs,
    validate_narrative,
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

    def test_numeric_quote_cannot_split_a_source_token(self):
        for page, quote, value in [
            ("Revenue GBP 82,000.", "Revenue GBP 82", "82"),
            ("Revenue GBP 82.50.", "Revenue GBP 82", "82"),
            ("Change was -42.", "42", "42"),
            ("Revenue GBP 182.", "82", "82"),
        ]:
            with self.subTest(page=page):
                result = Claims(
                    claims=[claim(value=value, evidence={"page": 1, "quote": quote})]
                )
                with self.assertRaisesRegex(
                    UnsupportedClaimEvidence, "value_not_in_quote"
                ):
                    validate_claims(result, [page])
        for value, quote in [("82000", "GBP 82,000"), ("-42", "−42")]:
            result = Claims(
                claims=[claim(value=value, evidence={"page": 1, "quote": quote})]
            )
            validate_claims(result, [f"Source: {quote}."])

    def test_narrative_context_keeps_margin_synonym_on_its_page(self):
        excerpt = "Gross profit as a share of sales climbed from 18% to 22%."
        for padding in ["", "x" * 8000]:
            pages = ["The following page explains the margin. " + padding, excerpt]
            windows = narrative_context(pages)
            self.assertLessEqual(
                len(json.dumps(windows, ensure_ascii=False).encode()), 8000
            )
            self.assertTrue(
                any(w["page"] == 2 and excerpt in w["text"] for w in windows)
            )

    def test_narrative_inputs_keep_fiscal_header_context_separate(self):
        pages = {
            "report": [
                "FY2025 annual report.",
                "Gross profit as a share of sales rose.",
            ],
            "summary": ["FY2025 summary. Gross margin rose."],
        }
        inputs = narrative_inputs(pages)
        self.assertEqual(inputs["fiscal_years"], {"report": "2025", "summary": "2025"})
        self.assertEqual(inputs["report"][1], {"page": 2, "text": pages["report"][1]})
        for text, expected in [
            ("FY2024 summary. Gross margin rose.", "2024"),
            ("FY2024 and FY2025 summary. Gross margin rose.", None),
            ("Q1 FY2025 summary. Gross margin rose.", None),
        ]:
            with self.subTest(text=text):
                result = narrative_inputs({**pages, "summary": [text]})
                self.assertEqual(result["fiscal_years"]["summary"], expected)

    def test_narrative_requires_comparable_explicit_periods(self):
        narrative = Narrative(
            classification="consistent",
            explanation="Both documents say margin was stable",
            report_evidence=Evidence(page=1, quote="Margin was stable."),
            summary_evidence=Evidence(page=1, quote="Margin was stable."),
        )
        pages = {
            "report": ["FY2025 report. Margin was stable."],
            "summary": ["Year 2025 summary. Margin was stable."],
        }
        self.assertEqual(
            validate_narrative(narrative, pages).classification, "consistent"
        )
        for summary in [
            "FY2024 summary. Margin was stable.",
            "FY2024 and FY2025 summary. Margin was stable.",
            "Q1 FY2025 summary. Margin was stable.",
            "FY2025/26 summary. Margin was stable.",
            "Margin was stable.",
        ]:
            with self.subTest(summary=summary):
                pages["summary"] = [summary]
                result = validate_narrative(narrative, pages)
                self.assertEqual(result.classification, "unresolved")
                self.assertIn("fiscal period", result.explanation)
        with self.assertRaisesRegex(ValueError, "Unsupported narrative evidence"):
            validate_narrative(narrative, {**pages, "summary": ["Unrelated text"]})

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
