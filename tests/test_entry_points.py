"""The launcher must work however it is invoked.

`dropwatch.py` sits next to a package also called `dropwatch`, so the
import path has to be arranged carefully. Both of the plausible mistakes —
importing the shim instead of the package, and the shim importing itself
forever — have happened, hence these run the real interpreter.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestInvocations:
    def test_script_from_the_project_root(self):
        result = run(["dropwatch.py", "--version"], ROOT)
        assert result.returncode == 0, result.stderr
        assert "dropwatch" in result.stdout

    def test_script_by_absolute_path_from_elsewhere(self, tmp_path):
        result = run([str(ROOT / "dropwatch.py"), "--version"], tmp_path)
        assert result.returncode == 0, result.stderr

    def test_module_form_from_the_project_root(self):
        # The shim shadows the package here unless sys.modules is corrected.
        result = run(["-m", "dropwatch", "--version"], ROOT)
        assert result.returncode == 0, result.stderr

    def test_no_recursion_error(self):
        result = run(["dropwatch.py", "--help"], ROOT)
        assert "RecursionError" not in result.stderr
        assert result.returncode == 0

    def test_help_lists_the_subcommands(self):
        result = run(["dropwatch.py", "--help"], ROOT)
        # Only advertised commands: `artists` used to be listed here and passed
        # by accident, matching "Deezer artists" in the `map` help text long
        # after it stopped being a subcommand.
        for command in ("scan", "check", "status", "fix", "map", "unmap", "block", "cache"):
            assert command in result.stdout

    def test_missing_configuration_exits_three_not_a_traceback(self, tmp_path):
        env_file = tmp_path / "empty.env"
        env_file.write_text("")
        result = run(
            [str(ROOT / "dropwatch.py"), "check", "--env-file", str(env_file)],
            tmp_path,
        )
        assert result.returncode == 3, result.stderr
        assert "Traceback" not in result.stderr
        assert "error:" in result.stderr
