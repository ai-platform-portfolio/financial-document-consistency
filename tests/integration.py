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
        cls.addClassCleanup(cls.cleanup)
        password = secrets.token_hex(24)
        fixture_env = {**os.environ, "POSTGRES_PASSWORD": password}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        docker("run", "--detach", "--name", cls.container,
               "--publish", f"127.0.0.1:{port}:5432", "--env", "POSTGRES_PASSWORD",
               "--env", "POSTGRES_DB=consistency_test", POSTGRES, env=fixture_env)
        cls.admin_url = f"postgresql://postgres:{password}@127.0.0.1:{port}/consistency_test?sslmode=disable"

        def database_ready():
            with psycopg.connect(cls.admin_url, connect_timeout=1) as connection:
                return connection.execute("SELECT 1").fetchone() == (1,)

        wait_for(database_ready)
        runtime_password = secrets.token_hex(24)
        migrator_password = secrets.token_hex(24)
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            for role, role_password in [("app_migrator", migrator_password),
                                        ("app_runtime", runtime_password)]:
                connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(role_password)
                ))
            connection.execute("REVOKE ALL ON DATABASE consistency_test FROM PUBLIC")
            connection.execute("GRANT CONNECT ON DATABASE consistency_test TO app_migrator, app_runtime")
            connection.execute("GRANT CREATE ON DATABASE consistency_test TO app_migrator")
            connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            connection.execute("GRANT USAGE, CREATE ON SCHEMA public TO app_migrator")
            connection.execute("GRANT USAGE ON SCHEMA public TO app_runtime")
            connection.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE app_migrator IN SCHEMA public "
                "GRANT SELECT, INSERT ON TABLES TO app_runtime"
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
        subprocess.run(["atlas", "migrate", "apply", "--env", "local"],
                       cwd=ROOT, env=cls.migration_env, check=True, stdout=subprocess.DEVNULL)

    @classmethod
    def start_api(cls):
        cls.api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
             "--port", str(cls.api_port), "--no-access-log", "--log-level", "warning"],
            cwd=ROOT / "backend",
            env={**os.environ, "DATABASE_URL": cls.runtime_url, "DATABASE_AUTH": "password",
                 "PYTHONDONTWRITEBYTECODE": "1"},
        )
        wait_for(lambda: cls.request("/api/readyz")[0] == 200)

    @classmethod
    def cleanup(cls):
        if cls.api:
            cls.api.terminate()
            cls.api.wait(timeout=10)
        subprocess.run(["docker", "rm", "--force", "--volumes", cls.container],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

    @classmethod
    def request(cls, path, body=None, key=None):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        request = Request(cls.base + path, data=None if body is None else json.dumps(body).encode(),
                          headers=headers)
        try:
            with urlopen(request, timeout=8) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def document(self, kind):
        status, row = self.request("/api/document-versions", {
            "content_hash": hashlib.sha256(uuid4().bytes).hexdigest(),
            "document_type": kind, "storage_ref": "packs/" + uuid4().hex,
        })
        self.assertEqual(status, 201)
        self.assertTrue(row["created_at"])
        return row

    def review_input(self):
        return {"report_version_id": self.document("report")["id"],
                "summary_version_id": self.document("summary")["id"],
                "check_profile_version": "financial-v1"}

    def test_concurrent_retries_and_conflicting_input(self):
        body, key = self.review_input(), uuid4().hex
        with ThreadPoolExecutor(max_workers=8) as workers:
            responses = list(workers.map(lambda _: self.request("/api/reviews", body, key), range(8)))
        self.assertTrue(all(status == 201 for status, _ in responses), responses)
        saved = responses[0][1]
        self.assertTrue(all(row == saved for _, row in responses))
        self.assertEqual(saved["status"], "registered")
        self.assertEqual(self.request("/api/reviews/" + saved["id"]), (200, saved))
        self.assertNotIn("idempotency_key", saved)
        self.assertNotIn("request_fingerprint", saved)
        with psycopg.connect(self.runtime_url) as connection:
            count = connection.execute("SELECT count(*) FROM public.reviews WHERE idempotency_key = %s",
                                       (key,)).fetchone()[0]
            self.assertEqual(count, 1)
        changed = {**body, "check_profile_version": "financial-v2"}
        self.assertEqual(self.request("/api/reviews", changed, key)[0], 409)
        competing_key = uuid4().hex
        with ThreadPoolExecutor(max_workers=2) as workers:
            competing = list(workers.map(
                lambda value: self.request("/api/reviews", value, competing_key), [body, changed]
            ))
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
            {**body, "report_version_id": body["summary_version_id"],
             "summary_version_id": body["report_version_id"]},
            {**body, "check_profile_version": ""}, {**body, "extra": "rejected"},
        ]:
            self.assertEqual(self.request("/api/reviews", invalid, uuid4().hex)[0], 422)
        self.assertEqual(self.request("/api/reviews", body)[0], 422)
        self.assertEqual(self.request("/api/reviews", body, "invalid key")[0], 422)
        self.assertEqual(self.request("/api/reviews/" + str(uuid4()))[0], 404)
        self.assertEqual(self.request("/api/reviews/not-a-uuid")[0], 422)
        valid_document = {"content_hash": "a" * 64, "document_type": "report", "storage_ref": "pack/report"}
        for invalid in [{**valid_document, "content_hash": "bad"},
                        {**valid_document, "document_type": "other"},
                        {**valid_document, "storage_ref": "https://external.example/file"},
                        {**valid_document, "storage_ref": "x" * 513}]:
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
            for statement in ["CREATE TABLE public.forbidden (id integer)",
                              "CREATE ROLE forbidden_role",
                              "ALTER TABLE public.reviews ADD COLUMN forbidden integer",
                              "UPDATE public.reviews SET check_profile_version = 'changed'",
                              "DELETE FROM public.document_versions",
                              "SELECT * FROM atlas.atlas_schema_revisions"]:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=statement):
                    connection.execute(statement)
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                connection.execute("""
                    INSERT INTO public.reviews
                    (id, report_version_id, summary_version_id, check_profile_version,
                     idempotency_key, request_fingerprint) VALUES (%s, %s, %s, 'v1', %s, %s)
                """, (uuid4(), uuid4(), uuid4(), uuid4().hex, "a" * 64))

    def test_database_outage_and_recovery(self):
        body = self.review_input()
        docker("stop", "--time", "1", self.container)
        try:
            self.assertEqual(self.request("/api/healthz"), (200, {"status": "ok"}))
            for path, value in [("/api/readyz", None),
                                ("/api/reviews/" + str(uuid4()), None),
                                ("/api/reviews", body)]:
                started = time.monotonic()
                self.assertEqual(self.request(path, value, uuid4().hex),
                                 (503, {"detail": "Database unavailable"}))
                self.assertLess(time.monotonic() - started, 6)
        finally:
            docker("start", self.container)
        wait_for(lambda: self.request("/api/readyz")[0] == 200)
        self.assertEqual(self.request("/api/reviews", body, uuid4().hex)[0], 201)


if __name__ == "__main__":
    unittest.main(verbosity=2)
