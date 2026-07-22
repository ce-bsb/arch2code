"""The single failure type of the ingest subpackage.

``app/ingest`` is deliberately free of any ``app.*`` import. It is a standalone
subpackage so that ``app/models.py`` can import :class:`~app.ingest.models.IngestSummary`
without dragging in ``app.errors`` (which imports ``app.models``) and deadlocking
the import graph. The price is one exception class that mirrors ``AppError``'s
four fields instead of subclassing it; the callers in ``routing.py`` and
``api/uploads.py`` translate it with :func:`IngestError.as_kwargs`.

The contract is the project's: nothing fails without saying what to do next. An
``IngestError`` with an empty ``remedy`` is a bug in this package, and
``tests/test_ingest_formats.py`` asserts that every refusal in the registry has
one.
"""

from __future__ import annotations

from typing import Any

__all__ = ["IngestError", "UnsupportedFormat", "CorruptSource", "MissingDependency"]


class IngestError(Exception):
    """A file could not be turned into nodes, edges or pixels.

    :param code: stable machine identifier the front end branches on
    :param title: one short human line
    :param detail: what actually happened
    :param remedy: what the user does about it — never optional in practice
    :param status: the HTTP status the API layer should use
    """

    status: int = 415

    def __init__(
        self,
        code: str,
        title: str,
        detail: str,
        remedy: str,
        *,
        status: int | None = None,
        **context: Any,
    ) -> None:
        super().__init__(f"{code}: {title} — {detail}")
        self.code = code
        self.title = title
        self.detail = detail
        self.remedy = remedy
        if status is not None:
            self.status = status
        self.context: dict[str, Any] = context

    def as_kwargs(self) -> dict[str, Any]:
        """Arguments for ``app.errors.AppError(...)``, so the translation is one line."""
        return {
            "code": self.code,
            "title": self.title,
            "detail": self.detail,
            "remedy": self.remedy,
            "status": self.status,
            "context": dict(self.context),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "detail": self.detail,
            "remedy": self.remedy,
            "context": dict(self.context),
        }


class UnsupportedFormat(IngestError):
    """The format was recognized and this build cannot read it. HTTP 415."""

    status = 415


class CorruptSource(IngestError):
    """The format was recognized and the bytes are broken. HTTP 422."""

    status = 422


class MissingDependency(IngestError):
    """The adapter exists but its library is not installed. HTTP 500 — our fault."""

    status = 500
