#!/usr/bin/env python3
"""Local, persistent API for hotel and band reservations."""

from __future__ import annotations

import argparse
import configparser
import json
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
RESERVATION_PATH = re.compile(r"^/(hotel|band)/api/reservation(?:/(available|\d+))?/?$")
OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
OPERATION_PATH = re.compile(r"^/(hotel|band)/api/operation/([A-Za-z0-9._:-]{1,128})/?$")


class ReservationStore:
    def __init__(
        self, database: Path, slot_count: int, *, busy_timeout: float = 1.0
    ) -> None:
        if (
            isinstance(slot_count, bool)
            or not isinstance(slot_count, int)
            or slot_count < 1
        ):
            raise ValueError("slot_count must be a positive integer")
        if not 0 < busy_timeout <= 30:
            raise ValueError("busy_timeout must be between 0 and 30 seconds")
        self.database = database
        self.busy_timeout = busy_timeout
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize(slot_count)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=self.busy_timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
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
                    operation_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (service, slot_id),
                    FOREIGN KEY (service, slot_id) REFERENCES slots(service, slot_id)
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(reservations)")
            }
            if "operation_id" not in columns:
                connection.execute(
                    "ALTER TABLE reservations ADD COLUMN operation_id TEXT"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS reservations_owner "
                "ON reservations(service, token)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    service TEXT NOT NULL,
                    token TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    method TEXT NOT NULL,
                    slot_id INTEGER NOT NULL,
                    expected_operation_id TEXT,
                    status INTEGER NOT NULL,
                    body TEXT NOT NULL,
                    reservation_operation_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(service, token, operation_id)
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

    def get_operation(self, service: str, token: str, operation_id: str) -> dict | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT o.*, r.operation_id AS current_reservation_operation_id
                FROM operations AS o
                LEFT JOIN reservations AS r
                    ON r.service=o.service AND r.slot_id=o.slot_id AND r.token=o.token
                WHERE o.service=? AND o.token=? AND o.operation_id=?
                """,
                (service, token, operation_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "operation_id": row["operation_id"],
            "method": row["method"],
            "slot_id": str(row["slot_id"]),
            "status": row["status"],
            "body": json.loads(row["body"]),
            "reservation_operation_id": row["reservation_operation_id"],
            "current_reservation_operation_id": row["current_reservation_operation_id"],
            "reservation_current": (
                row["reservation_operation_id"] is not None
                and row["reservation_operation_id"]
                == row["current_reservation_operation_id"]
            ),
        }

    def _mutate(
        self,
        service,
        slot_id,
        token,
        method,
        operation_id,
        expected_operation_id,
        action,
    ) -> tuple[int, dict[str, str]]:
        if operation_id is not None and not OPERATION_ID.fullmatch(operation_id):
            return HTTPStatus.BAD_REQUEST, {"message": "Invalid operation ID"}
        if expected_operation_id is not None and not OPERATION_ID.fullmatch(
            expected_operation_id
        ):
            return HTTPStatus.BAD_REQUEST, {"message": "Invalid expected operation ID"}
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if operation_id is not None:
                previous = connection.execute(
                    "SELECT * FROM operations "
                    "WHERE service=? AND token=? AND operation_id=?",
                    (service, token, operation_id),
                ).fetchone()
                if previous is not None:
                    if (
                        previous["method"] != method
                        or previous["slot_id"] != slot_id
                        or previous["expected_operation_id"] != expected_operation_id
                    ):
                        return HTTPStatus.CONFLICT, {
                            "message": "Operation key belongs to a different request",
                            "error": "idempotency_key_conflict",
                        }
                    return previous["status"], json.loads(previous["body"])
            status, body, reservation_operation_id = action(connection)
            if operation_id is not None:
                connection.execute(
                    """
                    INSERT INTO operations(service, token, operation_id, method,
                        slot_id, expected_operation_id, status, body,
                        reservation_operation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        service,
                        token,
                        operation_id,
                        method,
                        slot_id,
                        expected_operation_id,
                        status,
                        json.dumps(body),
                        reservation_operation_id,
                    ),
                )
        return status, body

    def reserve(
        self, service: str, slot_id: int, token: str, operation_id: str | None = None
    ) -> tuple[int, dict[str, str]]:
        def action(connection):
            exists = connection.execute(
                "SELECT 1 FROM slots WHERE service = ? AND slot_id = ?",
                (service, slot_id),
            ).fetchone()
            if not exists:
                return HTTPStatus.FORBIDDEN, {"message": "Slot does not exist"}, None

            held_count = connection.execute(
                "SELECT COUNT(*) FROM reservations WHERE service = ? AND token = ?",
                (service, token),
            ).fetchone()[0]
            if held_count >= 2:
                return (
                    HTTPStatus.UNAVAILABLE_FOR_LEGAL_REASONS,
                    {"message": "Reservation limit reached"},
                    None,
                )

            creation_id = operation_id or uuid.uuid4().hex
            try:
                connection.execute(
                    "INSERT INTO reservations(service, slot_id, token, operation_id) "
                    "VALUES (?, ?, ?, ?)",
                    (service, slot_id, token, creation_id),
                )
            except sqlite3.IntegrityError:
                return HTTPStatus.CONFLICT, {"message": "Slot is unavailable"}, None
            return HTTPStatus.OK, {"id": str(slot_id)}, creation_id

        return self._mutate(service, slot_id, token, "POST", operation_id, None, action)

    def release(
        self,
        service: str,
        slot_id: int,
        token: str,
        operation_id: str | None = None,
        expected_operation_id: str | None = None,
    ) -> tuple[int, dict[str, str]]:
        def action(connection):
            exists = connection.execute(
                "SELECT 1 FROM slots WHERE service = ? AND slot_id = ?",
                (service, slot_id),
            ).fetchone()
            if not exists:
                return HTTPStatus.FORBIDDEN, {"message": "Slot does not exist"}, None
            reservation = connection.execute(
                "SELECT operation_id FROM reservations "
                "WHERE service=? AND slot_id=? AND token=?",
                (service, slot_id, token),
            ).fetchone()
            if reservation is None:
                return (
                    HTTPStatus.NOT_FOUND,
                    {"message": "Reservation not held by this user"},
                    None,
                )
            creation_id = reservation["operation_id"]
            if (
                expected_operation_id is not None
                and creation_id != expected_operation_id
            ):
                return (
                    HTTPStatus.CONFLICT,
                    {
                        "message": "The reservation has changed; cancellation refused",
                        "error": "stale_reservation",
                    },
                    None,
                )
            result = connection.execute(
                "DELETE FROM reservations WHERE service = ? AND slot_id = ? AND token = ?",
                (service, slot_id, token),
            )
            if result.rowcount == 0:
                return (
                    HTTPStatus.NOT_FOUND,
                    {"message": "Reservation not held by this user"},
                    None,
                )
            return (
                HTTPStatus.OK,
                {"message": f"Released {service} slot {slot_id}"},
                creation_id,
            )

        return self._mutate(
            service,
            slot_id,
            token,
            "DELETE",
            operation_id,
            expected_operation_id,
            action,
        )


class ReservationServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

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
        if status == HTTPStatus.SERVICE_UNAVAILABLE:
            self.send_header("Retry-After", "1")
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

    def _database_request(self, action) -> None:
        try:
            action()
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", None)
            if code is None:
                # Python 3.10 does not expose SQLite error codes or constants.
                busy = (
                    str(error)
                    .casefold()
                    .startswith(
                        (
                            "database is locked",
                            "database table is locked",
                            "database schema is locked",
                        )
                    )
                )
            else:
                # SQLite primary codes 5 and 6 are BUSY and LOCKED respectively.
                busy = code & 0xFF in (5, 6)
            if not busy:
                raise
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"message": "Reservation database is busy; retry this operation"},
            )

    def do_GET(self) -> None:
        self._database_request(self._get)

    def _get(self) -> None:
        operation = OPERATION_PATH.fullmatch(unquote(self.path.split("?", 1)[0]))
        if operation:
            service, operation_id = operation.groups()
            token = self.authenticate(service)
            if token is None:
                return
            if not OPERATION_ID.fullmatch(operation_id):
                self.send_json(
                    HTTPStatus.BAD_REQUEST, {"message": "Invalid operation ID"}
                )
                return
            receipt = self.server.store.get_operation(service, token, operation_id)
            self.send_json(
                HTTPStatus.OK if receipt is not None else HTTPStatus.NOT_FOUND,
                receipt if receipt is not None else {"message": "Operation not found"},
            )
            return
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
        self._database_request(self._post)

    def _post(self) -> None:
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
        status, body = self.server.store.reserve(
            service, int(item), token, self.headers.get("Idempotency-Key")
        )
        self.send_json(status, body)

    def do_DELETE(self) -> None:
        self._database_request(self._delete)

    def _delete(self) -> None:
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
        status, body = self.server.store.release(
            service,
            int(item),
            token,
            self.headers.get("Idempotency-Key"),
            self.headers.get("Expected-Reservation-Operation-Id"),
        )
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
