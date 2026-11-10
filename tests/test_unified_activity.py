"""Activity remains visible before test outcomes and across partial file writes."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest
from _pytest.terminal import TerminalReporter

from rtest.unified import _Controller


def test_incomplete_records_are_retried_and_busy_workers_do_not_starve_others() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        controller = _Controller([], tmp_path, {})
        busy = tmp_path / "worker-1.jsonl"
        other = tmp_path / "worker-2.jsonl"
        record = json.dumps({"kind": "idle"}) + "\n"
        busy.write_text(record * 300)
        other.write_text('{"kind": "finish"')
        try:
            first = list(controller._read_events())
            assert len(first) == 256
            with other.open("a") as stream:
                stream.write("}\n")
            second = list(controller._read_events())
            assert len(second) == 45
            assert second[-1] == (other, {"kind": "finish"})
            with busy.open("a") as stream:
                stream.write(record * 300)
            assert len(list(controller._read_events(final=True))) == 300
        finally:
            controller.close()
        assert controller.streams == {}


def test_setup_activity_is_visible_before_all_workers_collect() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        controller = _Controller([], tmp_path, {})
        worker = tmp_path / "worker-1.jsonl"
        worker.write_text(
            json.dumps({"kind": "activity", "nodeid": "test_slow.py::test_case", "phase": "setup"}) + "\n"
        )
        reporter = Mock(spec=TerminalReporter)
        reporter._tw = Mock()
        manager = Mock()
        manager.getplugin.return_value = reporter
        session = cast(pytest.Session, SimpleNamespace(config=SimpleNamespace(pluginmanager=manager)))
        controller.expected = 2
        controller.last_progress -= 6
        try:
            controller._drain(session)
            assert not controller.collection_finished
            message = reporter.write_line.call_args.args[0]
            assert "1 workers active; setup test_slow.py::test_case" in message
            reporter._tw.flush.assert_called_once()
            controller._drain(session)
            assert reporter.write_line.call_count == 1
            with worker.open("a") as stream:
                stream.write(json.dumps({"kind": "idle"}) + "\n")
            controller._drain(session)
            assert not controller.active
        finally:
            controller.close()
