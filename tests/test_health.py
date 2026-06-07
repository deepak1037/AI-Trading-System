"""Tests for health.py endpoint (Days 26-30)."""

from __future__ import annotations

import json
import socket
import time

import pytest


class TestHealthServer:
    def _find_free_port(self) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_health_endpoint_returns_200(self):
        from health import HealthServer

        port = self._find_free_port()
        server = HealthServer(host="127.0.0.1", port=port)
        server.start()
        time.sleep(0.1)

        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                assert resp.status == 200
                body = json.loads(resp.read())
                assert body["status"] == "ok"
                assert "env" in body
                assert "uptime_seconds" in body
        finally:
            server.stop()

    def test_health_endpoint_json_fields(self):
        from health import HealthServer

        port = self._find_free_port()
        server = HealthServer(host="127.0.0.1", port=port)
        server.start()
        time.sleep(0.1)

        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                body = json.loads(resp.read())
                for field in ["status", "env", "broker", "dry_run", "live_trading_enabled", "uptime_seconds", "timestamp"]:
                    assert field in body, f"Missing field: {field}"
        finally:
            server.stop()

    def test_unknown_path_returns_404(self):
        from health import HealthServer

        port = self._find_free_port()
        server = HealthServer(host="127.0.0.1", port=port)
        server.start()
        time.sleep(0.1)

        try:
            import urllib.request
            import urllib.error
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/unknown", timeout=2)
            assert exc_info.value.code == 404
        finally:
            server.stop()
