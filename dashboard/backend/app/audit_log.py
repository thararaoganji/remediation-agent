"""Structured security-event logging (OWASP A09) -- plain stdlib `logging`
to stdout is deliberate, not a gap: both Cloud Run and Container Apps
capture container stdout/stderr as platform logs automatically, so this
needs no separate logging backend to be queryable/alertable on on either
cloud. `extra=` fields land as structured fields in Cloud Logging's jsonPayload
and are visible as regular log-line content everywhere else (e.g. `docker
logs` during local dev) -- never lost, just not structured off-platform."""

import logging
import sys

logger = logging.getLogger("dashboard.audit")
logger.setLevel(logging.INFO)
# Explicit handler, not reliant on root logger config -- the root logger's
# default level is WARNING with no handler at all, which silently drops
# every .info() call below with no error or warning of any kind (confirmed
# live: none of these showed up in a real run's logs until this was added,
# while uvicorn's own request-line INFO logs appeared fine throughout,
# since uvicorn configures its own loggers explicitly the same way this
# now does).
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(_handler)
    logger.propagate = False


def _log(action: str, actor: str | None, **fields) -> None:
    logger.info("audit action=%s actor=%s %s", action, actor, fields)


def login_failed(email: str, client_ip: str) -> None:
    _log("login_failed", email, client_ip=client_ip)


def login_locked_out(email: str, client_ip: str) -> None:
    _log("login_locked_out", email, client_ip=client_ip)


def login_succeeded(email: str, client_ip: str) -> None:
    _log("login_succeeded", email, client_ip=client_ip)


def password_changed(email: str) -> None:
    _log("password_changed", email)


def user_created(actor_email: str, new_user_email: str, role: str) -> None:
    _log("user_created", actor_email, new_user_email=new_user_email, role=role)


def user_deleted(actor_email: str, target_email: str) -> None:
    _log("user_deleted", actor_email, target_email=target_email)


def credential_rotated(actor_email: str, kind: str, resource_id: str) -> None:
    _log("credential_rotated", actor_email, kind=kind, resource_id=resource_id)


def credential_deleted(actor_email: str, kind: str, resource_id: str) -> None:
    _log("credential_deleted", actor_email, kind=kind, resource_id=resource_id)
