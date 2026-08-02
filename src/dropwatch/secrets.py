"""Secret handling.

`Secret` wraps a credential so it cannot leak through the usual accidental
paths: f-strings, ``repr()``, logging, ``pprint``, tracebacks (which render
locals with ``repr``), pickling, or JSON serialisation. The raw value is only
reachable through an explicit :meth:`Secret.reveal` call, which makes every
real use of the credential greppable.
"""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"


class Secret:
    """A string that refuses to render itself."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value or "")

    def reveal(self) -> str:
        """Return the raw value. Call only at the point of use."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        # Length is safe to expose and is useful for validation messages.
        return len(self._value)

    def __repr__(self) -> str:
        return f"Secret({REDACTED})"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return REDACTED

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    def __reduce__(self):
        raise TypeError("Secret values must not be pickled or copied")

    def __getstate__(self):
        raise TypeError("Secret values must not be serialised")


class SecretRegistry:
    """Tracks live secret values so log records can be scrubbed.

    Registration is by value, never by name, so anything that ends up in a log
    message is caught regardless of which field it came from.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, secret: Secret | str | None) -> None:
        value = secret.reveal() if isinstance(secret, Secret) else secret
        # Very short values would cause absurd over-redaction of normal text.
        if value and len(value) >= 6:
            self._values.add(value)

    def scrub(self, text: str) -> str:
        for value in self._values:
            if value in text:
                text = text.replace(value, REDACTED)
        return text

    def clear(self) -> None:
        self._values.clear()


#: Process-wide registry consulted by the logging redaction filter.
registry = SecretRegistry()


# Belt-and-braces: strip anything that looks like a credential in a URL query
# string, even if it was never registered (e.g. a token built at call time).
_QUERY_SECRET = re.compile(
    r"([?&](?:p|t|s|password|token|api_key|access_token|sid)=)[^&\s]+",
    re.IGNORECASE,
)


def scrub_text(text: str) -> str:
    """Remove known secrets and credential-shaped URL parameters from `text`."""
    return _QUERY_SECRET.sub(r"\1" + REDACTED, registry.scrub(text))
