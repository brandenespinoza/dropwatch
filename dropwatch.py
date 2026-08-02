#!/usr/bin/env python3
"""Entry point so the tool can run as `python dropwatch.py`.

Adds `src/` to the import path when the package has not been pip-installed.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir():
    # src/ must come *before* this script's own directory, otherwise
    # `import dropwatch` finds this file instead of the package and
    # imports itself forever. An editable install may already have src/ on the
    # path but behind the script directory, so move it rather than skip it.
    _src = str(_SRC)
    while _src in sys.path:
        sys.path.remove(_src)
    sys.path.insert(0, _src)
    # `python3 -m dropwatch` run from this directory imports *this file* as
    # the `dropwatch` module, which then shadows the real package in src/.
    # Drop that non-package entry so the import below finds the package.
    _shadow = sys.modules.get("dropwatch")
    if _shadow is not None and not hasattr(_shadow, "__path__"):
        del sys.modules["dropwatch"]

from dropwatch.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
