"""Conditional Blob writes through short-lived workload identity."""

import hashlib
import os

from azure.core.exceptions import ResourceExistsError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings


MAX_BYTES = 5 * 1024 * 1024


class BlobStore:
    def __init__(self):
        self.credential = DefaultAzureCredential(
            process_timeout=5, connection_timeout=3, read_timeout=5, retry_total=1
        )
        self.service = BlobServiceClient(
            account_url=os.environ["BLOB_ACCOUNT_URL"],
            credential=self.credential,
            connection_timeout=5,
            read_timeout=15,
            retry_total=2,
        )
        self.container = self.service.get_container_client(os.environ["BLOB_CONTAINER"])

    def close(self):
        self.service.close()
        self.credential.close()

    def put(self, key: str, content: bytes, content_type: str):
        blob = self.container.get_blob_client(key)
        try:
            blob.upload_blob(
                content,
                overwrite=False,
                content_settings=ContentSettings(content_type=content_type),
            )
        except ResourceExistsError:
            # Never silently accept an object with different bytes on a retry.
            if (
                hashlib.sha256(self.get(key)).digest()
                != hashlib.sha256(content).digest()
            ):
                raise ValueError("Immutable object conflict") from None

    def get(self, key: str) -> bytes:
        blob = self.container.get_blob_client(key)
        if blob.get_blob_properties().size > MAX_BYTES:
            raise ValueError("Stored object exceeds limit")
        data = blob.download_blob(max_concurrency=1).readall()
        if len(data) > MAX_BYTES:
            raise ValueError("Stored object exceeds limit")
        return data
