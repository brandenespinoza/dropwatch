"""Logging that always goes to stderr and always redacts credentials."""

from __future__ import annotations

import logging
import sys

from .secrets import scrub_text


class RedactionFilter(logging.Filter):
    """Scrub secrets from the formatted message and any string arguments.

    Applied as a filter rather than a formatter so it protects every handler,
    including ones added later.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = scrub_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: scrub_text(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            else:
                record.args = tuple(
                    scrub_text(a) if isinstance(a, str) else a for a in record.args
                )
        return True


def setup_logging(verbosity: int = 0) -> None:
    """0 = warnings only, 1 = info, 2+ = debug. Everything goes to stderr."""
    level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(levelname)s %(name)s: %(message)s"
            if verbosity >= 2
            else "%(levelname)s: %(message)s"
        )
    )
    handler.addFilter(RedactionFilter())

    root = logging.getLogger("dropwatch")
    root.handlers.clear()
    root.setLevel(level)
    root.addHandler(handler)
    root.propagate = False
