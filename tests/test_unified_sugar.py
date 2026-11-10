"""Optional Sugar rendering must use aggregate worker totals."""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


def _requires_sugar(cls: type) -> type:
    if importlib.util.find_spec("pytest_sugar") is None:
        return pytest.mark.skip(reason="pytest-sugar is not installed")(cls)
    return cls


@_requires_sugar
class TestUnifiedSugar:
    def test_explicit_sugar_exclusion_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_plain.py").write_text("def test_pass(): pass\n")
            env = dict(os.environ, PYTEST_ADDOPTS="-p no:sugar")
            env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
            env.pop("PYTEST_PLUGINS", None)
            result = subprocess.run(
                [sys.executable, "-m", "rtest", "--pytest-functions", "--unified-output", "-n", "2"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "pytest-sugar" not in result.stdout
            assert "1 passed" in result.stdout

    def test_one_sugar_summary_has_filtered_totals_and_finishes_at_100_percent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_sugar.py").write_text(
                "import pytest\n"
                "@pytest.mark.parametrize('value', [1, 2])\ndef test_keep(value): pass\n"
                "def test_failure(): assert False, 'expected failure'\n"
                "def test_drop(): pass\n"
            )
            env = dict(os.environ)
            env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
            env.pop("PYTEST_PLUGINS", None)
            env["PYTEST_ADDOPTS"] = ""
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "rtest",
                    "--pytest-functions",
                    "--unified-output",
                    "-n",
                    "2",
                    "--env",
                    "PYTEST_ADDOPTS=--force-sugar -k 'not drop'",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 1, result.stdout + result.stderr
            assert result.stdout.count("Test session starts") == 1
            assert result.stdout.count("Results (") == 1
            assert "pytest-sugar" in result.stdout
            assert "100%" in result.stdout
            assert "2 passed" in result.stdout
            assert "1 failed" in result.stdout
            assert "expected failure" in result.stdout

    def test_sugar_retains_progress_after_setup_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "test_sugar.py").write_text(
                "import pytest, time\n"
                "def test_fast(): pass\n"
                "@pytest.fixture\ndef slow(): time.sleep(6)\n"
                "def test_slow(slow): pass\n"
            )
            env = dict(os.environ, PYTEST_ADDOPTS="--force-sugar")
            env.pop("PYTEST_DISABLE_PLUGIN_AUTOLOAD", None)
            env.pop("PYTEST_PLUGINS", None)
            result = subprocess.run(
                [sys.executable, "-m", "rtest", "--pytest-functions", "--unified-output", "-n", "2"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "setup test_sugar.py::test_slow" in result.stdout
            assert "100%" in result.stdout
            assert "2 passed" in result.stdout
            assert result.stdout.count("Results (") == 1
