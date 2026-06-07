"""Health check endpoint for UptimeRobot (Days 26-30).

Run standalone:
    python health.py

Or import into main.py to run alongside the trading system.
Exposes GET /health returning 200 OK with system status JSON.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from datetime import datetime, timezone

from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

_START_TIME = datetime.now(tz=timezone.utc)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/health":
            self._respond_health()
        else:
            self.send_response(404)
            self.end_headers()

    def _respond_health(self) -> None:
        uptime_s = (datetime.now(tz=timezone.utc) - _START_TIME).total_seconds()
        status = {
            "status": "ok",
            "env": settings.ENV,
            "broker": settings.BROKER,
            "dry_run": settings.DRY_RUN,
            "live_trading_enabled": settings.LIVE_TRADING_ENABLED,
            "uptime_seconds": int(uptime_s),
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        }
        body = json.dumps(status).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("HealthCheck: %s", format % args)


class HealthServer:
    """Lightweight health check HTTP server, runs in a background thread."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8080) -> None:
        self._host = host
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        self._server = HTTPServer((self._host, self._port), HealthHandler)
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("HealthServer listening on %s:%d/health", self._host, self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            logger.info("HealthServer stopped")


if __name__ == "__main__":
    server = HealthServer()
    server.start()
    import time
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        server.stop()
