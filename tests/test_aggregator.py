"""
Tests for the ClusterPulse Aggregator server.

These tests mock the external HTTP calls to the collectors so they run
without actual Tailscale connectivity.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

# Patch POLL_INTERVAL to be fast in tests
import aggregator.server as server

server.POLL_INTERVAL = 1  # fast polling for tests

from aggregator.server import app, NODES

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the in-memory cache before each test."""
    server._cache = {
        "nodes": {name: {"error": "not_yet_polled"} for name in NODES},
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "poll_interval_seconds": server.POLL_INTERVAL,
    }
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_successful_responses(*, fail: set[str] | None = None):
    """Return an async context manager that patches httpx.AsyncClient so all
    configured nodes return realistic metrics, except those listed in *fail*
    which raise a timeout."""

    fail = fail or set()

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url):
            # Determine which node this URL belongs to
            for name, meta in NODES.items():
                if meta["tailscale_ip"] in url:
                    if name in fail:
                        raise httpx.ConnectError("unreachable")
                    return _mock_response(name)
            raise httpx.ConnectError("unknown node")

        def __call__(self, *args, **kwargs):
            # Support being used as context manager directly
            return self

    return patch.object(httpx, "AsyncClient", return_value=MockAsyncClient())


def _mock_response(node_name: str):
    """Return a realistic httpx.Response for a collector."""

    payloads = {
        "reumanlab": {
            "cpu_percent": 12.5,
            "ram_percent": 45.2,
            "ram_used_gb": 27.9,
            "disk_percent": 33.0,
            "gpu_util_percent": 8.0,
            "gpu_mem_percent": 15.0,
            "uptime_seconds": 604800,
        },
        "reumanlab-beta": {
            "cpu_percent": 3.1,
            "ram_percent": 22.8,
            "ram_used_gb": 1.7,
            "disk_percent": 12.0,
            "uptime_seconds": 1209600,
        },
        "reumanlab-terminal": {
            "cpu_percent": 8.3,
            "ram_percent": 55.0,
            "ram_used_gb": 8.2,
            "disk_percent": 40.0,
            "uptime_seconds": 86400,
        },
    }

    payload = payloads.get(node_name, {"cpu_percent": 0.0})
    ip = NODES[node_name]["tailscale_ip"]
    url = f"http://{ip}:9100/metrics"

    request = httpx.Request("GET", url)
    resp = httpx.Response(200, json=payload, request=request)
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAggregatorAPI:
    """Tests for the /api/status endpoint."""

    def test_status_returns_expected_keys(self):
        """The response contains the top-level keys: nodes, timestamp,
        poll_interval_seconds."""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "timestamp" in data
        assert "poll_interval_seconds" in data

    def test_nodes_returns_all_three_entries(self):
        """All three nodes appear in the response."""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        nodes = resp.json()["nodes"]
        assert set(nodes.keys()) == {"reumanlab", "reumanlab-beta", "reumanlab-terminal"}

    def test_poll_interval_is_correct(self):
        """poll_interval_seconds matches the configured value."""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["poll_interval_seconds"] == server.POLL_INTERVAL

    def test_timestamp_is_isoformat(self):
        """The timestamp is a valid ISO-8601 string."""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        ts = resp.json()["timestamp"]
        # Try parsing it
        datetime.fromisoformat(ts)

    def _poll_and_cache(self):
        """Run a poll cycle and write results into the in-memory cache."""
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(server._poll_all())
        server._cache.update(result)

    def test_all_nodes_reachable(self):
        """When all collectors respond, each node has metrics (no 'error' key)."""
        with _mock_successful_responses():
            self._poll_and_cache()

        resp = client.get("/api/status")
        assert resp.status_code == 200
        nodes = resp.json()["nodes"]
        for name in NODES:
            assert "error" not in nodes[name], f"{name} should not have error"
            assert "cpu_percent" in nodes[name], f"{name} should have cpu_percent"
            assert isinstance(nodes[name]["cpu_percent"], (int, float))

    def test_one_node_unreachable(self):
        """When a specific node is unreachable, it gets an 'error' key."""
        with _mock_successful_responses(fail={"reumanlab-beta"}):
            self._poll_and_cache()

        resp = client.get("/api/status")
        assert resp.status_code == 200
        nodes = resp.json()["nodes"]

        # reumanlab and reumanlab-terminal should be fine
        assert "error" not in nodes["reumanlab"], f"reumanlab: {nodes['reumanlab']}"
        assert "error" not in nodes["reumanlab-terminal"], f"reumanlab-terminal: {nodes['reumanlab-terminal']}"

        # reumanlab-beta should be unreachable
        assert nodes["reumanlab-beta"].get("error") == "unreachable"

    def test_all_nodes_unreachable(self):
        """When all collectors are down, every node has an error."""
        with _mock_successful_responses(fail=set(NODES.keys())):
            self._poll_and_cache()

        resp = client.get("/api/status")
        assert resp.status_code == 200
        nodes = resp.json()["nodes"]
        for name in NODES:
            assert nodes[name].get("error") == "unreachable", f"{name} should be unreachable"

    def test_metadata_in_response(self):
        """Node metadata (cores, ram_gb, gpu) is available from nodes.yaml
        and can be cross-referenced. This test verifies the config loaded
        correctly."""
        # Verify the NODES dict loaded from nodes.yaml has metadata
        for name, meta in NODES.items():
            assert "tailscale_ip" in meta
            assert "cores" in meta
            assert "ram_gb" in meta
            assert "gpu" in meta  # may be None

        # Specific checks from nodes.yaml
        assert NODES["reumanlab"]["cores"] == 22
        assert NODES["reumanlab"]["ram_gb"] == 62
        assert NODES["reumanlab"]["gpu"] == "NVIDIA RTX 2000 Ada 8GB"
        assert NODES["reumanlab-beta"]["gpu"] is None
        assert NODES["reumanlab-terminal"]["gpu"] is None

    def test_initial_cache_state(self):
        """Before any polling, the cache shows 'not_yet_polled' for all nodes."""
        resp = client.get("/api/status")
        assert resp.status_code == 200
        nodes = resp.json()["nodes"]
        for name in NODES:
            assert nodes[name] == {"error": "not_yet_polled"}

    def test_response_is_json(self):
        """The endpoint returns content-type application/json."""
        resp = client.get("/api/status")
        assert resp.headers["content-type"] == "application/json"
