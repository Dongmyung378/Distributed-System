"""Run the actual planner and API entry points without touching project data."""

from __future__ import annotations

import configparser
import contextlib
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


class HotelCliTests(unittest.TestCase):
    def test_real_cli_match_and_reservations_persist_across_api_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            hotel = directory / "Hotel"
            hotel.mkdir()
            for source in (ROOT / "Hotel").glob("*.py"):
                shutil.copy2(source, hotel / source.name)
            shutil.copy2(ROOT / "Hotel" / "api.ini", hotel / "api.ini")
            database = directory / "state" / "reservations.db"
            journal = directory / "state" / "planner.db"
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "BOOKING_JOURNAL_PATH": str(journal),
                    "HOTEL_API_KEY": "hotel-demo-user",
                    "BAND_API_KEY": "band-demo-user",
                    "NO_PROXY": "127.0.0.1,localhost",
                    "no_proxy": "127.0.0.1,localhost",
                }
            )
            server = None

            def stop_server() -> None:
                nonlocal server
                if server is None:
                    return
                if server.poll() is None:
                    server.terminate()
                try:
                    server.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.communicate(timeout=3)
                finally:
                    server = None

            def start_server(port: int) -> int:
                nonlocal server
                server = subprocess.Popen(
                    [
                        sys.executable,
                        "-X",
                        "utf8",
                        "-B",
                        str(hotel / "local_api.py"),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                        "--database",
                        str(database),
                        "--slots",
                        "5",
                    ],
                    cwd=directory,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                deadline = time.monotonic() + 10
                with requests.Session() as session:
                    session.trust_env = False
                    while time.monotonic() < deadline:
                        if server.poll() is not None:
                            stdout, stderr = server.communicate(timeout=2)
                            raise RuntimeError(
                                f"API exited during startup: {stdout}\n{stderr}"
                            )
                        try:
                            with session.get(
                                f"http://127.0.0.1:{port}/hotel/api/reservation",
                                headers={"Authorization": "Bearer hotel-demo-user"},
                                timeout=0.25,
                            ) as response:
                                if response.status_code == 200 and isinstance(
                                    response.json(), list
                                ):
                                    return server.pid
                        except requests.RequestException:
                            pass
                        time.sleep(0.03)
                raise TimeoutError("Temporary reservation API did not become ready")

            def run_planner(user_input: str) -> str:
                completed = subprocess.run(
                    [sys.executable, "-X", "utf8", "-B", str(hotel / "booking.py")],
                    input=user_input,
                    cwd=directory,
                    env=environment,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
                self.assertNotIn("Operation failed", completed.stdout)
                self.assertIn("Goodbye.", completed.stdout)
                return completed.stdout

            try:
                # Port zero selects an available port. If another process wins
                # the brief close/bind race, select a new port and retry startup.
                for attempt in range(3):
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        port = reservation.getsockname()[1]
                    config = configparser.ConfigParser()
                    config.read(hotel / "api.ini", encoding="utf-8")
                    for service in ("hotel", "band"):
                        config[service]["url"] = (
                            f"http://127.0.0.1:{port}/{service}/api"
                        )
                    config["global"].update(
                        {"retries": "1", "delay": "0", "timeout": "1"}
                    )
                    with (hotel / "api.ini").open("w", encoding="utf-8") as stream:
                        config.write(stream)
                    try:
                        first_pid = start_server(port)
                        break
                    except (RuntimeError, TimeoutError):
                        stop_server()
                        if attempt == 2:
                            raise

                first_output = run_planner("6\n1\n0\n")
                self.assertIn("Reserved matching hotel and band slot 1.", first_output)
                self.assertIn("Hotel: [1]", first_output)
                self.assertIn("Band: [1]", first_output)
                self.assertTrue(database.is_file())
                self.assertTrue(journal.is_file())
                with contextlib.closing(sqlite3.connect(journal)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT slot_id,state FROM workflows"
                        ).fetchall(),
                        [(1, "matched")],
                    )
                stop_server()
                second_pid = start_server(port)
                self.assertNotEqual(first_pid, second_pid)
                second_output = run_planner("1\n0\n")
                self.assertIn("Hotel: [1]", second_output)
                self.assertIn("Band: [1]", second_output)
                self.assertNotIn("Reserved matching", second_output)
                with contextlib.closing(sqlite3.connect(database)) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT service,slot_id FROM reservations ORDER BY service"
                        ).fetchall(),
                        [("band", 1), ("hotel", 1)],
                    )
            finally:
                stop_server()


if __name__ == "__main__":
    unittest.main()
