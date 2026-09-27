"""In-memory login-attempt rate limiting -- see auth_routes.py's login().

Deliberately in-process, not a shared store (Firestore/Cosmos or Redis):
this is one Cloud Run Service/Container App instance for an internal admin
tool, so the goal is to slow down a scripted brute force against it, not to
survive an attack distributed across many horizontally-scaled instances --
this app runs as a single instance under normal load (Connections page
create/rotate/delete flows would need the same shared-store treatment if it
ever scaled out, since each instance would otherwise track its own
independent attempt counts)."""

import time
from collections import defaultdict

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60  # 15 minutes

_failures: dict[str, list[float]] = defaultdict(list)


def _prune(key: str, now: float) -> None:
    cutoff = now - WINDOW_SECONDS
    _failures[key] = [t for t in _failures[key] if t > cutoff]
    if not _failures[key]:
        _failures.pop(key, None)


def is_locked_out(key: str) -> bool:
    now = time.time()
    _prune(key, now)
    return len(_failures.get(key, [])) >= MAX_ATTEMPTS


def record_failure(key: str) -> None:
    _failures[key].append(time.time())


def record_success(key: str) -> None:
    _failures.pop(key, None)
