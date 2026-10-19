"""Reservation correctness under concurrent writes, retries and restarts."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Hotel"))

from exceptions import (
    IdempotencyConflictError,
    InvalidTokenError,
    OperationUncertainError,
    SlotUnavailableError,
    StaleReservationError,
)
from local_api import ReservationStore, create_server
from reservationapi import ReservationApi


class ReservationReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "reservations.db"
        self.clients = []
        self.start_server()

    def start_server(self):
        self.server = create_server(
            database=self.database, host="127.0.0.1", port=0, slots=8
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.hotel = self.client("hotel", "hotel-demo-user")
        self.other = self.client("hotel", "hotel-demo-user-2")
        self.band = self.client("band", "band-demo-user")

    def client(self, service, token, **kwargs):
        client = ReservationApi(
            f"{self.base}/{service}/api", token, delay=0, timeout=2, **kwargs
        )
        self.clients.append(client)
        return client

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.stop_server()
        self.temporary.cleanup()

    @staticmethod
    def lose_first_response(client):
        original = client.session.request
        keys = []

        def request(method, url, **kwargs):
            response = original(method, url, **kwargs)
            keys.append(kwargs["headers"].get("Idempotency-Key"))
            if len(keys) == 1:
                # The real HTTP server committed. Only the client response is lost.
                response.close()
                raise requests.ReadTimeout("injected response loss after commit")
            return response

        return request, keys

    def test_legacy_unkeyed_contract_is_preserved(self):
        headers = {"Authorization": "Bearer hotel-demo-user"}
        url = f"{self.base}/hotel/api/reservation"
        with requests.Session() as session:
            response = session.post(f"{url}/1", headers=headers, timeout=2)
            self.assertEqual(
                (response.status_code, response.json()), (200, {"id": "1"})
            )
            self.assertEqual(
                session.post(f"{url}/1", headers=headers, timeout=2).status_code, 409
            )
            self.assertEqual(
                session.post(f"{url}/2", headers=headers, timeout=2).status_code, 200
            )
            self.assertEqual(
                session.post(f"{url}/3", headers=headers, timeout=2).status_code, 451
            )
            self.assertEqual(
                session.post(f"{url}/99", headers=headers, timeout=2).status_code, 403
            )
            response = session.delete(f"{url}/1", headers=headers, timeout=2)
            self.assertEqual(response.json(), {"message": "Released hotel slot 1"})
            self.assertEqual(
                session.delete(f"{url}/1", headers=headers, timeout=2).status_code, 404
            )

    def test_post_response_loss_reuses_one_automatically_generated_key(self):
        request, keys = self.lose_first_response(self.hotel)
        with patch.object(self.hotel.session, "request", side_effect=request):
            self.assertEqual(self.hotel.reserve_slot(1), {"id": "1"})
        self.assertEqual(len(keys), 2)
        self.assertIsNotNone(keys[0])
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(self.hotel.get_slots_held(), [{"id": "1"}])
        receipt = self.hotel.get_operation(keys[0])
        self.assertEqual(receipt["reservation_operation_id"], keys[0])
        self.assertEqual(receipt["status"], 200)

    def test_delete_response_loss_replays_success(self):
        self.hotel.reserve_slot(1, operation_id="reserve-original")
        request, keys = self.lose_first_response(self.hotel)
        with patch.object(self.hotel.session, "request", side_effect=request):
            result = self.hotel.release_slot(
                1,
                operation_id="release-original",
                expected_operation_id="reserve-original",
            )
        self.assertEqual(result, {"message": "Released hotel slot 1"})
        self.assertEqual(keys, ["release-original", "release-original"])
        self.assertEqual(self.hotel.get_slots_held(), [])
        receipt = self.hotel.get_operation("release-original")
        self.assertEqual(receipt["method"], "DELETE")
        self.assertEqual(receipt["reservation_operation_id"], "reserve-original")

    def test_exhausted_mutation_can_be_resolved_from_durable_receipt(self):
        self.hotel.retries = 1
        request, _ = self.lose_first_response(self.hotel)
        with (
            patch.object(self.hotel.session, "request", side_effect=request),
            self.assertRaises(OperationUncertainError) as raised,
        ):
            self.hotel.reserve_slot(1, operation_id="lost-success")
        self.assertEqual(raised.exception.operation_id, "lost-success")
        self.assertEqual(raised.exception.method, "POST")
        self.assertIsInstance(raised.exception.last_error, requests.ReadTimeout)
        self.assertEqual(self.hotel.get_operation("lost-success")["body"], {"id": "1"})
        self.assertEqual(
            self.hotel.reserve_slot(1, operation_id="lost-success"), {"id": "1"}
        )

    def test_same_key_different_request_is_rejected_without_modification(self):
        self.hotel.reserve_slot(1, operation_id="one-intent")
        with self.assertRaises(IdempotencyConflictError):
            self.hotel.reserve_slot(2, operation_id="one-intent")
        with self.assertRaises(IdempotencyConflictError):
            self.hotel.release_slot(1, operation_id="one-intent")
        self.assertEqual(self.hotel.get_slots_held(), [{"id": "1"}])
        self.assertEqual(self.hotel.get_operation("one-intent")["method"], "POST")

    def test_receipt_scope_separates_owners_and_services(self):
        self.hotel.reserve_slot(1, operation_id="shared-key")
        self.assertIsNone(self.other.get_operation("shared-key"))
        self.assertIsNone(self.band.get_operation("shared-key"))
        self.other.reserve_slot(2, operation_id="shared-key")
        self.band.reserve_slot(3, operation_id="shared-key")
        self.assertEqual(self.hotel.get_operation("shared-key")["slot_id"], "1")
        self.assertEqual(self.other.get_operation("shared-key")["slot_id"], "2")
        self.assertEqual(self.band.get_operation("shared-key")["slot_id"], "3")
        invalid = self.client("hotel", "not-valid")
        with self.assertRaises(InvalidTokenError):
            invalid.get_operation("shared-key")

    def test_confirmed_failure_receipt_is_not_reexecuted_after_state_change(self):
        self.hotel.reserve_slot(1)
        with self.assertRaises(SlotUnavailableError):
            self.other.reserve_slot(1, operation_id="failed-attempt")
        self.hotel.release_slot(1)
        with self.assertRaises(SlotUnavailableError):
            self.other.reserve_slot(1, operation_id="failed-attempt")
        self.assertEqual(self.other.get_operation("failed-attempt")["status"], 409)
        self.assertEqual(self.other.get_slots_held(), [])
        self.other.reserve_slot(1, operation_id="new-attempt")

    def test_stale_compensation_cannot_delete_a_replacement_reservation(self):
        self.hotel.reserve_slot(1, operation_id="old-reserve")
        self.hotel.release_slot(1, operation_id="manual-release")
        self.hotel.reserve_slot(1, operation_id="replacement-reserve")
        with self.assertRaises(StaleReservationError):
            self.hotel.release_slot(
                1, operation_id="late-compensation", expected_operation_id="old-reserve"
            )
        self.assertEqual(self.hotel.get_slots_held(), [{"id": "1"}])
        self.assertEqual(self.hotel.get_operation("old-reserve")["status"], 200)
        self.assertEqual(self.hotel.get_operation("late-compensation")["status"], 409)

    def test_successful_historical_delete_replay_does_not_delete_replacement(self):
        self.hotel.reserve_slot(1, operation_id="first-reserve")
        original = self.hotel.release_slot(
            1, operation_id="first-release", expected_operation_id="first-reserve"
        )
        self.hotel.reserve_slot(1, operation_id="second-reserve")
        self.assertEqual(
            self.hotel.release_slot(
                1, operation_id="first-release", expected_operation_id="first-reserve"
            ),
            original,
        )
        self.assertEqual(self.hotel.get_slots_held(), [{"id": "1"}])

    def test_changed_compensation_guard_conflicts_with_original_key(self):
        self.hotel.reserve_slot(1, operation_id="reservation")
        self.hotel.release_slot(
            1, operation_id="cancellation", expected_operation_id="reservation"
        )
        with self.assertRaises(IdempotencyConflictError):
            self.hotel.release_slot(1, operation_id="cancellation")

    def test_allowed_operation_id_punctuation_round_trips_through_url(self):
        key = "workflow:hotel.reserve_1-2"
        self.hotel.reserve_slot(1, operation_id=key)
        self.assertEqual(self.hotel.get_operation(key)["operation_id"], key)

    def test_receipt_current_generation_tracks_cancellation_and_same_owner_rebooking(
        self,
    ):
        self.hotel.reserve_slot(1, operation_id="initial-generation")
        receipt = self.hotel.get_operation("initial-generation")
        self.assertIs(receipt["reservation_current"], True)
        self.assertEqual(
            receipt["current_reservation_operation_id"], "initial-generation"
        )
        self.hotel.release_slot(1)
        receipt = self.hotel.get_operation("initial-generation")
        self.assertIs(receipt["reservation_current"], False)
        self.assertIsNone(receipt["current_reservation_operation_id"])
        self.assertEqual(receipt["status"], 200)
        self.hotel.reserve_slot(1, operation_id="replacement-generation")
        receipt = self.hotel.get_operation("initial-generation")
        self.assertIs(receipt["reservation_current"], False)
        self.assertEqual(
            receipt["current_reservation_operation_id"], "replacement-generation"
        )
        self.assertIs(
            self.hotel.get_operation("replacement-generation")["reservation_current"],
            True,
        )

    def test_receipt_current_generation_does_not_disclose_a_different_owner(self):
        self.hotel.reserve_slot(1, operation_id="original-owner-generation")
        self.hotel.release_slot(1)
        self.other.reserve_slot(1, operation_id="other-owner-secret-generation")
        receipt = self.hotel.get_operation("original-owner-generation")
        self.assertIs(receipt["reservation_current"], False)
        self.assertIsNone(receipt["current_reservation_operation_id"])
        self.assertNotIn("other-owner-secret-generation", str(receipt))
        self.assertIsNone(self.other.get_operation("original-owner-generation"))

    def test_restart_preserves_reservations_and_operation_receipts(self):
        self.hotel.reserve_slot(1, operation_id="persisted-reserve")
        self.hotel.reserve_slot(2)
        self.stop_server()
        self.start_server()
        self.assertEqual(self.hotel.get_slots_held(), [{"id": "1"}, {"id": "2"}])
        self.assertEqual(
            self.hotel.reserve_slot(1, operation_id="persisted-reserve"), {"id": "1"}
        )
        self.hotel.release_slot(1, operation_id="persisted-delete")
        self.stop_server()
        self.start_server()
        self.assertEqual(
            self.hotel.release_slot(1, operation_id="persisted-delete"),
            {"message": "Released hotel slot 1"},
        )

    def test_database_lock_returns_explicit_503_without_partial_operation(self):
        self.server.store.busy_timeout = 0.03
        with self.server.store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            response = requests.post(
                f"{self.base}/hotel/api/reservation/1",
                timeout=2,
                headers={
                    "Authorization": "Bearer hotel-demo-user",
                    "Idempotency-Key": "locked",
                },
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["Retry-After"], "1")
            self.assertIsNone(self.hotel.get_operation("locked"))
        self.assertEqual(self.hotel.reserve_slot(1, operation_id="locked"), {"id": "1"})

    def test_invalid_idempotency_header_is_rejected_without_writing(self):
        for key in ("", "bad/key", "a" * 129):
            with self.subTest(key=key):
                response = requests.post(
                    f"{self.base}/hotel/api/reservation/1",
                    timeout=2,
                    headers={
                        "Authorization": "Bearer hotel-demo-user",
                        "Idempotency-Key": key,
                    },
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.hotel.get_slots_held(), [])

    def test_simultaneous_http_requests_have_one_slot_winner(self):
        count = 12
        barrier = threading.Barrier(count)

        def reserve(index):
            barrier.wait(timeout=5)
            response = requests.post(
                f"{self.base}/hotel/api/reservation/1",
                timeout=3,
                headers={
                    "Authorization": f"Bearer hotel-demo-user{'-2' if index % 2 else ''}",
                    "Idempotency-Key": f"http-race-{index}",
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=count) as pool:
            statuses = list(pool.map(reserve, range(count)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(409), count - 1)
        self.assertEqual(
            len(self.hotel.get_slots_held()) + len(self.other.get_slots_held()), 1
        )

    def test_simultaneous_http_requests_preserve_owner_limit(self):
        count = 8
        barrier = threading.Barrier(count)

        def reserve(index):
            barrier.wait(timeout=5)
            response = requests.post(
                f"{self.base}/hotel/api/reservation/{index + 1}",
                timeout=3,
                headers={
                    "Authorization": "Bearer hotel-demo-user",
                    "Idempotency-Key": f"http-limit-{index}",
                },
            )
            return response.status_code

        with ThreadPoolExecutor(max_workers=count) as pool:
            statuses = list(pool.map(reserve, range(count)))
        self.assertEqual(statuses.count(200), 2)
        self.assertEqual(statuses.count(451), count - 2)
        self.assertEqual(len(self.hotel.get_slots_held()), 2)


class ReservationStoreConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "reservations.db"
        self.store = ReservationStore(self.database, 20)

    def tearDown(self):
        self.temporary.cleanup()

    def simultaneously(self, count, action):
        barrier = threading.Barrier(count)

        def run(index):
            barrier.wait(timeout=5)
            return action(index)

        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(run, range(count)))

    def test_simultaneous_reservation_has_exactly_one_winner(self):
        results = self.simultaneously(
            12, lambda i: self.store.reserve("hotel", 1, f"user-{i}", f"race-{i}")[0]
        )
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 11)

    def test_simultaneous_same_owner_never_exceeds_limit(self):
        results = self.simultaneously(
            12,
            lambda i: self.store.reserve("hotel", i + 1, "same-owner", f"limit-{i}")[0],
        )
        self.assertEqual(results.count(200), 2)
        self.assertEqual(results.count(451), 10)
        self.assertEqual(len(self.store.held("hotel", "same-owner")), 2)

    def test_simultaneous_same_operation_replays_one_creation(self):
        results = self.simultaneously(
            12, lambda _: self.store.reserve("hotel", 1, "owner", "single-intent")
        )
        self.assertEqual(results, [(200, {"id": "1"})] * 12)
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 1
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 1
            )

    def test_reservation_and_receipt_are_committed_atomically(self):
        with self.store.connection() as connection:
            connection.execute(
                "CREATE TRIGGER reject_receipt BEFORE INSERT ON operations "
                "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reserve("hotel", 1, "owner", "atomic-intent")
        self.assertEqual(self.store.held("hotel", "owner"), [])
        self.assertIsNone(self.store.get_operation("hotel", "owner", "atomic-intent"))
        with self.store.connection() as connection:
            connection.execute("DROP TRIGGER reject_receipt")
        self.assertEqual(
            self.store.reserve("hotel", 1, "owner", "atomic-intent")[0], 200
        )

    def test_foreign_key_constraint_is_enabled_on_every_connection(self):
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO reservations(service, slot_id, token) VALUES ('hotel', 999, 'owner')"
                )

    def test_failed_delete_receipt_write_rolls_back_cancellation(self):
        self.store.reserve("hotel", 1, "owner", "created")
        with self.store.connection() as connection:
            connection.execute(
                "CREATE TRIGGER reject_receipt BEFORE INSERT ON operations "
                "BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.release("hotel", 1, "owner", "cancelled", "created")
        self.assertEqual(self.store.held("hotel", "owner"), [{"id": "1"}])
        self.assertIsNone(self.store.get_operation("hotel", "owner", "cancelled"))

    def test_original_schema_migration_preserves_legacy_reservation(self):
        legacy = Path(self.temporary.name) / "legacy.db"
        with sqlite3.connect(legacy) as connection:
            connection.executescript(
                "CREATE TABLE slots(service TEXT,slot_id INTEGER,PRIMARY KEY(service,slot_id));"
                "CREATE TABLE reservations(service TEXT,slot_id INTEGER,token TEXT,"
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(service,slot_id),"
                "FOREIGN KEY(service,slot_id) REFERENCES slots(service,slot_id));"
                "INSERT INTO slots VALUES ('hotel',1);"
                "INSERT INTO reservations(service,slot_id,token) VALUES ('hotel',1,'legacy-owner');"
            )
        connection.close()
        store = ReservationStore(legacy, 5)
        self.assertEqual(store.held("hotel", "legacy-owner"), [{"id": "1"}])
        self.assertEqual(
            store.release("hotel", 1, "legacy-owner", "guarded", "unknown-creation")[0],
            409,
        )
        self.assertEqual(store.held("hotel", "legacy-owner"), [{"id": "1"}])
        self.assertEqual(store.release("hotel", 1, "legacy-owner")[0], 200)
        self.assertEqual(ReservationStore(legacy, 5).held("hotel", "legacy-owner"), [])


class ReservationClientValidationTests(unittest.TestCase):
    def test_invalid_retry_configuration_is_rejected(self):
        for kwargs in (
            {"retries": 0},
            {"retries": 11},
            {"retries": True},
            {"retries": 1.5},
            {"delay": -1},
            {"delay": float("nan")},
            {"timeout": 0},
            {"timeout": float("inf")},
            {"max_elapsed": -1},
            {"max_elapsed": 301},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ReservationApi("http://127.0.0.1/hotel/api", "owner", **kwargs)

    def test_retry_budget_does_not_schedule_attempts_beyond_deadline(self):
        with ReservationApi(
            "http://127.0.0.1/hotel/api",
            "owner",
            retries=10,
            delay=0,
            timeout=2,
            max_elapsed=0.05,
        ) as client:
            with (
                patch("reservationapi.time.monotonic", side_effect=[0, 0, 0.06, 0.06]),
                patch.object(
                    client.session, "request", side_effect=requests.Timeout("fault")
                ) as request,
                self.assertRaises(OperationUncertainError),
            ):
                client.reserve_slot(1, operation_id="budget")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs["timeout"], 0.05)

    def test_invalid_operation_id_never_reaches_network(self):
        with (
            ReservationApi("http://127.0.0.1/hotel/api", "owner") as client,
            patch.object(client.session, "request") as request,
        ):
            for key in ("", "../bad", "a" * 129, "한글"):
                with self.subTest(key=key), self.assertRaises(ValueError):
                    client.reserve_slot(1, operation_id=key)
            request.assert_not_called()

    def test_receipt_confirmation_retries_share_one_deadline(self):
        with ReservationApi(
            "http://127.0.0.1/band/api",
            "owner",
            retries=1,
            delay=0,
            timeout=1,
            max_elapsed=0.05,
        ) as client:
            with (
                patch("reservationapi.time.monotonic", side_effect=[0, 0, 0.06, 0.06]),
                patch.object(
                    client.session, "request", side_effect=requests.Timeout("fault")
                ) as request,
                self.assertRaises(requests.HTTPError),
            ):
                client.get_operation("receipt-budget", retries=2)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[0], "GET")
            self.assertEqual(request.call_args.kwargs["timeout"], 0.05)
            self.assertEqual(client.retries, 1)

    def test_invalid_receipt_retry_override_never_reaches_network(self):
        with (
            ReservationApi("http://127.0.0.1/band/api", "owner") as client,
            patch.object(client.session, "request") as request,
        ):
            for retries in (0, 11, True, 1.5):
                with self.subTest(retries=retries), self.assertRaises(ValueError):
                    client.get_operation("valid-receipt", retries=retries)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
