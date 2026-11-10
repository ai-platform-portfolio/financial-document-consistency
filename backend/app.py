"""Register immutable document metadata and review requests; no processing yet."""

from datetime import datetime
import hashlib
import json
import asyncio

import procrastinate
from typing import Annotated, Literal
from uuid import UUID, uuid4

from azure.core.exceptions import AzureError
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
import psycopg

from db import connect, DATABASE_URL
from extraction import extract_pages, MAX_BYTES
from processing import CONFIG, PROFILE
from storage import BlobStore
from telemetry import configure, tracer
from psycopg_pool import PoolTimeout


configure()
app = FastAPI(title="Financial document consistency")
uploads = asyncio.Semaphore(1)
producer = procrastinate.App(
    connector=procrastinate.SyncPsycopgConnector(conninfo=DATABASE_URL)
)


@app.middleware("http")
async def trace_request(request, call_next):
    with tracer.start_as_current_span(
        "api.request", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("http.request.method", request.method)
        response = await call_next(request)
        span.set_attribute("http.response.status_code", response.status_code)
        route = request.scope.get("route")
        if route:
            span.set_attribute("http.route", route.path)
        return response


class DocumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_type: Literal["report", "summary"]
    storage_ref: str = Field(
        min_length=1, max_length=512, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
    )


class DocumentVersion(DocumentInput):
    verified: bool = False
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
    status: Literal["registered", "queued", "running", "completed", "failed"]
    findings: list[dict] | None = None
    error_code: str | None = None
    created_at: datetime


@app.exception_handler(PoolTimeout)
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
        connection.execute("SELECT status,config_hash FROM public.review_runs LIMIT 0")
        connection.execute("SELECT id FROM public.procrastinate_jobs LIMIT 0")
        connection.execute("SELECT id FROM public.upload_intents LIMIT 0")
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
        str,
        Header(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
    ],
):
    if body.report_version_id == body.summary_version_id:
        raise HTTPException(
            status_code=422, detail="Document versions must be distinct"
        )

    fingerprint = hashlib.sha256(
        json.dumps(
            body.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    with connect() as connection:
        documents = connection.execute(
            "SELECT id, document_type FROM public.document_versions WHERE id IN (%s, %s)",
            (body.report_version_id, body.summary_version_id),
        ).fetchall()
        kinds = {row["id"]: row["document_type"] for row in documents}
        if (
            kinds.get(body.report_version_id) != "report"
            or kinds.get(body.summary_version_id) != "summary"
        ):
            raise HTTPException(
                status_code=422,
                detail="Expected registered report and summary versions",
            )

        row = connection.execute(
            "INSERT INTO public.reviews "
            "(id, report_version_id, summary_version_id, check_profile_version, "
            "idempotency_key, request_fingerprint) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (idempotency_key) DO NOTHING RETURNING *",
            (
                uuid4(),
                body.report_version_id,
                body.summary_version_id,
                body.check_profile_version,
                idempotency_key,
                fingerprint,
            ),
        ).fetchone()
        if row is None:
            # At READ COMMITTED this new statement sees the concurrent winner
            # after ON CONFLICT has waited for its transaction to commit.
            row = connection.execute(
                "SELECT * FROM public.reviews WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if row["request_fingerprint"] != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key already used for different inputs",
                )
        verified = connection.execute(
            "SELECT count(*) AS n FROM public.document_versions WHERE id IN (%s,%s) AND verified",
            (body.report_version_id, body.summary_version_id),
        ).fetchone()["n"]
        if verified == 2:
            if body.check_profile_version != PROFILE:
                raise HTTPException(status_code=422, detail="Unsupported check profile")
            inserted = connection.execute(
                "INSERT INTO public.review_runs (review_id,config_hash) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING review_id",
                (row["id"], CONFIG),
            ).fetchone()
            if inserted:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended('review-admission',0))"
                )
                pending = connection.execute(
                    "SELECT count(*) AS n FROM public.review_runs WHERE status IN ('queued','running')"
                ).fetchone()["n"]
                if pending > 100:
                    raise HTTPException(
                        status_code=429, detail="Review backlog limit reached"
                    )
                producer.configure_task(
                    "process-review", connection=connection, lock=str(row["id"])
                ).defer(review_id=str(row["id"]))
        run = connection.execute(
            "SELECT status,findings,error_code FROM public.review_runs WHERE review_id=%s",
            (row["id"],),
        ).fetchone()
    return {**row, **(run or {"status": "registered"})}


