"""PARITY-2 — Azure Blob Storage driver for the attachment object store.

Mirrors the S3/MinIO driver pattern (`_s3_blob_store`) with Azure SDK +
DefaultAzureCredential. The live path (against a real Storage Account)
runs only in nightly / live-flagged tests; here we exercise the
construction + wrapper-method dispatch with the SDK mocked, so the
driver shape stays correct under the existing portability contract.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def _mock_azure_sdk(monkeypatch):
    """Provide stub `azure.storage.blob` + `azure.identity` modules so the
    driver imports cleanly without the real SDK installed."""
    # azure.identity → DefaultAzureCredential
    az_id = types.ModuleType("azure.identity")
    az_id.DefaultAzureCredential = MagicMock(name="DefaultAzureCredential")
    # azure.storage.blob → BlobServiceClient (lightweight stub)
    az_sb = types.ModuleType("azure.storage.blob")
    blob_service = MagicMock(name="BlobServiceClient")
    az_sb.BlobServiceClient = blob_service
    # azure (root) parent
    az = types.ModuleType("azure")
    az_storage = types.ModuleType("azure.storage")
    monkeypatch.setitem(sys.modules, "azure", az)
    monkeypatch.setitem(sys.modules, "azure.identity", az_id)
    monkeypatch.setitem(sys.modules, "azure.storage", az_storage)
    monkeypatch.setitem(sys.modules, "azure.storage.blob", az_sb)
    return {"identity": az_id, "blob": az_sb, "client": blob_service}


def test_azure_blob_driver_constructs_with_env_inputs(monkeypatch, _mock_azure_sdk):
    monkeypatch.setenv(
        "ASOE_OBJECT_STORE_ENDPOINT", "https://acct.blob.core.windows.net",
    )
    monkeypatch.setenv("ASOE_OBJECT_STORE_BUCKET", "attachments")
    from gateways.attachment_store import _azure_blob_store
    driver = _azure_blob_store()
    assert driver is not None
    # DefaultAzureCredential was used (no plaintext keys).
    _mock_azure_sdk["identity"].DefaultAzureCredential.assert_called_once()
    # BlobServiceClient was given the account URL + credential.
    _mock_azure_sdk["blob"].BlobServiceClient.assert_called_once()
    args, kwargs = _mock_azure_sdk["blob"].BlobServiceClient.call_args
    # Either positional or kwarg for account_url.
    assert "acct.blob.core.windows.net" in (args[0] if args else kwargs.get("account_url", ""))


def test_select_backend_routes_to_azure_when_driver_env_set(monkeypatch, _mock_azure_sdk):
    monkeypatch.setenv("ASOE_ATTACHMENT_BACKEND", "object_store")
    monkeypatch.setenv("ASOE_OBJECT_STORE_DRIVER", "azure")
    monkeypatch.setenv(
        "ASOE_OBJECT_STORE_ENDPOINT", "https://acct.blob.core.windows.net",
    )
    monkeypatch.setenv("ASOE_OBJECT_STORE_BUCKET", "attachments")
    from gateways import attachment_store
    backend = attachment_store._select_backend()
    # Returns an ObjectStoreBackend wrapping the Azure driver.
    assert isinstance(backend, attachment_store.ObjectStoreBackend)


def test_azure_driver_put_get_delete_dispatch(monkeypatch, _mock_azure_sdk):
    monkeypatch.setenv(
        "ASOE_OBJECT_STORE_ENDPOINT", "https://acct.blob.core.windows.net",
    )
    monkeypatch.setenv("ASOE_OBJECT_STORE_BUCKET", "attachments")
    # Wire the BlobServiceClient mock to return a container_client with the
    # required get_blob_client / list_blobs surface.
    blob_client = MagicMock()
    blob_client.download_blob.return_value.readall.return_value = b"DATA"
    container_client = MagicMock()
    container_client.get_blob_client.return_value = blob_client
    container_client.list_blobs.return_value = []
    service = _mock_azure_sdk["blob"].BlobServiceClient.return_value
    service.get_container_client.return_value = container_client

    from gateways.attachment_store import _azure_blob_store
    driver = _azure_blob_store()
    driver.put_blob("tenant/att-1", b"BYTES")
    blob_client.upload_blob.assert_called_once()
    got = driver.get_blob("tenant/att-1")
    assert got == b"DATA"
    driver.delete_blob("tenant/att-1")
    blob_client.delete_blob.assert_called_once()


def test_azure_driver_get_returns_none_on_missing(monkeypatch, _mock_azure_sdk):
    monkeypatch.setenv(
        "ASOE_OBJECT_STORE_ENDPOINT", "https://acct.blob.core.windows.net",
    )
    monkeypatch.setenv("ASOE_OBJECT_STORE_BUCKET", "attachments")
    # Build a ResourceNotFoundError stub matching what azure-core raises.
    from gateways.attachment_store import _azure_blob_store

    class _RNF(Exception):
        pass

    blob_client = MagicMock()
    blob_client.download_blob.side_effect = _RNF("missing")
    container_client = MagicMock()
    container_client.get_blob_client.return_value = blob_client
    service = _mock_azure_sdk["blob"].BlobServiceClient.return_value
    service.get_container_client.return_value = container_client

    # Patch the azure-core exception module the driver references.
    az_core_exc = types.ModuleType("azure.core.exceptions")
    az_core_exc.ResourceNotFoundError = _RNF
    monkeypatch.setitem(sys.modules, "azure.core", types.ModuleType("azure.core"))
    monkeypatch.setitem(sys.modules, "azure.core.exceptions", az_core_exc)

    driver = _azure_blob_store()
    assert driver.get_blob("tenant/missing") is None
