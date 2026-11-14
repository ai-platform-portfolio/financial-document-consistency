"""Bounded page-text extraction; OCR and layout reconstruction are not inferred."""

import json
import subprocess
import sys


MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 20
MAX_TEXT = 24000


def extract_pages(content: bytes) -> list[str]:
    if not content.startswith(b"%PDF-") or len(content) > MAX_BYTES:
        raise ValueError("Expected a PDF of at most 5 MiB")
    try:
        result = subprocess.run(
            [sys.executable, __file__], input=content, capture_output=True, timeout=12
        )
    except subprocess.TimeoutExpired:
        raise ValueError("PDF extraction exceeded resource limits") from None
    if result.returncode:
        raise ValueError("Unsupported, encrypted, scanned or oversized PDF")
    pages = json.loads(result.stdout)
    if len(json.dumps({"pages": pages}, ensure_ascii=False).encode()) > 32000:
        raise ValueError("Extracted text exceeds inference input limit")
    return pages


def _extract():
    # A malformed compressed PDF must not consume the API's address space/CPU.
    import io
    import resource

    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    from pypdf import PdfReader

    data = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Size limit")
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
        raise ValueError("Unsupported PDF")
    pages = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if not text or len(text) + sum(map(len, pages)) > MAX_TEXT:
            raise ValueError("Empty or oversized text")
        pages.append(text)
    print(json.dumps(pages))


if __name__ == "__main__":
    _extract()
