from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Hotel"))

from booking import reserve_best_match
from exceptions import (
    InvalidTokenError,
    ReservationLimitError,
    SlotUnavailableError,
)
from local_api import create_server
from reservationapi import ReservationApi


class HotelIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        database = Path(self.temporary_directory.name) / "reservations.db"
        self.server = create_server(
            database=database, host="127.0.0.1", port=0, slots=5
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.hotel = ReservationApi(
            f"http://127.0.0.1:{port}/hotel/api", "hotel-demo-user", delay=0
        )
        self.hotel_two = ReservationApi(
            f"http://127.0.0.1:{port}/hotel/api", "hotel-demo-user-2", delay=0
        )
        self.band = ReservationApi(
            f"http://127.0.0.1:{port}/band/api", "band-demo-user", delay=0
        )

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def test_reserve_conflict_release_and_service_isolation(self) -> None:
        self.assertEqual(len(self.hotel.get_slots_available()), 5)
        self.assertEqual(self.hotel.reserve_slot(1), {"id": "1"})
        self.assertEqual(self.hotel.get_slots_held(), [{"id": "1"}])
        self.assertEqual(len(self.band.get_slots_available()), 5)

        with self.assertRaises(SlotUnavailableError):
            self.hotel_two.reserve_slot(1)

        self.assertIn("Released", self.hotel.release_slot(1)["message"])
        self.assertEqual(self.hotel.get_slots_held(), [])

    def test_authentication_and_reservation_limit(self) -> None:
        invalid = ReservationApi(self.hotel.base_url, "not-valid", delay=0)
        with self.assertRaises(InvalidTokenError):
            invalid.get_slots_available()

        self.hotel.reserve_slot(1)
        self.hotel.reserve_slot(2)
        with self.assertRaises(ReservationLimitError):
            self.hotel.reserve_slot(3)

    def test_wedding_planner_reserves_best_matching_slot(self) -> None:
        clients = {"hotel": self.hotel, "band": self.band}
        reserve_best_match(clients)
        self.assertEqual(self.hotel.get_slots_held(), [{"id": "1"}])
        self.assertEqual(self.band.get_slots_held(), [{"id": "1"}])


if __name__ == "__main__":
    unittest.main()
