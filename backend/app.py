"""Register immutable document metadata and review requests; no processing yet."""

from datetime import datetime
import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID, uuid4

from azure.core.exceptions import AzureError
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
import psycopg

from db import connect


app = FastAPI(title="Financial document consistency")


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_type: Literal["report", "summary"]
    storage_ref: str = Field(
        min_length=1, max_length=512, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
    )


class DocumentVersion(DocumentInput):
    id: UUID
    created_at: datetime


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report_version_id: UUID
    summary_version_id: UUID
    check_profile_version: str = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )


class Review(ReviewInput):
    # Response validation filters internal idempotency metadata from SQL rows.
    model_config = ConfigDict(extra="ignore")
    id: UUID
    status: Literal["registered"]
    created_at: datetime


@app.exception_handler(psycopg.Error)
@app.exception_handler(AzureError)
def database_unavailable(request, exc):
    # Driver/credential errors may contain connection details. Return no raw errors.
    return JSONResponse(status_code=503, content={"detail": "Database unavailable"})


@app.get("/api/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/readyz")
def readyz():
    with connect() as connection:
        connection.execute(
            "SELECT id, content_hash, document_type, storage_ref, created_at "
            "FROM public.document_versions LIMIT 0"
        )
        connection.execute(
            "SELECT id, report_version_id, summary_version_id, check_profile_version, "
            "idempotency_key, request_fingerprint, created_at FROM public.reviews LIMIT 0"
        )
    return {"status": "ready"}


@app.post("/api/document-versions", response_model=DocumentVersion, status_code=201)
def register_document(body: DocumentInput):
    with connect() as connection:
        return connection.execute(
            "INSERT INTO public.document_versions "
            "(id, content_hash, document_type, storage_ref) VALUES (%s, %s, %s, %s) "
            "RETURNING *",
            (uuid4(), body.content_hash, body.document_type, body.storage_ref),
        ).fetchone()


@app.post("/api/reviews", response_model=Review, status_code=201)
def register_review(
    body: ReviewInput,
    idempotency_key: Annotated[
        str, Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    ],
):
    if body.report_version_id == body.summary_version_id:
        raise HTTPException(status_code=422, detail="Document versions must be distinct")

    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with connect() as connection:
        documents = connection.execute(
            "SELECT id, document_type FROM public.document_versions WHERE id IN (%s, %s)",
            (body.report_version_id, body.summary_version_id),
        ).fetchall()
        kinds = {row["id"]: row["document_type"] for row in documents}
        if kinds.get(body.report_version_id) != "report" or kinds.get(body.summary_version_id) != "summary":
            raise HTTPException(status_code=422, detail="Expected registered report and summary versions")

        row = connection.execute(
            "INSERT INTO public.reviews "
            "(id, report_version_id, summary_version_id, check_profile_version, "
            "idempotency_key, request_fingerprint) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
            (uuid4(), body.report_version_id, body.summary_version_id,
             body.check_profile_version, idempotency_key, fingerprint),
        ).fetchone()
        if row is None:
            # At READ COMMITTED this new statement sees the concurrent winner
            # after ON CONFLICT has waited for its transaction to commit.
            row = connection.execute(
                "SELECT * FROM public.reviews WHERE idempotency_key = %s", (idempotency_key,)
            ).fetchone()
            if row["request_fingerprint"] != fingerprint:
                raise HTTPException(status_code=409, detail="Idempotency key already used for different inputs")
    return {**row, "status": "registered"}


@app.get("/api/reviews/{review_id}", response_model=Review)
def get_review(review_id: UUID):
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM public.reviews WHERE id = %s", (review_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return {**row, "status": "registered"}
