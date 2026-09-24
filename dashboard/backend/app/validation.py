"""Shared field types for request validation -- every router's create/update
models build their string fields from these instead of a bare `str`, so
"empty", "just whitespace", and "not actually a URL" are rejected
consistently (422, before any handler code or Firestore/Secret Manager
write runs) rather than each router re-deriving its own rule."""

from typing import Annotated

from pydantic import AfterValidator, StringConstraints

# Trims surrounding whitespace and rejects "" / "   " -- a bare `str` field
# happily accepts either, which would otherwise create a Sonar server named
# " " or silently store a token that's just spaces.
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Passwords are NOT whitespace-stripped (a leading/trailing space could be
# intentional, however unlikely) -- just a minimum length.
PasswordStr = Annotated[str, StringConstraints(min_length=8)]


def _check_http_url(value: str) -> str:
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ValueError("must start with http:// or https://")
    if len(value.split("://", 1)[1]) < 3:
        raise ValueError("must include a host, e.g. https://sonar.example.com")
    return value


# Still a plain str on the model (no URL-object normalization surprises --
# e.g. Pydantic's HttpUrl silently appending a trailing slash), just
# constrained to look like a real http(s) URL.
HttpUrlStr = Annotated[NonEmptyStr, AfterValidator(_check_http_url)]
