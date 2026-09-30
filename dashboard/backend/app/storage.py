"""Document-store interface + provider registry, mirroring
core/adapters/base.py's LanguageAdapter/ADAPTER_REGISTRY pattern: one ABC,
one concrete class per cloud provider, one plain dict mapping a
CLOUD_PROVIDER name to its class. Adding a third provider later is one new
class + one registry entry -- nothing here or at any call site changes.

Every existing call site keeps calling the plain module-level functions
below (create_doc/get_doc/...) exactly as it did when this module was
firestore_db.py -- they just forward to whichever DocumentStore the
registry picks, cached for the life of the process."""

import os
from abc import ABC, abstractmethod
from typing import Any


class DocumentStore(ABC):
    @abstractmethod
    def create_doc(self, collection: str, data: dict[str, Any], doc_id: str | None = None) -> str: ...

    @abstractmethod
    def get_doc(self, collection: str, doc_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def list_docs(
        self, collection: str, order_by: str | None = None, descending: bool = False
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    def update_doc(self, collection: str, doc_id: str, data: dict[str, Any]) -> None: ...

    @abstractmethod
    def delete_doc(self, collection: str, doc_id: str) -> None: ...


def _gcp():
    from .storage_gcp import FirestoreStore

    return FirestoreStore


def _azure():
    from .storage_azure import CosmosStore

    return CosmosStore


def _local():
    from .storage_local import MongoStore

    return MongoStore


# Values are loader functions, not classes directly -- keeps a provider's
# SDK import lazy (only the one actually selected by CLOUD_PROVIDER ever
# gets imported), same reasoning core/llm_config.py already applies to
# LiteLlm's import for non-Google vendors.
STORAGE_REGISTRY = {
    "gcp": _gcp,
    "azure": _azure,
    "local": _local,
}

_instance: DocumentStore | None = None


def get_storage() -> DocumentStore:
    global _instance
    if _instance is None:
        provider = os.environ.get("CLOUD_PROVIDER", "gcp")
        if provider not in STORAGE_REGISTRY:
            raise ValueError(f"No DocumentStore registered for CLOUD_PROVIDER={provider!r}, expected one of {sorted(STORAGE_REGISTRY)}")
        _instance = STORAGE_REGISTRY[provider]()()
    return _instance


def create_doc(collection: str, data: dict[str, Any], doc_id: str | None = None) -> str:
    return get_storage().create_doc(collection, data, doc_id)


def get_doc(collection: str, doc_id: str) -> dict[str, Any] | None:
    return get_storage().get_doc(collection, doc_id)


def list_docs(collection: str, order_by: str | None = None, descending: bool = False) -> list[dict[str, Any]]:
    return get_storage().list_docs(collection, order_by, descending)


def update_doc(collection: str, doc_id: str, data: dict[str, Any]) -> None:
    get_storage().update_doc(collection, doc_id, data)


def delete_doc(collection: str, doc_id: str) -> None:
    get_storage().delete_doc(collection, doc_id)
