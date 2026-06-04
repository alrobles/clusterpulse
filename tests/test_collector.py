"""Tests for the ClusterPulse collector agent."""

import json
import socket
import threading
import time
from datetime import datetime, timezone

import pytest
import requests

from collector.collector import get_metrics, start_http_server, METRICS_PORT, METRICS_HOST

EXPECTED_KEYS = {
    "cpu_percent", "ram_percent", "disk_percent",
    "gpu_percent", "gpu_name", "gpu_memory_used_mb", "gpu_memory_total_mb",
    "hostname", "timestamp",
}


class TestGetMetrics:
    """Tests for the get_metrics() function."""

    def test_returns_dict_with_all_keys(self):
        """get_metrics() should return a dict containing all expected keys."""
        metrics = get_metrics()
        assert isinstance(metrics, dict)
        missing = EXPECTED_KEYS - set(metrics.keys())
        assert not missing, f"Missing keys: {missing}"

    def test_cpu_ram_disk_are_floats(self):
        """CPU, RAM, and disk percentages should be floats between 0 and 100."""
        metrics = get_metrics()
        for key in ("cpu_percent", "ram_percent", "disk_percent"):
            val = metrics[key]
            assert isinstance(val, float), f"{key} should be float, got {type(val)}"
            assert 0.0 <= val <= 100.0, f"{key} should be 0-100, got {val}"

    def test_hostname_is_nonempty_string(self):
        """hostname should be a non-empty string."""
        metrics = get_metrics()
        assert isinstance(metrics["hostname"], str)
        assert len(metrics["hostname"]) > 0

    def test_timestamp_is_iso8601(self):
        """timestamp should be a valid ISO 8601 string."""
        metrics = get_metrics()
        ts = metrics["timestamp"]
        assert isinstance(ts, str)
        # Parse it back — should succeed
        parsed = datetime.fromisoformat(ts)
        assert parsed.tzinfo is not None, "timestamp should be timezone-aware"

    def test_gpu_graceful_when_no_gpu(self):
        """GPU fields should be None if no GPU is available."""
        metrics = get_metrics()
        gpu_keys = ["gpu_percent", "gpu_name", "gpu_memory_used_mb", "gpu_memory_total_mb"]
        for key in gpu_keys:
            # Either null (no GPU) or valid type (GPU present)
            assert metrics[key] is None or isinstance(metrics[key], (float, int, str))

    def test_output_serializable(self):
        """The dict should be JSON-serializable without errors."""
        metrics = get_metrics()
        dumped = json.dumps(metrics)
        assert isinstance(dumped, str)
        # Round-trip
        loaded = json.loads(dumped)
        assert loaded == metrics

    def test_ram_disk_keys_match_expected(self):
        """RAM and disk should be reported as percentages."""
        metrics = get_metrics()
        # ram_percent and disk_percent both represent percentages
        for key in ("ram_percent", "disk_percent"):
            val = metrics[key]
            assert isinstance(val, float), f"{key} should be float"
            assert 0.0 <= val <= 100.0, f"{key} out of range"


class TestHTTPServer:
    """Tests for the HTTP server on port 9100."""

    @pytest.fixture(autouse=True)
    def _server(self):
        """Start the HTTP server in a background thread for testing."""
        server, thread = start_http_server()
        # Give it a moment to bind
        time.sleep(0.5)
        yield
        server.shutdown()
        thread.join(timeout=2)

    def test_metrics_endpoint_returns_200(self):
        """GET /metrics should return HTTP 200."""
        resp = requests.get(f"http://{METRICS_HOST}:{METRICS_PORT}/metrics", timeout=5)
        assert resp.status_code == 200

    def test_metrics_endpoint_returns_json(self):
        """GET /metrics should return a valid JSON body."""
        resp = requests.get(f"http://{METRICS_HOST}:{METRICS_PORT}/metrics", timeout=5)
        assert resp.headers.get("Content-Type", "").startswith("application/json")
        data = resp.json()
        assert isinstance(data, dict)
        assert "cpu_percent" in data
        assert "timestamp" in data

    def test_metrics_endpoint_numeric_values(self):
        """All numeric values in the JSON response should be valid."""
        resp = requests.get(f"http://{METRICS_HOST}:{METRICS_PORT}/metrics", timeout=5)
        data = resp.json()
        # CPU, RAM, disk should be floats
        for field in ("cpu_percent", "ram_percent", "disk_percent"):
            assert isinstance(data[field], float), f"{field} must be float"

    def test_root_returns_404(self):
        """GET / (without /metrics) should return 404."""
        resp = requests.get(f"http://{METRICS_HOST}:{METRICS_PORT}/", timeout=5)
        assert resp.status_code == 404
