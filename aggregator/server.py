"""
ClusterPulse Aggregator — central server that polls all 3 collectors
via Tailscale IPs and exposes a unified status API.

Run with: uvicorn aggregator.server:app --host 127.0.0.1 --port 9200
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
import yaml
from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES_YAML = os.path.join(REPO_ROOT, "nodes.yaml")

POLL_INTERVAL = 5          # seconds between polls
REQUEST_TIMEOUT = 3        # seconds per node before marking unreachable
COLLECTOR_PORT = 9100
COLLECTOR_PATH = "/metrics"

# ---------------------------------------------------------------------------
# Load node metadata
# ---------------------------------------------------------------------------
with open(NODES_YAML) as f:
    _data = yaml.safe_load(f)

NODES = _data["nodes"]  # dict[str, dict] — name -> {tailscale_ip, cores, ram_gb, gpu}

# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------
_cache: dict = {
    "nodes": {name: {"error": "not_yet_polled"} for name in NODES},
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "poll_interval_seconds": POLL_INTERVAL,
}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


async def _poll_node(name: str, ip: str) -> tuple[str, dict]:
    """Poll one collector, returning (name, metrics_dict)."""
    url = f"http://{ip}:{COLLECTOR_PORT}{COLLECTOR_PATH}"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            return name, data
    except Exception:
        return name, {"error": "unreachable"}


async def _poll_all() -> dict:
    """Poll every node concurrently and return the fresh cache dict."""
    now = datetime.now(timezone.utc).isoformat()

    tasks = [
        _poll_node(name, meta["tailscale_ip"])
        for name, meta in NODES.items()
    ]

    results = await asyncio.gather(*tasks)

    node_results = {}
    for name, metrics in results:
        node_results[name] = metrics

    return {
        "nodes": node_results,
        "timestamp": now,
        "poll_interval_seconds": POLL_INTERVAL,
    }


async def _polling_loop():
    """Background task: poll collectors every POLL_INTERVAL seconds."""
    while True:
        try:
            _cache.update(await _poll_all())
        except Exception:
            pass  # keep last good cache on total failure
        await asyncio.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start background polling on startup, cancel on shutdown."""
    task = asyncio.create_task(_polling_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="ClusterPulse Aggregator", version="0.1.0", lifespan=lifespan)


@app.get("/api/status")
async def status():
    """Return the cached cluster-wide status."""
    return _cache
