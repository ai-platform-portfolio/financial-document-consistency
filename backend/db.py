"""Bounded connections; migrations and runtime retain separate ownership."""

import os
import threading
from contextlib import contextmanager

from telemetry import tracer

from azure.identity import DefaultAzureCredential
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


DATABASE_URL = os.environ["DATABASE_URL"]
DATABASE_AUTH = os.environ.get("DATABASE_AUTH", "password")
if DATABASE_AUTH not in {"password", "entra"}:
    raise ValueError("DATABASE_AUTH must be password or entra")
CREDENTIAL = DefaultAzureCredential() if DATABASE_AUTH == "entra" else None


class Connection(psycopg.Connection):
    @classmethod
    def connect(cls, conninfo="", **kwargs):
        if CREDENTIAL:
            kwargs["password"] = CREDENTIAL.get_token(
                "https://ossrdbms-aad.database.windows.net/.default"
            ).token
        return super().connect(conninfo, **kwargs)


_pool = None
_lock = threading.Lock()


@contextmanager
def connect():
    global _pool
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(
                conninfo=DATABASE_URL,
                connection_class=Connection,
                min_size=0,
                max_size=4,
                timeout=3,
                reconnect_timeout=3,
                check=ConnectionPool.check_connection,
                kwargs={
                    "connect_timeout": 3,
                    "row_factory": dict_row,
                    "options": "-c statement_timeout=3000 -c lock_timeout=2000",
                },
            )
    with tracer.start_as_current_span(
        "postgres.operation", record_exception=False, set_status_on_exception=False
    ):
        with _pool.connection(timeout=3) as connection:
            yield connection
