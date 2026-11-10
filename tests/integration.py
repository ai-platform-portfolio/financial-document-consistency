"""Real HTTP/SQL acceptance on an owned, disposable loopback PostgreSQL fixture.

Run with the backend dependencies installed, Docker running and Atlas logged in.
No external database URL is accepted. Random passwords live only in process memory.
"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import tempfile
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]
POSTGRES = "postgres:18.6@sha256:edb0c08dc33befd0262ab3b5cf1c4f3f8cfd8b09e88c05b45adee29d6761ddb6"


def docker(*args, **kwargs):
    return subprocess.check_output(["docker", *args], text=True, **kwargs).strip()


def wait_for(check):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (psycopg.Error, URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise AssertionError("Local fixture did not become ready within 30 seconds")


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.container = "consistency-test-" + uuid4().hex[:12]
        cls.api = None
        cls.workers = []
        cls.storage = tempfile.TemporaryDirectory(prefix="pipeline-fixture-")
        cls.addClassCleanup(cls.storage.cleanup)
        cls.addClassCleanup(cls.cleanup)
        password = secrets.token_hex(24)
        fixture_env = {**os.environ, "POSTGRES_PASSWORD": password}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        docker(
            "run",
            "--detach",
            "--name",
            cls.container,
            "--publish",
            f"127.0.0.1:{port}:5432",
            "--env",
            "POSTGRES_PASSWORD",
            "--env",
            "POSTGRES_DB=consistency_test",
            POSTGRES,
            env=fixture_env,
        )
        cls.admin_url = f"postgresql://postgres:{password}@127.0.0.1:{port}/consistency_test?sslmode=disable"

        def database_ready():
            with psycopg.connect(cls.admin_url, connect_timeout=1) as connection:
                return connection.execute("SELECT 1").fetchone() == (1,)

        wait_for(database_ready)
        runtime_password = secrets.token_hex(24)
        migrator_password = secrets.token_hex(24)
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            for role, role_password in [
                ("app_migrator", migrator_password),
                ("app_runtime", runtime_password),
            ]:
                connection.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(role_password)
                    )
                )
            connection.execute("REVOKE ALL ON DATABASE consistency_test FROM PUBLIC")
            connection.execute(
                "GRANT CONNECT ON DATABASE consistency_test TO app_migrator, app_runtime"
            )
            connection.execute(
                "GRANT CREATE ON DATABASE consistency_test TO app_migrator"
            )
            connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            connection.execute("GRANT USAGE, CREATE ON SCHEMA public TO app_migrator")
            connection.execute("GRANT USAGE ON SCHEMA public TO app_runtime")
            connection.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public "
                "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime"
            )
            connection.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_runtime"
            )

        cls.runtime_url = f"postgresql://app_runtime:{runtime_password}@127.0.0.1:{port}/consistency_test?sslmode=disable"
        cls.migration_env = {
            **os.environ,
            "DATABASE_URL": f"postgresql://app_migrator:{migrator_password}@127.0.0.1:{port}/consistency_test?sslmode=disable",
            "DEV_DATABASE_URL": "docker://postgres/18.6/dev?search_path=public",
        }
        cls.migrate()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cls.api_port = listener.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.api_port}"
        cls.start_api()

    @classmethod
    def migrate(cls):
        subprocess.run(
            ["atlas", "migrate", "apply", "--env", "local"],
            cwd=ROOT,
            env=cls.migration_env,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    @classmethod
    def start_api(cls):
        cls.api = subprocess.Popen(
            [sys.executable, str(ROOT / "tests/integration.py"), "--serve-fixture"],
            cwd=ROOT / "backend",
            env={
                **os.environ,
                "DATABASE_URL": cls.runtime_url,
                "DATABASE_AUTH": "password",
                "PYTHONDONTWRITEBYTECODE": "1",
                "FIXTURE_STORAGE": cls.storage.name,
                "FIXTURE_API_PORT": str(cls.api_port),
            },
        )
        wait_for(lambda: cls.request("/api/readyz")[0] == 200)

    @classmethod
    def cleanup(cls):
        for process in getattr(cls, "workers", []):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        if cls.api:
            cls.api.terminate()
            cls.api.wait(timeout=10)
        subprocess.run(
            ["docker", "rm", "--force", "--volumes", cls.container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    @classmethod
    def request(cls, path, body=None, key=None):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        request = Request(
            cls.base + path,
            data=None if body is None else json.dumps(body).encode(),
            headers=headers,
        )
        try:
            with urlopen(request, timeout=8) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def tearDown(self):
        for process in self.workers:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)

    def document(self, kind):
        status, row = self.request(
            "/api/document-versions",
            {
                "content_hash": hashlib.sha256(uuid4().bytes).hexdigest(),
                "document_type": kind,
                "storage_ref": "packs/" + uuid4().hex,
            },
        )
        self.assertEqual(status, 201)
        self.assertTrue(row["created_at"])
        return row

    def review_input(self):
        return {
            "report_version_id": self.document("report")["id"],
            "summary_version_id": self.document("summary")["id"],
            "check_profile_version": "financial-v1",
        }

    def test_concurrent_retries_and_conflicting_input(self):
        body, key = self.review_input(), uuid4().hex
        with ThreadPoolExecutor(max_workers=8) as workers:
            responses = list(
                workers.map(lambda _: self.request("/api/reviews", body, key), range(8))
            )
        self.assertTrue(all(status == 201 for status, _ in responses), responses)
        saved = responses[0][1]
        self.assertTrue(all(row == saved for _, row in responses))
        self.assertEqual(saved["status"], "registered")
        self.assertEqual(self.request("/api/reviews/" + saved["id"]), (200, saved))
        self.assertNotIn("idempotency_key", saved)
        self.assertNotIn("request_fingerprint", saved)
        with psycopg.connect(self.runtime_url) as connection:
            count = connection.execute(
                "SELECT count(*) FROM public.reviews WHERE idempotency_key = %s", (key,)
            ).fetchone()[0]
            self.assertEqual(count, 1)
        changed = {**body, "check_profile_version": "financial-v2"}
        self.assertEqual(self.request("/api/reviews", changed, key)[0], 409)
        competing_key = uuid4().hex
        with ThreadPoolExecutor(max_workers=2) as workers:
            competing = list(
                workers.map(
                    lambda value: self.request("/api/reviews", value, competing_key),
                    [body, changed],
                )
            )
        self.assertEqual(sorted(status for status, _ in competing), [201, 409])

    def test_restart_and_migration_reapply_preserve_review(self):
        status, saved = self.request("/api/reviews", self.review_input(), uuid4().hex)
        self.assertEqual(status, 201)
        self.migrate()
        self.api.terminate()
        self.api.wait(timeout=10)
        self.start_api()
        self.assertEqual(self.request("/api/reviews/" + saved["id"]), (200, saved))

    def test_validation(self):
        body = self.review_input()
        for invalid in [
            {**body, "report_version_id": str(uuid4())},
            {**body, "summary_version_id": body["report_version_id"]},
            {
                **body,
                "report_version_id": body["summary_version_id"],
                "summary_version_id": body["report_version_id"],
            },
            {**body, "check_profile_version": ""},
            {**body, "extra": "rejected"},
        ]:
            self.assertEqual(self.request("/api/reviews", invalid, uuid4().hex)[0], 422)
        self.assertEqual(self.request("/api/reviews", body)[0], 422)
        self.assertEqual(self.request("/api/reviews", body, "invalid key")[0], 422)
        self.assertEqual(self.request("/api/reviews/" + str(uuid4()))[0], 404)
        self.assertEqual(self.request("/api/reviews/not-a-uuid")[0], 422)
        valid_document = {
            "content_hash": "a" * 64,
            "document_type": "report",
            "storage_ref": "pack/report",
        }
        for invalid in [
            {**valid_document, "content_hash": "bad"},
            {**valid_document, "document_type": "other"},
            {**valid_document, "storage_ref": "https://external.example/file"},
            {**valid_document, "storage_ref": "x" * 513},
        ]:
            self.assertEqual(self.request("/api/document-versions", invalid)[0], 422)

    def test_runtime_permissions_and_constraints(self):
        with psycopg.connect(self.runtime_url, autocommit=True) as connection:
            privileges = connection.execute("""
                SELECT current_user = 'app_runtime', NOT rolsuper AND NOT rolcreatedb AND NOT rolcreaterole,
                       NOT has_database_privilege(current_user, current_database(), 'CREATE'),
                       NOT has_schema_privilege(current_user, 'public', 'CREATE')
                FROM pg_roles WHERE rolname = current_user
            """).fetchone()
            self.assertEqual(privileges, (True, True, True, True))
            for statement in [
                "CREATE TABLE public.forbidden (id integer)",
                "CREATE ROLE forbidden_role",
                "ALTER TABLE public.reviews ADD COLUMN forbidden integer",
                "TRUNCATE public.reviews",
                "SELECT * FROM atlas.atlas_schema_revisions",
            ]:
                with self.assertRaises(
                    psycopg.errors.InsufficientPrivilege, msg=statement
                ):
                    connection.execute(statement)
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                connection.execute(
                    """
                    INSERT INTO public.reviews
                    (id, report_version_id, summary_version_id, check_profile_version,
                     idempotency_key, request_fingerprint) VALUES (%s, %s, %s, 'v1', %s, %s)
                """,
                    (uuid4(), uuid4(), uuid4(), uuid4().hex, "a" * 64),
                )

    def test_database_outage_and_recovery(self):
        body = self.review_input()
        docker("stop", "--time", "1", self.container)
        try:
            self.assertEqual(self.request("/api/healthz"), (200, {"status": "ok"}))
            for path, value in [
                ("/api/readyz", None),
                ("/api/reviews/" + str(uuid4()), None),
                ("/api/reviews", body),
            ]:
                started = time.monotonic()
                self.assertEqual(
                    self.request(path, value, uuid4().hex),
                    (503, {"detail": "Database unavailable"}),
                )
                self.assertLess(time.monotonic() - started, 6)
        finally:
            docker("start", self.container)
        wait_for(lambda: self.request("/api/readyz")[0] == 200)
        self.assertEqual(self.request("/api/reviews", body, uuid4().hex)[0], 201)

    def upload(self, kind, text):
        from test_processing import pdf

        identity = str(uuid4())
        request = Request(
            self.base + f"/api/documents/{kind}/{identity}",
            data=pdf(text),
            headers={"Content-Type": "application/pdf"},
            method="PUT",
        )
        with urlopen(request, timeout=15) as response:
            row = json.load(response)
        self.assertTrue(row["verified"])
        with urlopen(request, timeout=15) as response:
            self.assertEqual(json.load(response), row)
        return row

    def start_worker(self, delay="0", quiet=False, failure=""):
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "tests/integration.py"), "--worker-fixture"],
            cwd=ROOT / "backend",
            env={
                **os.environ,
                "DATABASE_URL": self.runtime_url,
                "DATABASE_AUTH": "password",
                "FIXTURE_STORAGE": self.storage.name,
                "MODEL_BASE_URL": "https://fixture.invalid/v1/",
                "MODEL_DEPLOYMENT": "fixture",
                "FIXTURE_MODEL_DELAY": delay,
                "FIXTURE_MODEL_FAILURE": failure,
                "WORKER_HEALTH_PATH": str(Path(self.storage.name) / uuid4().hex),
            },
            stdout=subprocess.DEVNULL if quiet else None,
            stderr=subprocess.DEVNULL if quiet else None,
        )
        self.workers.append(process)
        return process

    def verified_review(self):
        report = self.upload(
            "report", "Example Ltd 2025 statutory revenue 100 GBP. Margin declined."
        )
        summary = self.upload(
            "summary", "Example Ltd 2025 statutory revenue 110 GBP. Margin improved."
        )
        body = {
            "report_version_id": report["id"],
            "summary_version_id": summary["id"],
            "check_profile_version": "financial-v1",
        }
        key = uuid4().hex
        with ThreadPoolExecutor(max_workers=4) as workers:
            result = list(
                workers.map(lambda _: self.request("/api/reviews", body, key), range(4))
            )
        self.assertTrue(all(status == 201 for status, _ in result))
        self.assertEqual(len({row["id"] for _, row in result}), 1)
        review = result[0][1]
        with psycopg.connect(self.runtime_url) as c:
            count = c.execute(
                "SELECT count(*) FROM public.procrastinate_jobs WHERE args->>'review_id'=%s",
                (review["id"],),
            ).fetchone()[0]
        self.assertEqual(count, 1)
        return review

    def test_worker_pipeline_and_transactional_rollback(self):
        review = self.verified_review()
        with psycopg.connect(self.runtime_url) as c:
            before = c.execute(
                "SELECT count(*) FROM public.procrastinate_jobs"
            ).fetchone()[0]
            import procrastinate

            producer = procrastinate.App(
                connector=procrastinate.SyncPsycopgConnector(conninfo=self.runtime_url)
            )
            producer.configure_task("process-review", connection=c).defer(
                review_id=str(uuid4())
            )
            c.rollback()
            self.assertEqual(
                c.execute("SELECT count(*) FROM public.procrastinate_jobs").fetchone()[
                    0
                ],
                before,
            )
        worker = self.start_worker()
        wait_for(
            lambda: (
                self.request("/api/reviews/" + review["id"])[1]["status"] == "completed"
            )
        )
        result = self.request("/api/reviews/" + review["id"])[1]
        self.assertEqual(
            [x["classification"] for x in result["findings"]], ["issue", "issue"]
        )
        with psycopg.connect(self.runtime_url) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) FROM public.inference_attempts WHERE review_id=%s",
                    (review["id"],),
                ).fetchone()[0],
                3,
            )
            # A repeated delivered job must reuse terminal publication without another inference.
            producer.configure_task(
                "process-review", connection=c, lock=review["id"]
            ).defer(review_id=review["id"])
        time.sleep(1)
        with psycopg.connect(self.runtime_url) as c:
            self.assertEqual(
                c.execute(
                    "SELECT count(*) FROM public.inference_attempts WHERE review_id=%s",
                    (review["id"],),
                ).fetchone()[0],
                3,
            )
        worker.terminate()
        worker.wait(timeout=15)

    def test_worker_interruption_and_recovery(self):
        review = self.verified_review()
        worker = self.start_worker(delay="3")

        def started():
            with psycopg.connect(self.runtime_url) as c:
                return c.execute(
                    "SELECT count(*)>0 FROM public.inference_attempts WHERE review_id=%s",
                    (review["id"],),
                ).fetchone()[0]

        wait_for(started)
        worker.kill()
        worker.wait(timeout=5)
        replacement = self.start_worker()
        wait_for(
            lambda: (
                self.request("/api/reviews/" + review["id"])[1]["status"] == "completed"
            )
        )
        with psycopg.connect(self.runtime_url) as c:
            calls = c.execute(
                "SELECT model_calls FROM public.review_runs WHERE review_id=%s",
                (review["id"],),
            ).fetchone()[0]
        self.assertGreaterEqual(
            calls, 4
        )  # The interrupted call is an uncertain, reserved attempt.
        self.assertLessEqual(calls, 12)
        replacement.terminate()
        replacement.wait(timeout=15)

    def test_worker_retry_exhaustion_and_invalid_output(self):
        for failure in ["rate", "schema"]:
            with self.subTest(failure=failure):
                review = self.verified_review()
                worker = self.start_worker(failure=failure)
                wait_for(
                    lambda: (
                        self.request("/api/reviews/" + review["id"])[1]["status"]
                        == "failed"
                    )
                )
                worker.terminate()
                worker.wait(timeout=15)
                with psycopg.connect(self.runtime_url) as c:
                    attempts = c.execute(
                        "SELECT model_calls,worker_attempts FROM public.review_runs WHERE review_id=%s",
                        (review["id"],),
                    ).fetchone()
                self.assertEqual(
                    attempts,
                    {"rate": (3, 3), "schema": (1, 1)}[failure],
                )

    def test_exhausted_evidence_preserves_independent_narrative(self):
        review = self.verified_review()
        worker = self.start_worker(failure="evidence")
        path = "/api/reviews/" + review["id"]
        wait_for(lambda: self.request(path)[1]["status"] == "completed")
        worker.terminate()
        worker.wait(timeout=15)
        status, result = self.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(
            {(f["check"], f["classification"]) for f in result["findings"]},
            {("coverage", "unresolved"), ("narrative", "issue")},
        )
        status, evidence = self.request(path + "/evidence")
        self.assertEqual(status, 200)
        self.assertEqual(evidence["run"]["model_calls"], 5)
        self.assertEqual(evidence["run"]["worker_attempts"], 1)
        numeric_steps = [
            s["output"] for s in evidence["steps"] if s["step"] in {"report", "summary"}
        ]
        self.assertEqual(
            numeric_steps, [{"status": "unresolved", "reason": "quote_not_on_page"}] * 2
        )

    def test_unsupported_evidence_repair_is_counted(self):
        review = self.verified_review()
        worker = self.start_worker(failure="repair")
        wait_for(
            lambda: (
                self.request("/api/reviews/" + review["id"])[1]["status"] == "completed"
            )
        )
        worker.terminate()
        worker.wait(timeout=15)
        status, evidence = self.request("/api/reviews/" + review["id"] + "/evidence")
        self.assertEqual(status, 200)
        self.assertEqual(evidence["run"]["model_calls"], 5)
        self.assertEqual(evidence["usage"]["known_output_tokens"], 250)
        self.assertEqual(len(evidence["steps"]), 3)

    def test_backpressure_rolls_back_unaccepted_work(self):
        report = self.upload(
            "report", "Example Ltd 2025 statutory revenue 100 GBP. Margin declined."
        )
        summary = self.upload(
            "summary", "Example Ltd 2025 statutory revenue 110 GBP. Margin improved."
        )
        body = {
            "report_version_id": report["id"],
            "summary_version_id": summary["id"],
            "check_profile_version": "financial-v1",
        }
        ids = []
        try:
            for _ in range(100):
                status, result = self.request("/api/reviews", body, uuid4().hex)
                self.assertEqual(status, 201)
                ids.append(result["id"])
            self.assertEqual(
                self.request("/api/reviews", body, "overflow-" + uuid4().hex)[0], 429
            )
            with psycopg.connect(self.runtime_url) as c:
                self.assertEqual(
                    c.execute(
                        "SELECT count(*) FROM public.review_runs WHERE status IN ('queued','running')"
                    ).fetchone()[0],
                    100,
                )
        finally:
            with psycopg.connect(self.runtime_url) as c:
                c.execute(
                    "DELETE FROM public.procrastinate_jobs WHERE args->>'review_id'=ANY(%s)",
                    (ids,),
                )
                c.execute(
                    "DELETE FROM public.review_runs WHERE review_id=ANY(%s::uuid[])",
                    (ids,),
                )
                c.execute("DELETE FROM public.reviews WHERE id=ANY(%s::uuid[])", (ids,))

    def test_labelled_document_evaluation_harness(self):
        from deployed import run

        cases = json.loads((ROOT / "tests/evaluation_cases.json").read_text())[
            "documents"
        ]
        worker = self.start_worker(quiet=True)
        try:
            reports = [run(self.base, case) for case in cases]
            self.assertEqual(
                sum(
                    x["revenue_extraction_fp"] + x["revenue_extraction_fn"]
                    for x in reports
                ),
                0,
            )
        finally:
            worker.terminate()
            worker.wait(timeout=15)


def fixture_runtime():
    """Test-only adapters; production code contains no mock mode or storage credentials."""
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT / "backend"))
    import storage

    class Store:
        def close(self):
            pass

        def put(self, key, content, content_type):
            path = Path(os.environ["FIXTURE_STORAGE"]) / key
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("xb") as stream:
                    stream.write(content)
            except FileExistsError:
                if path.read_bytes() != content:
                    raise ValueError("Immutable object conflict")

        def get(self, key):
            return (Path(os.environ["FIXTURE_STORAGE"]) / key).read_bytes()

    storage.BlobStore = Store
    import processing

    class Model:
        def __init__(self, **kwargs):
            self.chat = self
            self.completions = self

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def create(self, **kwargs):
            import re

            inputs = json.loads(kwargs["messages"][1]["content"])
            time.sleep(float(os.environ.get("FIXTURE_MODEL_DELAY", "0")))
            failure = os.environ.get("FIXTURE_MODEL_FAILURE", "")
            if failure == "rate":
                import httpx2 as httpx
                from openai import RateLimitError

                raise RateLimitError(
                    "fixture throttled",
                    response=httpx.Response(
                        429, request=httpx.Request("POST", "https://fixture.invalid")
                    ),
                    body={},
                )
            if "pages" in inputs:
                text = inputs["pages"][0]
                match = re.search(r"revenue (\d+)", text)
                value = match[1] if match else None
                output = {
                    "claims": [
                        {
                            "metric": "revenue",
                            "value": value,
                            "entity": "Example Ltd",
                            "period": "2025",
                            "currency": "GBP",
                            "scale": "units",
                            "basis": "statutory",
                            "evidence": {"page": 1, "quote": text},
                        }
                    ]
                }
                if value is None:
                    output = {"claims": []}
                elif failure == "evidence" or (
                    failure == "repair"
                    and "(quote_not_on_page)" not in kwargs["messages"][0]["content"]
                ):
                    output["claims"][0]["evidence"]["quote"] = "Unsupported paraphrase"
            else:
                report_text = inputs["report"][0]["text"]
                summary_text = inputs["summary"][0]["text"]
                classification = (
                    "unresolved"
                    if "not discussed" in report_text
                    else "issue"
                    if ("declined" in report_text) != ("declined" in summary_text)
                    else "consistent"
                )
                output = {
                    "classification": classification,
                    "explanation": "Margin direction contradicts the report",
                    "report_evidence": {
                        "page": 1,
                        "quote": inputs["report"][0]["text"],
                    },
                    "summary_evidence": {
                        "page": 1,
                        "quote": inputs["summary"][0]["text"],
                    },
                }
            if failure == "schema":
                output = {"not_the_schema": "rejected"}
            return SimpleNamespace(
                model="fixture-model-v1",
                usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content=json.dumps(output)),
                    )
                ],
            )

    processing.OpenAI = Model
    processing.get_bearer_token_provider = lambda *args: lambda: "fixture-token"
    if "--serve-fixture" in sys.argv:
        import uvicorn

        uvicorn.run(
            "app:app",
            host="127.0.0.1",
            port=int(os.environ["FIXTURE_API_PORT"]),
            access_log=False,
            log_level="warning",
        )
    else:
        import asyncio
        import worker

        # Accelerate the same native heartbeat/recovery mechanism on this local fixture only.
        import procrastinate

        worker.process.retry_strategy = procrastinate.RetryStrategy(
            max_attempts=3,
            wait=0,
            retry_exceptions=(processing.TransientProcessingError,),
        )
        original = worker.queue.run_worker_async

        async def run(**kwargs):
            kwargs.update(
                update_heartbeat_interval=1,
                stalled_worker_timeout=3,
                fetch_job_polling_interval=0.5,
            )
            return await original(**kwargs)

        worker.queue.run_worker_async = run

        async def recover():
            while True:
                await asyncio.sleep(1)
                for job in await worker.queue.job_manager.get_stalled_jobs(
                    seconds_since_heartbeat=3
                ):
                    if job.task_name == "process-review":
                        await worker.queue.job_manager.retry_job(job)

        async def main():
            recovery = asyncio.create_task(recover())
            try:
                await worker.main()
            finally:
                recovery.cancel()

        asyncio.run(main())


if __name__ == "__main__":
    if "--serve-fixture" in sys.argv or "--worker-fixture" in sys.argv:
        fixture_runtime()
    else:
        unittest.main(verbosity=2)
