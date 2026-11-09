"""Bounded database connections; migrations are run separately by Atlas."""

import os

from azure.identity import DefaultAzureCredential
import psycopg
from psycopg.rows import dict_row


DATABASE_URL = os.environ["DATABASE_URL"]
DATABASE_AUTH = os.environ.get("DATABASE_AUTH", "password")
if DATABASE_AUTH not in {"password", "entra"}:
    raise ValueError("DATABASE_AUTH must be password or entra")

CREDENTIAL = DefaultAzureCredential() if DATABASE_AUTH == "entra" else None


def connect():
    credentials = {}
    if CREDENTIAL:
        credentials["password"] = CREDENTIAL.get_token(
            "https://ossrdbms-aad.database.windows.net/.default"
        ).token
    return psycopg.connect(
        DATABASE_URL,
        **credentials,
        connect_timeout=3,
        options="-c statement_timeout=3000 -c lock_timeout=2000",
        row_factory=dict_row,
    )
