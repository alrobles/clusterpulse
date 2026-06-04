"""ClusterPulse — Hermes integration tool.

Queries the aggregator API and prints a formatted cluster status table.
Usage: python3 hermes/cluster_query.py [--json]
"""
import json
import sys
import urllib.request
from datetime import datetime

AGGREGATOR_URL = "http://127.0.0.1:9200/api/status"

# ANSI colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"
DIM = "\033[2m"


def color_for(val, threshold_warn=50, threshold_crit=80):
    if val is None:
        return DIM
    if val > threshold_crit:
        return RED
    if val > threshold_warn:
        return YELLOW
    return GREEN


def status_indicator(node_data):
    if node_data.get("error"):
        return f"{RED}● OFFLINE{RESET}"
    vals = [
        node_data.get("cpu_percent"),
        node_data.get("ram_percent"),
        node_data.get("disk_percent"),
    ]
    filtered = [v for v in vals if v is not None]
    if any(v > 80 for v in filtered):
        return f"{YELLOW}● WARN{RESET}"
    return f"{GREEN}● OK{RESET}"


def fetch_status():
    try:
        with urllib.request.urlopen(AGGREGATOR_URL, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"{RED}✗ Cannot reach aggregator at {AGGREGATOR_URL}: {e}{RESET}")
        sys.exit(1)


def print_table(data):
    nodes = data.get("nodes", {})
    timestamp = data.get("timestamp", "unknown")

    # Header
    print(f"\n{BOLD}{CYAN}⚡ ClusterPulse — reumanlab Cluster{RESET}")
    print(f"{DIM}   Updated: {timestamp}{RESET}")
    print()

    # Summary
    online = sum(1 for n in nodes.values() if not n.get("error"))
    total = len(nodes)
    total_cpu = 0
    for name in nodes:
        n = nodes[name]
        if not n.get("error") and n.get("cpu_percent") is not None:
            total_cpu += n.get("cpu_percent", 0) / 100 * (
                {"reumanlab": 22, "reumanlab-beta": 8, "reumanlab-terminal": 8}.get(name, 0)
            )

    print(f"  Nodes: {GREEN}{online}{RESET}/{total} online  |  "
          f"Poll interval: {data.get('poll_interval_seconds', '?')}s")
    print()

    # Table header
    header = f"  {'NODE':<20} {'CPU%':>6} {'RAM%':>6} {'DISK%':>6} {'GPU%':>6} {'STATUS'}"
    print(header)
    print(f"  {'─'*58}")

    # Rows
    for name in ["reumanlab", "reumanlab-beta", "reumanlab-terminal"]:
        node = nodes.get(name, {"error": "unknown"})
        if node.get("error"):
            row = (
                f"  {DIM}{name:<20}{RESET} "
                f"{DIM}{'—':>6}{RESET} "
                f"{DIM}{'—':>6}{RESET} "
                f"{DIM}{'—':>6}{RESET} "
                f"{DIM}{'—':>6}{RESET} "
                f"{status_indicator(node)}"
            )
        else:
            cpu = node.get("cpu_percent")
            ram = node.get("ram_percent")
            disk = node.get("disk_percent")
            gpu = node.get("gpu_percent")

            row = (
                f"  {name:<20} "
                f"{color_for(cpu)}{cpu if cpu is not None else '—':>6.1f}{RESET} "
                f"{color_for(ram)}{ram if ram is not None else '—':>6.1f}{RESET} "
                f"{color_for(disk)}{disk if disk is not None else '—':>6.1f}{RESET} "
                f"{color_for(gpu)}{gpu if gpu is not None else '—':>6.1f}{RESET} "
                f"{status_indicator(node)}"
            )
        print(row)

    print()


def main():
    if "--json" in sys.argv:
        data = fetch_status()
        print(json.dumps(data, indent=2))
    else:
        data = fetch_status()
        print_table(data)


if __name__ == "__main__":
    main()
