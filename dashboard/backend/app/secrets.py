"""Secret-store interface + provider registry -- same pattern as
storage.py/core/adapters/base.py. Every existing call site keeps calling
the plain module-level functions below exactly as it did when this module
was secret_manager.py."""

import os
from abc import ABC, abstractmethod


class SecretStore(ABC):
    @abstractmethod
    def create_secret_with_value(self, secret_id: str, value: str) -> None: ...

    @abstractmethod
    def add_secret_version(self, secret_id: str, value: str) -> None: ...

    @abstractmethod
    def access_secret_value(self, secret_id: str) -> str: ...

    @abstractmethod
    def secret_exists(self, secret_id: str) -> bool: ...

    @abstractmethod
    def delete_secret(self, secret_id: str) -> None: ...


def _gcp():
    from .secrets_gcp import SecretManagerStore

    return SecretManagerStore


def _azure():
    from .secrets_azure import KeyVaultStore

    return KeyVaultStore


def _local():
    from .secrets_local import LocalSecretStore

    return LocalSecretStore


SECRET_REGISTRY = {
    "gcp": _gcp,
    "azure": _azure,
    "local": _local,
}

_instance: SecretStore | None = None


def get_secret_store() -> SecretStore:
    global _instance
    if _instance is None:
        provider = os.environ.get("CLOUD_PROVIDER", "gcp")
        if provider not in SECRET_REGISTRY:
            raise ValueError(f"No SecretStore registered for CLOUD_PROVIDER={provider!r}, expected one of {sorted(SECRET_REGISTRY)}")
        _instance = SECRET_REGISTRY[provider]()()
    return _instance


def create_secret_with_value(secret_id: str, value: str) -> None:
    get_secret_store().create_secret_with_value(secret_id, value)


def add_secret_version(secret_id: str, value: str) -> None:
    get_secret_store().add_secret_version(secret_id, value)


def access_secret_value(secret_id: str) -> str:
    return get_secret_store().access_secret_value(secret_id)


def secret_exists(secret_id: str) -> bool:
    return get_secret_store().secret_exists(secret_id)


def delete_secret(secret_id: str) -> None:
    get_secret_store().delete_secret(secret_id)
