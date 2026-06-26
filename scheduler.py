"""Simple scheduler for periodic collection and validation."""

from __future__ import annotations

import logging
import signal
import threading
import time

log = logging.getLogger(__name__)


class Scheduler:
    """Run a callback at a fixed interval until stopped."""

    def __init__(self, interval_seconds: float, callback, name: str = "scheduler"):
        self.interval = interval_seconds
        self.callback = callback
        self.name = name
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        log.info("Starting %s (every %ds)", self.name, int(self.interval))
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name=self.name)
        self._thread.start()

    def stop(self) -> None:
        log.info("Stopping %s", self.name)
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.callback()
            except Exception as e:
                log.error("%s callback error: %s", self.name, e)
            self._stop_event.wait(self.interval)


def run_daemon(collect_and_write_fn, validate_fn, cleanup_fn, config: dict) -> None:
    """Run the main daemon loop with collection and validation schedulers."""
    sched_cfg = config.get("scheduler", {})
    refresh_h = sched_cfg.get("refresh_interval_hours", 4)
    validate_h = sched_cfg.get("validate_interval_hours", 1)
    do_cleanup = sched_cfg.get("cleanup_expired", True)
    max_fail = sched_cfg.get("max_consecutive_failures", 3)

    log.info("=" * 60)
    log.info("Free Token API Collector — daemon mode")
    log.info("  Refresh every %dh, Validate every %dh", refresh_h, validate_h)
    log.info("=" * 60)

    # Run initial collection
    log.info("--- Initial collection ---")
    collect_and_write_fn()

    if do_cleanup:
        log.info("--- Initial cleanup ---")
        cleanup_fn(max_fail)

    # Start schedulers
    refresh_sched = Scheduler(refresh_h * 3600, collect_and_write_fn, "refresh")
    validate_sched = Scheduler(validate_h * 3600, validate_fn, "validate")

    refresh_sched.start()
    validate_sched.start()

    # Wait for Ctrl+C
    stop_event = threading.Event()

    def _signal_handler(sig, frame):
        log.info("Received signal %s, shutting down...", sig)
        stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        while not stop_event.is_set():
            stop_event.wait(1)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt, shutting down...")
    finally:
        refresh_sched.stop()
        validate_sched.stop()
        log.info("Daemon stopped.")
