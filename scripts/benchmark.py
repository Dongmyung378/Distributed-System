"""Comparable real-socket measurements. Uses only Python's standard library.

Run this same harness against an initial checkout and the current source.
Timings are loopback observations, not internet or production capacity claims.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import json
import math
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def peak_rss_mib():
    """Peak memory of the complete harness process, including clients."""
    if os.name != "nt":
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(value / (1024 * 1024 if sys.platform == "darwin" else 1024), 3)
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in [
                "peak_working",
                "working",
                "peak_paged",
                "paged",
                "peak_nonpaged",
                "nonpaged",
                "pagefile",
                "peak_pagefile",
            ]
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    function = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not function(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        return None
    return round(counters.peak_working / 1024 / 1024, 3)


def percentile(values, fraction):
    ordered = sorted(values)
    return (
        round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)
        if ordered
        else None
    )


class Peer:
    def __init__(self, address, name, stalled=False):
        self.socket = socket.socket()
        self.socket.settimeout(3)
        if stalled:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        self.socket.connect(address)
        self.buffer = b""
        self.send(f"register {name}")
        if self.line() != f"{name} registered":
            raise RuntimeError("Registration failed")
        self.socket.settimeout(0.2)
        self.received = {}
        self.duplicate = 0
        self.errors = []
        self.stop = threading.Event()
        self.thread = None

    def send(self, text):
        self.socket.sendall((text + "\n").encode())

    def line(self):
        while b"\n" not in self.buffer:
            block = self.socket.recv(65536)
            if not block:
                raise EOFError()
            self.buffer += block
        line, self.buffer = self.buffer.split(b"\n", 1)
        return line.decode("utf-8").rstrip("\r")

    def start(self):
        def receive():
            while not self.stop.is_set():
                try:
                    line = self.line()
                    prefix = "message from bench-0: "
                    if not line.startswith(prefix):
                        self.errors.append("unexpected protocol response")
                        continue
                    message_id = line[len(prefix) :].split(" ", 1)[0]
                    if message_id in self.received:
                        self.duplicate += 1
                    self.received[message_id] = time.perf_counter()
                except TimeoutError:
                    continue
                except (OSError, EOFError):
                    break

        self.thread = threading.Thread(target=receive, daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        with contextlib.suppress(OSError):
            self.socket.shutdown(socket.SHUT_RDWR)
        self.socket.close()
        if self.thread:
            self.thread.join(2)


def run_trial(project, scenario, clients, trial):
    sys.path.insert(0, str(Path(project) / "Talk"))
    module = importlib.import_module("myserver")
    source_sha256 = hashlib.sha256(
        Path(module.__file__).read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()
    # Suppress console message contents in both versions, avoiding terminal I/O effects.
    module.print = lambda *args, **kwargs: None
    # Keep each version's published defaults, including the final queue/deadline.
    server = module.create_server("127.0.0.1", 0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    peers = []
    stalled = None
    starts = {}
    submitted = set()
    writer = None
    writer_error = []
    try:
        if scenario == "slow":
            stalled = Peer(server.server_address, "stalled", stalled=True)
            # Apply the same small server send buffer to both versions.
            for handler in server.state.recipients():
                handler.request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        for index in range(clients):
            peer = Peer(server.server_address, f"bench-{index}")
            peers.append(peer)
            peer.start()
        count, warmups = (300, 0) if scenario == "slow" else (20, 5)
        payload = "x" * (4096 if scenario == "slow" else 256)

        def produce():
            try:
                for index in range(-warmups, count):
                    message_id = f"m{index:05d}"
                    starts[message_id] = time.perf_counter()
                    peers[0].send(f"send_all {message_id} {payload}")
                    submitted.add(message_id)
                    if scenario == "slow":
                        time.sleep(0.005)
                    else:
                        deadline = time.monotonic() + 3
                        while not all(message_id in p.received for p in peers):
                            if time.monotonic() >= deadline:
                                raise TimeoutError("Delivery deadline")
                            time.sleep(0.0005)
            except (OSError, TimeoutError) as error:
                writer_error.append(type(error).__name__)

        began = time.perf_counter()
        cpu_began = time.process_time()
        peak_threads = threading.active_count()
        writer = threading.Thread(target=produce, daemon=True)
        writer.start()
        deadline = time.monotonic() + (6 if scenario == "slow" else 70)
        expected_ids = [f"m{i:05d}" for i in range(count)]
        while time.monotonic() < deadline:
            peak_threads = max(peak_threads, threading.active_count())
            if all(all(k in p.received for k in expected_ids) for p in peers):
                break
            if scenario != "slow" and not writer.is_alive() and writer_error:
                break
            time.sleep(0.005)
        observed = time.perf_counter()
        cpu_seconds = time.process_time() - cpu_began
        delivered = sum(sum(k in p.received for k in expected_ids) for p in peers)
        latencies = [
            (p.received[k] - starts[k]) * 1000
            for p in peers
            for k in expected_ids
            if k in p.received and k in starts
        ]
        first = starts.get("m00000", began)
        last = max(
            (p.received[k] for p in peers for k in expected_ids if k in p.received),
            default=observed,
        )
        elapsed = (
            max(last - first, 0.000001)
            if delivered == count * clients
            else observed - first
        )
        diagnostics = (
            server.diagnostics()
            if callable(getattr(server, "diagnostics", None))
            else None
        )
        return {
            "scenario": scenario,
            "server_source_sha256": source_sha256,
            "clients_receiving": clients,
            "stalled_clients": int(scenario == "slow"),
            "trial": trial,
            "warmup_messages": warmups,
            "measured_broadcasts": count,
            "payload_bytes": len(payload),
            "attempted_broadcasts": sum(k in starts for k in expected_ids),
            "submitted_broadcasts": sum(k in submitted for k in expected_ids),
            "expected_deliveries": count * clients,
            "observed_deliveries": delivered,
            "missing_deliveries": count * clients - delivered,
            "duplicate_deliveries": sum(p.duplicate for p in peers),
            "protocol_errors": sum(len(p.errors) for p in peers),
            "writer_errors": writer_error,
            "completed": delivered == count * clients,
            "elapsed_seconds": round(elapsed, 6),
            "delivered_per_second": round(delivered / max(elapsed, 0.000001), 3),
            "delivery_p50_ms": percentile(latencies, 0.5),
            "delivery_p95_ms": percentile(latencies, 0.95),
            "latency_denominator": len(latencies),
            "latencies_ms": [round(x, 6) for x in latencies],
            "trial_cpu_seconds": round(cpu_seconds, 6),
            "trial_wall_seconds": round(observed - began, 6),
            "harness_peak_threads": peak_threads,
            "harness_peak_rss_mib": peak_rss_mib(),
            "diagnostics": diagnostics,
        }
    finally:
        for peer in peers:
            peer.close()
        if stalled:
            stalled.close()
        if writer:
            writer.join(2)
        server.shutdown()
        server.server_close()
        server_thread.join(3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output", type=Path, default=Path("target/benchmark.json"))
    parser.add_argument("--label", default="working-source")
    parser.add_argument("--scenario", choices=["all", "healthy", "slow"], default="all")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--clients", type=int, default=3, help=argparse.SUPPRESS)
    parser.add_argument("--trial", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        row = run_trial(args.project, args.scenario, args.clients, args.trial)
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
        return
    if not 1 <= args.trials <= 10:
        parser.error("Use 1..10 trials")
    rows = []
    for scenario in ["healthy", "slow"] if args.scenario == "all" else [args.scenario]:
        for clients in [2, 10, 30, 60] if scenario == "healthy" else [3]:
            for trial in range(args.trials):
                command = [
                    sys.executable,
                    "-B",
                    "-X",
                    "utf8",
                    str(Path(__file__).resolve()),
                    "--worker",
                    "--project",
                    str(args.project.resolve()),
                    "--scenario",
                    scenario,
                    "--clients",
                    str(clients),
                    "--trial",
                    str(trial),
                ]
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=90,
                )
                if result.returncode:
                    raise RuntimeError(
                        f"Measurement worker failed: {result.stderr[-2000:]}"
                    )
                row = json.loads(result.stdout.strip().splitlines()[-1])
                rows.append(row)
                report = {
                    "schema": "network-systems-benchmark/v1",
                    "source": args.label,
                    "environment": {
                        "python": platform.python_version(),
                        "os": platform.system(),
                        "cpu_count": os.cpu_count(),
                    },
                    "scope": "Single-machine loopback TCP, sequential trials, same client/harness. Console content logging suppressed. Client and server share one process; not production capacity.",
                    "limits": [
                        "Slow-peer fixture uses a small receive/send buffer and 300 broadcasts at 5ms intervals.",
                        "Published defaults are retained: final queue128/send_timeout2s; the initial version has no corresponding bounds.",
                        "Percentiles describe observed deliveries; always read missing-delivery counts alongside them.",
                        "Healthy trials use5warmup+20measured broadcasts, with delivery completion before each next broadcast.",
                        "Normal concurrent desktop workloads were not excluded.",
                    ],
                    "rows": rows,
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(
                    f"Measured {scenario} clients={clients} trial={trial}: {row['observed_deliveries']}/{row['expected_deliveries']}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