@app.get("/api/reviews/{review_id}", response_model=Review)
def get_review(review_id: UUID):
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM public.reviews WHERE id = %s", (review_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Review not found")
        run = connection.execute(
            "SELECT status,findings,error_code FROM public.review_runs WHERE review_id=%s",
            (row["id"],),
        ).fetchone()
    return {**row, **(run or {"status": "registered"})}


@app.put(
    "/api/documents/{document_type}/{upload_id}",
    response_model=DocumentVersion,
    status_code=201,
)
async def upload_document(
    document_type: Literal["report", "summary"], upload_id: UUID, request: Request
):
    if request.headers.get("content-type", "").split(";")[0] != "application/pdf":
        raise HTTPException(status_code=415, detail="Expected application/pdf")
    try:
        async with asyncio.timeout(20):
            await uploads.acquire()
    except TimeoutError:
        raise HTTPException(
            status_code=429, detail="Upload capacity exhausted"
        ) from None
    try:
        content = bytearray()
        async with asyncio.timeout(20):
            async for part in request.stream():
                content.extend(part)
                if len(content) > MAX_BYTES:
                    raise HTTPException(status_code=413, detail="PDF exceeds 5 MiB")
        from starlette.concurrency import run_in_threadpool

        return await run_in_threadpool(
            _commit_upload, document_type, upload_id, bytes(content)
        )
    except TimeoutError:
        raise HTTPException(status_code=408, detail="Upload timed out") from None
    finally:
        uploads.release()


def _commit_upload(document_type, upload_id, content):
    try:
        extract_pages(content)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="Unsupported, empty or oversized PDF"
        ) from None
    digest = hashlib.sha256(content).hexdigest()
    with connect() as c:
        previous = c.execute(
            "SELECT content_hash,document_type,verified FROM public.document_versions WHERE id=%s",
            (upload_id,),
        ).fetchone()
        if previous and (
            not previous["verified"]
            or previous["content_hash"] != digest
            or previous["document_type"] != document_type
        ):
            raise HTTPException(
                status_code=409, detail="Document identity already exists"
            )
        c.execute(
            "INSERT INTO public.upload_intents (id,document_type,content_hash,size_bytes) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (upload_id, document_type, digest, len(content)),
        )
        intent = c.execute(
            "SELECT * FROM public.upload_intents WHERE id=%s", (upload_id,)
        ).fetchone()
        if intent["content_hash"] != digest or intent["document_type"] != document_type:
            raise HTTPException(
                status_code=409,
                detail="Upload identity already used for different content",
            )
    key = f"originals/{upload_id}.pdf"
    store = BlobStore()
    try:
        store.put(key, content, "application/pdf")
    finally:
        store.close()
    with connect() as c:
        c.execute(
            "INSERT INTO public.document_versions (id,document_type,content_hash,storage_ref,verified) "
            "VALUES (%s,%s,%s,%s,true) ON CONFLICT DO NOTHING",
            (upload_id, document_type, digest, key),
        )
        return c.execute(
            "SELECT * FROM public.document_versions WHERE id=%s AND verified",
            (upload_id,),
        ).fetchone()


@app.get("/api/reviews/{review_id}/evidence")
def review_evidence(review_id: UUID):
    with connect() as c:
        run = c.execute(
            "SELECT config_hash,status,model_deployment,actual_model,worker_attempts,model_calls "
            "FROM public.review_runs WHERE review_id=%s",
            (review_id,),
        ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="Processing run not found")
        steps = c.execute(
            "SELECT step,output FROM public.review_steps WHERE review_id=%s ORDER BY step",
            (review_id,),
        ).fetchall()
        usage = c.execute(
            "SELECT COALESCE(sum(input_tokens),0) AS known_input_tokens, "
            "COALESCE(sum(output_tokens),0) AS known_output_tokens, "
            "count(*) FILTER (WHERE completed_at IS NULL OR input_tokens IS NULL OR output_tokens IS NULL) AS uncertain_attempts "
            "FROM public.inference_attempts WHERE review_id=%s",
            (review_id,),
        ).fetchone()
    return {"run": run, "steps": steps, "usage": usage}
