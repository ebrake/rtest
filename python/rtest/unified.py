"""Replay worker reports through one pytest session, without parent collection."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import warnings
from pathlib import Path
from typing import Any, Iterator, TextIO

import pytest
from _pytest.terminal import TerminalReporter

from rtest.pytest_reporter import REPORT_DIRECTORY


class _Controller:
    def __init__(self, arguments: list[str], directory: Path, environment: dict[str, str]) -> None:
        self.arguments = arguments
        self.directory = directory
        self.environment = environment
        self.streams: dict[Path, TextIO] = {}
        self.active: dict[Path, tuple[str, str, float]] = {}
        self.last_progress = time.monotonic()
        self.expected: int | None = None
        self.collected: dict[Path, tuple[int, int]] = {}
        self.finished: set[Path] = set()
        self.pending: list[dict[str, Any]] = []
        self.collection_finished = False
        self.exitstatus: int | None = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_configure(self, config: pytest.Config) -> None:
        # The reporter is serial; only the Rust subprocess launches workers.
        if hasattr(config.option, "numprocesses"):
            config.option.numprocesses = 0

    @pytest.hookimpl(tryfirst=True)
    def pytest_collection(self) -> bool:
        return True

    def _read_events(self, *, final: bool = False) -> Iterator[tuple[Path, dict[str, Any]]]:
        for path in sorted(self.directory.glob("*.jsonl")):
            if path not in self.streams:
                self.streams[path] = path.open(encoding="utf-8")
            stream = self.streams[path]
            # Bound each worker's turn while live; final draining must reach EOF.
            count = 0
            while final or count < 256:
                offset = stream.tell()
                line = stream.readline()
                if not line.endswith("\n"):
                    stream.seek(offset)
                    break
                count += 1
                yield path, json.loads(line)

    def close(self) -> None:
        for stream in self.streams.values():
            stream.close()
        self.streams.clear()

    def _show_activity(self, session: pytest.Session) -> None:
        now = time.monotonic()
        if not self.active or now - self.last_progress < 5:
            return
        reporter = session.config.pluginmanager.getplugin("terminalreporter")
        if not isinstance(reporter, TerminalReporter):
            return
        nodeid, phase, started = min(self.active.values(), key=lambda activity: activity[2])
        reset_lines = getattr(reporter, "reset_tracked_lines", None)
        if reset_lines is not None:
            # Sugar tracks cursor positions for previously printed test rows.
            # A status line starts a fresh block so later results cannot overwrite it.
            reporter.write_line("")
            reset_lines()
        reporter.write_line(f"rtest: {len(self.active)} workers active; {phase} {nodeid} ({now - started:.0f}s)")
        reporter._tw.flush()
        self.last_progress = now

    def _dispatch(self, config: pytest.Config, event: dict[str, Any]) -> None:
        if event["kind"] == "report":
            data = event["data"]
            if "when" in data:
                config.hook.pytest_runtest_logreport(report=pytest.TestReport._from_json(data))
            else:
                config.hook.pytest_collectreport(report=pytest.CollectReport._from_json(data))
        elif event["kind"] == "warning":
            category = type(event["category"], (Warning,), {})
            warning = warnings.WarningMessage(category(event["message"]), category, event["filename"], event["lineno"])
            config.hook.pytest_warning_recorded.call_historic(
                kwargs={"warning_message": warning, "when": event["when"], "nodeid": event["nodeid"], "location": None}
            )

    def _drain(self, session: pytest.Session, *, final: bool = False) -> None:
        for path, event in self._read_events(final=final):
            kind = event["kind"]
            if kind == "activity":
                self.active[path] = event["nodeid"], event["phase"], time.monotonic()
            elif kind == "idle":
                self.active.pop(path, None)
            elif kind == "workers":
                self.expected = event["count"]
                reporter = session.config.pluginmanager.getplugin("terminalreporter")
                if isinstance(reporter, TerminalReporter):
                    reporter.write_line(f"rtest: {self.expected} workers collecting")
            elif kind == "collection":
                self.collected[path] = event["selected"], event["deselected"]
                reporter = session.config.pluginmanager.getplugin("terminalreporter")
                if isinstance(reporter, TerminalReporter) and reporter.isatty() and self.expected is not None:
                    reporter.rewrite(f"collecting: {len(self.collected)}/{self.expected} workers ready")
            elif kind == "finish":
                self.finished.add(path)
                self.active.pop(path, None)
            elif self.collection_finished:
                self._dispatch(session.config, event)
                self.last_progress = time.monotonic()
            else:
                self.pending.append(event)
        if not self.collection_finished and (final or self.expected == len(self.collected)):
            self.collection_finished = True
            selected = sum(count[0] for count in self.collected.values())
            deselected = sum(count[1] for count in self.collected.values())
            session.testscollected = selected
            reporter = session.config.pluginmanager.getplugin("terminalreporter")
            if isinstance(reporter, TerminalReporter):
                # No parent Collectors/Items exist; only their aggregate counts
                # belong in the terminal report. These fields are pytest's totals.
                reporter._numcollected = selected + deselected
                reporter.stats.setdefault("deselected", []).extend([None] * deselected)
            session.config.hook.pytest_collection_finish(session=session)
            if isinstance(reporter, TerminalReporter) and hasattr(reporter, "tests_count"):
                # Sugar's collection hook sees no parent Items in reporting-only sessions.
                reporter.tests_count = selected
            for event in self.pending:
                self._dispatch(session.config, event)
            self.pending.clear()
        if not final:
            self._show_activity(session)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtestloop(self, session: pytest.Session) -> bool:
        arguments = self.arguments.copy()
        if "--quiet-collection" not in arguments:
            arguments.append("--quiet-collection")
        # Include environment initialized by the parent's pytest configuration
        # (for example a shared random seed), then apply explicit CLI overrides.
        environment = dict(os.environ, **self.environment)
        # Sugar renders only in the parent; workers deliberately do not load it.
        environment["PYTEST_ADDOPTS"] = shlex.join(
            option for option in shlex.split(environment.get("PYTEST_ADDOPTS", "")) if option != "--force-sugar"
        )
        for index, argument in enumerate(arguments):
            if argument.startswith("--env=PYTEST_ADDOPTS="):
                arguments[index] = f"--env=PYTEST_ADDOPTS={environment['PYTEST_ADDOPTS']}"
            elif argument.startswith("PYTEST_ADDOPTS=") and index and arguments[index - 1] in {"-e", "--env"}:
                arguments[index] = f"PYTEST_ADDOPTS={environment['PYTEST_ADDOPTS']}"
        with subprocess.Popen([sys.executable, "-m", "rtest", *arguments], env=environment) as process:
            try:
                while process.poll() is None:
                    self._drain(session)
                    time.sleep(0.025)
                self._drain(session, final=True)
                if self.expected is None and not self.collected and process.returncode == 0:
                    self.expected = 0
                if process.returncode == 0 and len(self.finished) != self.expected:
                    raise RuntimeError("rtest workers finished without complete reporting metadata")
                self.exitstatus = process.returncode if process.returncode >= 0 else 2
            finally:
                if process.poll() is None:
                    process.terminate()
        return True

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if self.exitstatus is not None:
            session.exitstatus = pytest.ExitCode(self.exitstatus)


def run(arguments: list[str]) -> int:
    """Run one reporting-only pytest session around the Rust worker launcher."""
    if any(
        value == "--runner=native" or (value == "--runner" and arguments[index + 1 : index + 2] == ["native"])
        for index, value in enumerate(arguments)
    ):
        print("--unified-output requires the pytest runner", file=sys.stderr)
        return 4
    with tempfile.TemporaryDirectory(prefix="rtest-reports-") as directory:
        root = Path(directory)
        environment = {REPORT_DIRECTORY: directory}
        for index, argument in enumerate(arguments):
            assignment = (
                arguments[index + 1]
                if argument in {"-e", "--env"} and index + 1 < len(arguments)
                else argument.removeprefix("--env=")
                if argument.startswith("--env=")
                else ""
            )
            if "=" in assignment:
                key, value = assignment.split("=", 1)
                environment[key] = value
        controller = _Controller(arguments, root, environment)
        previous_addopts = os.environ.get("PYTEST_ADDOPTS")
        if "PYTEST_ADDOPTS" in environment:
            os.environ["PYTEST_ADDOPTS"] = environment["PYTEST_ADDOPTS"]
        try:
            return int(pytest.main(["-o", "addopts=", "-o", f"cache_dir={root / 'cache'}"], plugins=[controller]))
        finally:
            controller.close()
            if previous_addopts is None:
                os.environ.pop("PYTEST_ADDOPTS", None)
            else:
                os.environ["PYTEST_ADDOPTS"] = previous_addopts
