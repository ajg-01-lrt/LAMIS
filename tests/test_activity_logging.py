"""Regression coverage for ATLAS's unified file/GUI activity stream."""

from __future__ import annotations

import logging
import inspect
from queue import Queue
from types import SimpleNamespace
import threading

from gui.gui4_0 import InventoryGUI, _GuiQueueLogHandler
from gui import gui4_0
from gui.rls_r4_0_config_frame import _log_r40_event
from gui import rls_route_frame
from utils.helpers import CredentialFilter
from utils import logging_setup


def _remove_atlas_handlers() -> None:
    root = logging.getLogger()
    for handler in tuple(root.handlers):
        if (
            getattr(handler, "_atlas_file_handler", False)
            or getattr(handler, "_atlas_console_handler", False)
            or getattr(handler, "_atlas_gui_handler", False)
        ):
            root.removeHandler(handler)
            handler.close()


def test_logging_setup_is_idempotent_and_redacts_disk_log(
    monkeypatch,
    tmp_path,
) -> None:
    root = logging.getLogger()
    original_level = root.level
    _remove_atlas_handlers()
    monkeypatch.setattr(logging_setup, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(
        logging_setup,
        "restrict_path_to_owner",
        lambda *_args, **_kwargs: None,
    )
    try:
        first = logging_setup.configure_atlas_logging()
        second = logging_setup.configure_atlas_logging()
        assert first == second
        assert (
            sum(
                bool(getattr(handler, "_atlas_file_handler", False))
                for handler in root.handlers
            )
            == 1
        )

        logging.info("[TEST] activity password=do-not-write")
        for handler in root.handlers:
            if getattr(handler, "_atlas_file_handler", False):
                handler.flush()
        contents = first.read_text(encoding="utf-8")
        assert "[TEST] activity" in contents
        assert "do-not-write" not in contents
        assert "password=[REDACTED]" in contents
    finally:
        _remove_atlas_handlers()
        root.setLevel(original_level)


def test_development_gui_entry_point_configures_rotating_log() -> None:
    source = inspect.getsource(gui4_0.main)
    assert "configure_atlas_logging" in source
    assert "Log file:" in source


def test_gui_handler_install_preserves_events_in_active_file(
    monkeypatch,
    tmp_path,
) -> None:
    root = logging.getLogger()
    original_level = root.level
    _remove_atlas_handlers()
    monkeypatch.setattr(logging_setup, "get_logs_dir", lambda: tmp_path)
    monkeypatch.setattr(
        logging_setup,
        "restrict_path_to_owner",
        lambda *_args, **_kwargs: None,
    )
    subject = SimpleNamespace(
        _activity_log_queue=Queue(),
        _activity_log_handler=None,
    )
    try:
        log_file = logging_setup.configure_atlas_logging()
        logging.info("[TEST] before GUI initialization")

        InventoryGUI._install_activity_log_handler(subject)
        logging.info("[TEST] after GUI initialization")

        for handler in root.handlers:
            if getattr(handler, "_atlas_file_handler", False):
                handler.flush()
        contents = log_file.read_text(encoding="utf-8")
        assert "[TEST] before GUI initialization" in contents
        assert "[TEST] after GUI initialization" in contents
        assert (
            sum(
                bool(getattr(handler, "_atlas_file_handler", False))
                for handler in root.handlers
            )
            == 1
        )
    finally:
        _remove_atlas_handlers()
        root.setLevel(original_level)


def test_gui_log_handler_is_worker_safe_and_redacted() -> None:
    messages: Queue[str] = Queue()
    handler = _GuiQueueLogHandler(messages)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))
    handler.addFilter(CredentialFilter())
    logger = logging.getLogger("tests.atlas.gui-queue")
    original_handlers = tuple(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        worker = threading.Thread(
            target=lambda: logger.info(
                "[WORKER] completed token=do-not-display"
            )
        )
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        message = messages.get_nowait()
        assert message == "INFO:[WORKER] completed token=[REDACTED]"

        logger.info(
            "already displayed",
            extra={"atlas_skip_gui": True},
        )
        assert messages.empty()
    finally:
        logger.handlers[:] = list(original_handlers)
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        handler.close()


def test_credential_filter_covers_cli_and_bearer_syntax() -> None:
    credential_filter = CredentialFilter()
    samples = (
        ("user create admin password cleartext", "cleartext"),
        ("snmp community public", "public"),
        ("Authorization: Bearer eyJ-secret-token", "eyJ-secret-token"),
        ("API key sk-sensitive", "sk-sensitive"),
    )
    for raw, forbidden in samples:
        record = logging.LogRecord(
            "test",
            logging.INFO,
            __file__,
            1,
            raw,
            (),
            None,
        )
        assert credential_filter.filter(record)
        rendered = record.getMessage()
        assert forbidden not in rendered
        assert "[REDACTED]" in rendered

    formatted = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "password %s",
        ("formatted-secret",),
        None,
    )
    assert credential_filter.filter(formatted)
    assert formatted.getMessage() == "password=[REDACTED]"


def test_route_fiber_activity_is_not_mistaken_for_a_credential() -> None:
    source = inspect.getsource(
        rls_route_frame.RlsRouteFrame._apply_route_native_fiber_type
    )
    assert "native fiber type across all route " in source
    assert "native fiber token to all route paths" not in source

    message = (
        "Applied one operator-confirmed native fiber type across all route "
        "paths; paths=15."
    )
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        message,
        (),
        None,
    )
    assert CredentialFilter().filter(record)
    assert record.getMessage() == message
    assert "[REDACTED]" not in record.getMessage()


def test_inventory_queue_log_event_uses_persistent_activity_bridge() -> None:
    events: list[tuple[object, int]] = []
    queue: Queue[tuple[str, str]] = Queue()
    queue.put(("log", "[INVENTORY] one durable breadcrumb"))
    subject = SimpleNamespace(
        run_queue=queue,
        run_future=None,
        export_future=None,
        log_activity=lambda message, level=logging.INFO: events.append(
            (message, level)
        ),
    )

    InventoryGUI.poll_run_queue(subject)

    assert events == [
        ("[INVENTORY] one durable breadcrumb", logging.INFO)
    ]


def test_rls_r40_config_log_uses_controller_activity_bridge() -> None:
    events: list[tuple[str, int]] = []
    subject = SimpleNamespace(
        controller=SimpleNamespace(
            log_activity=lambda message, level=logging.INFO: events.append(
                (message, level)
            )
        )
    )

    _log_r40_event(
        subject,
        "[RLS R4.0 CONFIG] validation failed",
        logging.WARNING,
    )

    assert events == [
        ("[RLS R4.0 CONFIG] validation failed", logging.WARNING)
    ]


def test_route_worker_failure_preserves_traceback(caplog) -> None:
    results: Queue[object] = Queue()

    def fail() -> None:
        raise RuntimeError("synthetic route worker failure")

    with caplog.at_level(logging.ERROR, logger=rls_route_frame.LOGGER.name):
        rls_route_frame._run_background_job(
            results,
            kind="preview",
            generation=1,
            fingerprint="abc",
            work=fail,
        )

    result = results.get_nowait()
    assert isinstance(result.error, RuntimeError)
    records = [
        record
        for record in caplog.records
        if "Background preview task failed" in record.getMessage()
    ]
    assert len(records) == 1
    assert records[0].exc_info is not None
