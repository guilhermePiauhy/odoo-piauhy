"""
libs/healthcheck/healthcheck.py

Production-ready HTTP healthcheck server for long-running Python services.
Exposes a /health endpoint on a configurable port using only the standard library.
Designed to be run in a background daemon thread.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

_DEFAULT_HOST = os.getenv("HEALTHCHECK_HOST", "0.0.0.0")
_DEFAULT_PORT = int(os.getenv("HEALTHCHECK_PORT", "8080"))
_DEFAULT_PATH = os.getenv("HEALTHCHECK_PATH", "/health")


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

class _HealthState:
    """Thread-safe container for the current health status and metadata."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: str = "starting"
        self._details: Dict[str, Any] = {}
        self._started_at: float = time.time()
        self._custom_checks: Dict[str, Callable[[], Dict[str, Any]]] = {}

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def set_status(self, status: str, details: Optional[Dict[str, Any]] = None) -> None:
        """Update the global health status.

        Args:
            status: One of ``"healthy"``, ``"unhealthy"``, ``"degraded"``,
                    ``"starting"``, or any custom string.
            details: Optional dictionary with extra context to expose.
        """
        if not isinstance(status, str):
            raise TypeError(f"status must be a str, got {type(status)!r}")
        with self._lock:
            self._status = status
            if details is not None:
                self._details = dict(details)

    def set_details(self, details: Dict[str, Any]) -> None:
        """Replace the details dictionary."""
        with self._lock:
            self._details = dict(details)

    def register_check(self, name: str, fn: Callable[[], Dict[str, Any]]) -> None:
        """Register a named callable that returns a dict with check results.

        The callable is invoked on every request to ``/health``.  It must be
        thread-safe and return a plain ``dict``.

        Args:
            name: Unique identifier for the check.
            fn:   Zero-argument callable returning ``{"status": ..., ...}``.
        """
        with self._lock:
            self._custom_checks[name] = fn

    def unregister_check(self, name: str) -> None:
        """Remove a previously registered check."""
        with self._lock:
            self._custom_checks.pop(name, None)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def snapshot(self) -> Dict[str, Any]:
        """Return a consistent snapshot of the current health payload."""
        with self._lock:
            checks: Dict[str, Any] = {}
            overall_ok = self._status in {"healthy", "degraded", "starting"}
            for check_name, fn in self._custom_checks.items():
                try:
                    result = fn()
                    if not isinstance(result, dict):
                        result = {"status": "unknown", "raw": str(result)}
                    checks[check_name] = result
                    if result.get("status") not in {"healthy", "ok", "pass", True}:
                        overall_ok = False
                except Exception as exc:  # noqa: BLE001
                    checks[check_name] = {"status": "error", "error": str(exc)}
                    overall_ok = False

            return {
                "status": self._status,
                "uptime_seconds": round(time.time() - self._started_at, 2),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "details": dict(self._details),
                "checks": checks,
            }


# Module-level singleton so callers can import and mutate it directly.
_state = _HealthState()


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal HTTP request handler that serves ``/health``."""

    # Suppress default access log noise; we provide our own.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401
        pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]

        if path == self.server.health_path:
            self._serve_health()
        elif path in {"/", "/ping"}:
            self._respond(200, b"pong")
        else:
            self._respond(404, b"Not Found")

    def _serve_health(self) -> None:
        payload = _state.snapshot()
        status_code = 200 if payload["status"] in {"healthy", "starting", "degraded"} else 503
        body = json.dumps(payload, default=str).encode("utf-8")
        self._respond(status_code, body, content_type="application/json")
        logger.debug(
            "Healthcheck request from %s — HTTP %s status=%s",
            self.client_address[0],
            status_code,
            payload["status"],
        )

    def _respond(
        self,
        code: int,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Server wrapper
# ---------------------------------------------------------------------------

class HealthcheckServer:
    """Background HTTP server that exposes a ``/health`` endpoint.

    Usage::

        server = HealthcheckServer(port=8080)
        server.start()

        # Mark service as healthy once initialisation completes:
        server.set_healthy({"version": "1.0.0", "env": "production"})

        # Register a custom check (e.g. database connectivity):
        def db_check():
            ok = ping_database()
            return {"status": "healthy" if ok else "unhealthy"}

        server.register_check("database", db_check)

        # On shutdown:
        server.stop()
    """

    def __init__(
        self,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        path: str = _DEFAULT_PATH,
    ) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> "HealthcheckServer":
        """Start the healthcheck HTTP server in a background daemon thread.

        Returns:
            self  (allows chaining: ``HealthcheckServer().start()``)

        Raises:
            RuntimeError: If the server is already running.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("HealthcheckServer is already running.")

        self._stop_event.clear()
        self._server = HTTPServer((self._host, self._port), _HealthHandler)
        self._server.health_path = self._path  # type: ignore[attr-defined]
        # Allow quick restart after crash / container recycle.
        self._server.allow_reuse_address = True
        self._server.timeout = 1.0

        self._thread = threading.Thread(
            target=self._serve_forever,
            name="healthcheck-server