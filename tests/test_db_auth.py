"""Fresh Entra credentials on every pooled or queue listener connection."""

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://unconfigured/unused")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import db


class DatabaseAuthenticationTests(unittest.TestCase):
    def test_sync_connections_refresh_tokens_and_preserve_tls_url(self):
        credential = Mock()
        credential.get_token.side_effect = [SimpleNamespace(token="first"), SimpleNamespace(token="second")]
        url = "postgresql://runtime@server/database?sslmode=verify-full"
        with patch.object(db, "CREDENTIAL", credential), patch("psycopg.Connection.connect") as connect:
            db.Connection.connect(url)
            db.Connection.connect(url)
        self.assertEqual([c.kwargs["password"] for c in connect.call_args_list], ["first", "second"])
        self.assertTrue(all(c.args == (url,) for c in connect.call_args_list))

    def test_async_connections_refresh_tokens_including_listener_reconnects(self):
        async def run():
            credential = Mock()
            credential.get_token.side_effect = [SimpleNamespace(token="first"), SimpleNamespace(token="second")]
            with patch.object(db, "CREDENTIAL", credential), patch("psycopg.AsyncConnection.connect", new_callable=AsyncMock) as connect:
                await db.AsyncConnection.connect(db.DATABASE_URL, autocommit=True)
                await db.AsyncConnection.connect(db.DATABASE_URL, autocommit=True)
            self.assertEqual([c.kwargs["password"] for c in connect.call_args_list], ["first", "second"])
            self.assertTrue(all(c.kwargs["autocommit"] for c in connect.call_args_list))
        asyncio.run(run())

    def test_certificate_connections_do_not_inject_a_password(self):
        with patch.object(db, "CREDENTIAL", None), patch("psycopg.Connection.connect") as connect:
            db.Connection.connect(db.DATABASE_URL)
        self.assertNotIn("password", connect.call_args.kwargs)

    def test_failed_token_acquisition_cannot_open_an_unauthenticated_connection(self):
        credential = Mock()
        credential.get_token.side_effect = RuntimeError("identity unavailable")
        with patch.object(db, "CREDENTIAL", credential), patch("psycopg.Connection.connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "identity unavailable"):
                db.Connection.connect(db.DATABASE_URL)
        connect.assert_not_called()
