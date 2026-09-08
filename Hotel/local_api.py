#!/usr/bin/env python3
"""Local, persistent replacement for the retired course reservation APIs."""

from __future__ import annotations

import argparse
import configparser
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RESERVATION_PATH = re.compile(r"^/(hotel|band)/api/reservation(?:/(available|\d+))?/?$")


class ReservationStore:
    def __init__(self, database: Path, slot_count: int) -> None:
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(slot_count)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self, slot_count: int) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS slots (
                    service TEXT NOT NULL CHECK(service IN ('hotel', 'band')),
                    slot_id INTEGER NOT NULL CHECK(slot_id > 0),
                    PRIMARY KEY (service, slot_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reservations (
                    service TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (service, slot_id),
                    FOREIGN KEY (service, slot_id) REFERENCES slots(service, slot_id)
                )
                """
            )
            connection.executemany(
                "INSERT OR IGNORE INTO slots(service, slot_id) VALUES (?, ?)",
                (
                    (service, slot_id)
                    for service in ("hotel", "band")
                    for slot_id in range(1, slot_count + 1)
                ),
            )

    def available(self, service: str) -> list[dict[str, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT slot_id FROM slots
                WHERE service = ? AND slot_id NOT IN (
                    SELECT slot_id FROM reservations WHERE service = ?
                )
                ORDER BY slot_id
                """,
                (service, service),
            ).fetchall()
        return [{"id": str(row["slot_id"])} for row in rows]

    def held(self, service: str, token: str) -> list[dict[str, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT slot_id FROM reservations
                WHERE service = ? AND token = ? ORDER BY slot_id
                """,
                (service, token),
            ).fetchall()
        return [{"id": str(row["slot_id"])} for row in rows]

    def reserve(
        self, service: str, slot_id: int, token: str
    ) -> tuple[int, dict[str, str]]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM slots WHERE service = ? AND slot_id = ?",
                (service, slot_id),
            ).fetchone()
            if not exists:
                return HTTPStatus.FORBIDDEN, {"message": "Slot does not exist"}

            held_count = connection.execute(
                "SELECT COUNT(*) FROM reservations WHERE service = ? AND token = ?",
                (service, token),
            ).fetchone()[0]
            if held_count >= 2:
                return HTTPStatus.UNAVAILABLE_FOR_LEGAL_REASONS, {
                    "message": "Reservation limit reached"
                }

            try:
                connection.execute(
                    "INSERT INTO reservations(service, slot_id, token) VALUES (?, ?, ?)",
                    (service, slot_id, token),
                )
            except sqlite3.IntegrityError:
                return HTTPStatus.CONFLICT, {"message": "Slot is unavailable"}
        return HTTPStatus.OK, {"id": str(slot_id)}

    def release(
        self, service: str, slot_id: int, token: str
    ) -> tuple[int, dict[str, str]]:
        with self.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM slots WHERE service = ? AND slot_id = ?",
                (service, slot_id),
            ).fetchone()
            if not exists:
                return HTTPStatus.FORBIDDEN, {"message": "Slot does not exist"}
            result = connection.execute(
                "DELETE FROM reservations WHERE service = ? AND slot_id = ? AND token = ?",
                (service, slot_id, token),
            )
            if result.rowcount == 0:
                return HTTPStatus.NOT_FOUND, {
                    "message": "Reservation not held by this user"
                }
        return HTTPStatus.OK, {"message": f"Released {service} slot {slot_id}"}


class ReservationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: ReservationStore,
        tokens: dict[str, set[str]],
    ) -> None:
        super().__init__(address, ReservationHandler)
        self.store = store
        self.tokens = tokens


class ReservationHandler(BaseHTTPRequestHandler):
    server: ReservationServer

    def send_json(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authenticate(self, service: str) -> str | None:
        prefix = "Bearer "
        header = self.headers.get("Authorization", "")
        token = header[len(prefix) :] if header.startswith(prefix) else ""
        if not token or token not in self.server.tokens[service]:
            self.send_json(
                HTTPStatus.UNAUTHORIZED, {"message": "Invalid or missing token"}
            )
            return None
        return token

    def route(self) -> tuple[str, str | None] | None:
        match = RESERVATION_PATH.fullmatch(self.path.split("?", 1)[0])
        if not match:
            self.send_json(HTTPStatus.NOT_FOUND, {"message": "Endpoint not found"})
            return None
        return match.group(1), match.group(2)

    def do_GET(self) -> None:
        route = self.route()
        if not route:
            return
        service, item = route
        token = self.authenticate(service)
        if not token:
            return
        if item == "available":
            self.send_json(HTTPStatus.OK, self.server.store.available(service))
        elif item is None:
            self.send_json(HTTPStatus.OK, self.server.store.held(service, token))
        else:
            self.send_json(
                HTTPStatus.BAD_REQUEST, {"message": "Unsupported GET request"}
            )

    def do_POST(self) -> None:
        route = self.route()
        if not route:
            return
        service, item = route
        token = self.authenticate(service)
        if not token:
            return
        if not item or not item.isdigit():
            self.send_json(
                HTTPStatus.BAD_REQUEST, {"message": "A numeric slot ID is required"}
            )
            return
        status, body = self.server.store.reserve(service, int(item), token)
        self.send_json(status, body)

    def do_DELETE(self) -> None:
        route = self.route()
        if not route:
            return
        service, item = route
        token = self.authenticate(service)
        if not token:
            return
        if not item or not item.isdigit():
            self.send_json(
                HTTPStatus.BAD_REQUEST, {"message": "A numeric slot ID is required"}
            )
            return
        status, body = self.server.store.release(service, int(item), token)
        self.send_json(status, body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.client_address[0]} - {format % args}")


def parse_tokens(config: configparser.ConfigParser) -> dict[str, set[str]]:
    return {
        service: {
            token.strip()
            for token in config[service]
            .get("tokens", config[service]["key"])
            .split(",")
            if token.strip()
        }
        for service in ("hotel", "band")
    }


def create_server(
    config_path: Path = ROOT / "api.ini",
    *,
    host: str | None = None,
    port: int | None = None,
    database: Path | None = None,
    slots: int | None = None,
) -> ReservationServer:
    config = configparser.ConfigParser()
    if not config.read(config_path, encoding="utf-8"):
        raise FileNotFoundError(f"Configuration file not found: {config_path}")
    local = config["local_server"]
    db_path = database or ROOT / local.get("database", "data/reservations.db")
    return ReservationServer(
        (
            host or local.get("host", "127.0.0.1"),
            port if port is not None else local.getint("port", 8081),
        ),
        ReservationStore(
            db_path, slots if slots is not None else local.getint("slots", 100)
        ),
        parse_tokens(config),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--slots", type=int)
    parser.add_argument("--reset-data", action="store_true")
    args = parser.parse_args()

    config_path = ROOT / "api.ini"
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")
    default_database = ROOT / config["local_server"].get(
        "database", "data/reservations.db"
    )
    database = args.database or default_database
    if args.reset_data:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{database}{suffix}")
            if candidate.exists():
                candidate.unlink()

    server = create_server(
        config_path,
        host=args.host,
        port=args.port,
        database=database,
        slots=args.slots,
    )
    host, port = server.server_address
    print(f"Local reservation API listening on http://{host}:{port}")
    print(f"Database: {database}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local reservation API.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
