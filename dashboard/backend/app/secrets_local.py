"""MongoDB-backed SecretStore -- local-dev equivalent of secrets_gcp.py's
SecretManagerStore/secrets_azure.py's KeyVaultStore. No real access
control beyond "can you reach the compose network" (same as before) --
deliberately NOT a real secret store, just enough to let the dashboard's
create/rotate/access flows work end to end against a local database. Never
point this at anything other than the local `mongo` service in
docker-compose.yml.

Values ARE encrypted at rest (Fernet, AES-128-CBC + HMAC) as of this
module's latest revision -- previously plaintext, matching a real
complaint: anyone with read access to the `mongo` service/its volume/a
dump of it could previously read every Sonar token, GitHub PAT, and LLM
key directly. A *hash* was asked for first but would have been wrong here
-- hashing is one-way (fine for verifying a password you never need back),
while every value stored through this module (Sonar token, GitHub PAT, LLM
key) has to come back out byte-for-byte to actually authenticate to those
services. Encryption is the reversible equivalent, and it's what Secret
Manager/Key Vault themselves do to data at rest anyway -- this brings
local storage in line with them instead of introducing new behavior.

Key management: LOCAL_SECRETS_ENCRYPTION_KEY, any string (not required to
be a raw Fernet key already -- _fernet() below runs it through SHA-256 to
get the right byte length/encoding Fernet needs, same low-friction "any
string works" ergonomics as SESSION_SECRET_KEY). Defaults to a committed,
clearly-labeled placeholder when unset, same precedent as
SESSION_SECRET_KEY in docker-compose.yml -- fine for a disposable local
dev database, override it with your own value for anything you'd mind
someone else reading off disk. Changing this key makes every
already-stored value permanently undecryptable (by design -- there's no
way to decrypt old ciphertext with a new key); access_secret_value raises
a clear error rather than guessing, and the fix is just to re-add the
affected connection via the Connections page.

Stored in its own `_secrets` collection (not a per-app one named
"secrets") so it can never collide with a real collection the dashboard
creates. Each doc is {_id: secret_id, versions: [v1, v2, ...]} -- an
append-only list standing in for Secret Manager's/Key Vault's own version
history; `access_secret_value` always returns the latest (last) one,
matching both real stores' "versions/latest" behavior."""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken
from pymongo import MongoClient

from .secrets import SecretStore

_DEFAULT_KEY = "local-dev-only-not-a-real-key-do-not-reuse"


def _fernet() -> Fernet:
    passphrase = os.environ.get("LOCAL_SECRETS_ENCRYPTION_KEY", _DEFAULT_KEY)
    # Fernet needs a 32-byte urlsafe-base64 key specifically, not an
    # arbitrary string -- SHA-256 always produces exactly 32 bytes
    # regardless of passphrase length, so any string here works, same as
    # SESSION_SECRET_KEY's own ergonomics.
    key = base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest())
    return Fernet(key)


class LocalSecretStore(SecretStore):
    def __init__(self):
        self._client = None

    def _get_client(self) -> MongoClient:
        if self._client is None:
            self._client = MongoClient(os.environ.get("MONGO_URL", "mongodb://mongo:27017"))
        return self._client

    def _collection(self):
        db_name = os.environ.get("MONGO_DB", "dashboard")
        return self._get_client()[db_name]["_secrets"]

    def create_secret_with_value(self, secret_id: str, value: str) -> None:
        encrypted = _fernet().encrypt(value.encode()).decode()
        self._collection().replace_one({"_id": secret_id}, {"_id": secret_id, "versions": [encrypted]}, upsert=True)

    def add_secret_version(self, secret_id: str, value: str) -> None:
        encrypted = _fernet().encrypt(value.encode()).decode()
        self._collection().update_one({"_id": secret_id}, {"$push": {"versions": encrypted}}, upsert=True)

    def access_secret_value(self, secret_id: str) -> str:
        doc = self._collection().find_one({"_id": secret_id})
        if not doc or not doc.get("versions"):
            raise KeyError(f"No secret value found for {secret_id!r}")
        try:
            return _fernet().decrypt(doc["versions"][-1].encode()).decode()
        except InvalidToken as e:
            # Deliberately not auto-"migrated"/overwritten on a decrypt
            # failure -- a value written under a DIFFERENT
            # LOCAL_SECRETS_ENCRYPTION_KEY fails the exact same way a
            # pre-encryption plaintext value does (Fernet can't tell "wrong
            # key" from "not a Fernet token at all" from the ciphertext
            # alone), so silently treating either as "must be legacy
            # plaintext, re-encrypt it" risks permanently overwriting
            # already-undecryptable real data with garbage instead of
            # just failing loudly.
            raise KeyError(
                f"Could not decrypt stored secret for {secret_id!r} -- it either predates this encryption-at-rest "
                "change, or LOCAL_SECRETS_ENCRYPTION_KEY changed since it was saved. Re-add it via the Connections "
                "page (or whichever router created it) to store it encrypted under the current key."
            ) from e

    def secret_exists(self, secret_id: str) -> bool:
        return self._collection().find_one({"_id": secret_id}) is not None

    def delete_secret(self, secret_id: str) -> None:
        self._collection().delete_one({"_id": secret_id})
