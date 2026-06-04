"""ClusterPulse Collector — node-local metrics agent.

Reads CPU, RAM, disk, and GPU metrics, then serves them as JSON
on http://127.0.0.1:9100/metrics.

Usage:
    python3 collector/collector.py
"""

import json
import os
import platform
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import psutil

METRICS_HOST = os.environ.get("CLUSTERPULSE_HOST", "0.0.0.0")
METRICS_PORT = 9100


def _get_gpu_metrics() -> dict:
    """Read GPU metrics via GPUtil or fallback to nvidia-smi subprocess.

    Returns a dict with keys:
        gpu_percent, gpu_name, gpu_memory_used_mb, gpu_memory_total_mb
    All values are None if no GPU is found.
    """
    # Default: no GPU
    result = {
        "gpu_percent": None,
        "gpu_name": None,
        "gpu_memory_used_mb": None,
        "gpu_memory_total_mb": None,
    }

    # Try GPUtil first (preferred)
    try:
        import GPUtil

        gpus = GPUtil.getGPUs()
        if gpus:
            gpu = gpus[0]  # Primary GPU
            result["gpu_percent"] = round(gpu.load * 100, 1)
            result["gpu_name"] = gpu.name
            result["gpu_memory_used_mb"] = int(gpu.memoryUsed)
            result["gpu_memory_total_mb"] = int(gpu.memoryTotal)
            return result
    except (ImportError, Exception):
        pass

    # Fallback: nvidia-smi subprocess
    try:
        nvidia_smi = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if nvidia_smi.returncode == 0 and nvidia_smi.stdout.strip():
            parts = nvidia_smi.stdout.strip().split(", ")
            if len(parts) >= 4:
                result["gpu_percent"] = round(float(parts[0]), 1)
                result["gpu_name"] = parts[3]
                result["gpu_memory_used_mb"] = int(float(parts[1]))
                result["gpu_memory_total_mb"] = int(float(parts[2]))
                return result
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, Exception):
        pass

    return result


def get_metrics() -> dict:
    """Collect system metrics from the local node.

    Returns a dictionary with:
        cpu_percent      : float  — CPU utilization percentage (0–100)
        ram_percent      : float  — RAM utilization percentage (0–100)
        disk_percent     : float  — disk utilization percentage (0–100)
        gpu_percent      : float | None
        gpu_name         : str   | None
        gpu_memory_used_mb : int | None
        gpu_memory_total_mb: int | None
        hostname         : str
        timestamp        : str   — ISO 8601 with timezone
    """
    cpu_percent = psutil.cpu_percent(interval=0.5)
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.sep)

    gpu = _get_gpu_metrics()

    return {
        "cpu_percent": round(cpu_percent, 1),
        "ram_percent": round(ram.percent, 1),
        "disk_percent": round(disk.percent, 1),
        "gpu_percent": gpu["gpu_percent"],
        "gpu_name": gpu["gpu_name"],
        "gpu_memory_used_mb": gpu["gpu_memory_used_mb"],
        "gpu_memory_total_mb": gpu["gpu_memory_total_mb"],
        "hostname": platform.node() or socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP request handler that serves JSON metrics on /metrics."""

    def do_GET(self):
        if self.path == "/metrics":
            try:
                metrics = get_metrics()
                body = json.dumps(metrics, indent=2).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                error_body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(error_body)))
                self.end_headers()
                self.wfile.write(error_body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        """Suppress default HTTP server logging."""
        pass


def start_http_server():
    """Start the metrics HTTP server and return (server, thread).

    The server runs on METRICS_HOST:METRICS_PORT in a daemon thread.
    Returns:
        tuple: (HTTPServer, threading.Thread)
    """
    server = HTTPServer((METRICS_HOST, METRICS_PORT), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main():
    """Entry point: start the HTTP server and block."""
    print(f"Starting ClusterPulse Collector on {METRICS_HOST}:{METRICS_PORT}/metrics")
    server, _ = start_http_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
