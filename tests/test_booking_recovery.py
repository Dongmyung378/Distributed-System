"""Fault-injection checks over the real local reservation HTTP service."""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Hotel"))

from booking import main, recover_pending, reserve_best_match
from local_api import create_server
from recovery import RecoveryJournal, WorkflowBusyError, client_scope
from reservationapi import ReservationApi


class ProcessInterrupted(BaseException):
    """Simulate a process ending without running workflow error handling."""


class BookingRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.database = self.directory / "reservations.db"
        self.journal_path = self.directory / "planner.db"
        self.server = create_server(
            database=self.database, host="127.0.0.1", port=0, slots=5
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self._make_clients()
        self.journal = RecoveryJournal(self.journal_path)
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()

    def _make_clients(self) -> None:
        port = self.server.server_address[1]
        self.clients = {
            service: ReservationApi(
                f"http://127.0.0.1:{port}/{service}/api",
                f"{service}-demo-user",
                retries=1,
                delay=0,
                timeout=1,
            )
            for service in ("hotel", "band")
        }

    def tearDown(self) -> None:
        self.output.__exit__(None, None, None)
        self.journal.close()
        for client in self.clients.values():
            client.session.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def held(self, service: str) -> list[int]:
        return [int(item["id"]) for item in self.clients[service].get_slots_held()]

    def restart_journal(self) -> None:
        self.journal.close()
        self.journal = RecoveryJournal(self.journal_path)

    def restart_server(self) -> None:
        port = self.server.server_address[1]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = create_server(
            database=self.database, host="127.0.0.1", port=port, slots=5
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def pending_hotel(self, slot: int = 1):
        workflow = self.journal.create(client_scope(self.clients), slot)
        self.clients["hotel"].reserve_slot(
            slot, operation_id=workflow.hotel_operation_id
        )
        return workflow

    def pending_pair(self, slot: int = 1):
        workflow = self.pending_hotel(slot)
        workflow.state = "band_pending"
        self.journal.save(workflow, "band_reserve_intent")
        self.clients["band"].reserve_slot(slot, operation_id=workflow.band_operation_id)
        return workflow

    def test_success_is_durable_and_repeated_recovery_is_a_noop(self) -> None:
        reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.held("hotel"), [1])
        self.assertEqual(self.held("band"), [1])
        self.assertEqual(self.journal.all()[0].state, "matched")
        self.restart_journal()
        with patch.object(self.clients["hotel"], "release_slot") as release:
            for _ in range(2):
                summary = recover_pending(self.clients, self.journal)
                self.assertEqual(
                    (summary.matched, summary.aborted, summary.unresolved), (0, 0, 0)
                )
            release.assert_not_called()

    def test_cancelled_hotel_is_not_recreated_and_only_owned_band_is_compensated(
        self,
    ) -> None:
        self.pending_pair()
        self.clients["hotel"].release_slot(1)
        self.restart_journal()
        with patch.object(self.clients["hotel"], "reserve_slot") as reserve:
            summary = recover_pending(self.clients, self.journal)
            reserve.assert_not_called()
        self.assertEqual(
            (summary.aborted, summary.matched, summary.unresolved), (1, 0, 0)
        )
        self.assertEqual(self.held("hotel"), [])
        self.assertEqual(self.held("band"), [])

    def test_replaced_hotel_is_preserved_while_orphaned_band_is_removed(self) -> None:
        self.pending_pair()
        self.clients["hotel"].release_slot(1)
        self.clients["hotel"].reserve_slot(
            1, operation_id="replacement-after-pair-interruption"
        )
        self.restart_journal()
        with patch.object(self.clients["hotel"], "reserve_slot") as reserve:
            summary = recover_pending(self.clients, self.journal)
            reserve.assert_not_called()
        self.assertEqual((summary.aborted, summary.matched), (1, 0))
        self.assertEqual(self.held("hotel"), [1])
        self.assertEqual(self.held("band"), [])

    def test_replaced_band_is_preserved_while_original_hotel_is_removed(self) -> None:
        self.pending_pair()
        self.clients["band"].release_slot(1)
        self.clients["band"].reserve_slot(1, operation_id="manual-band-replacement")
        summary = recover_pending(self.clients, self.journal)
        self.assertEqual((summary.aborted, summary.matched), (1, 0))
        self.assertEqual(self.held("hotel"), [])
        self.assertEqual(self.held("band"), [1])

    def test_orphaned_band_compensation_failure_survives_restart(self) -> None:
        workflow = self.pending_pair()
        self.clients["hotel"].release_slot(1)
        original = self.clients["band"].session.request

        def broken_delete(method, url, **kwargs):
            if method == "DELETE":
                raise requests.ConnectionError("band rollback unavailable")
            return original(method, url, **kwargs)

        with patch.object(
            self.clients["band"].session, "request", side_effect=broken_delete
        ):
            self.assertEqual(recover_pending(self.clients, self.journal).unresolved, 1)
        self.assertEqual(self.journal.all()[0].state, "pair_compensating")
        self.assertEqual(self.held("hotel"), [])
        self.assertEqual(self.held("band"), [1])
        self.restart_journal()
        self.assertEqual(
            self.journal.all()[0].band_release_id, workflow.band_release_id
        )
        self.assertEqual(recover_pending(self.clients, self.journal).aborted, 1)
        self.assertEqual(self.held("hotel"), [])
        self.assertEqual(self.held("band"), [])
        self.assertEqual(
            self.clients["band"].get_operation(workflow.band_release_id)["status"], 200
        )

    def test_missing_current_reservation_proof_keeps_pair_unresolved(self) -> None:
        workflow = self.pending_pair()
        receipt = self.clients["hotel"].get_operation(workflow.hotel_operation_id)
        del receipt["reservation_current"]
        with (
            patch.object(self.clients["hotel"], "get_operation", return_value=receipt),
            patch.object(self.clients["hotel"], "release_slot") as hotel_release,
            patch.object(self.clients["band"], "release_slot") as band_release,
        ):
            self.assertEqual(recover_pending(self.clients, self.journal).unresolved, 1)
            hotel_release.assert_not_called()
            band_release.assert_not_called()
        self.assertEqual(self.journal.all()[0].state, "band_pending")
        self.assertEqual(self.held("hotel"), [1])
        self.assertEqual(self.held("band"), [1])

    def test_final_pair_check_failure_is_durable_and_rechecked(self) -> None:
        with patch.object(
            self.clients["hotel"],
            "get_operation",
            side_effect=requests.ConnectionError("current state unavailable"),
        ):
            reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.journal.all()[0].state, "pair_check")
        self.restart_journal()
        self.assertEqual(recover_pending(self.clients, self.journal).matched, 1)
        self.assertEqual(self.held("hotel"), [1])
        self.assertEqual(self.held("band"), [1])

    def test_default_memory_journal_explicitly_explains_restart_recovery_limit(
        self,
    ) -> None:
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            patch.object(
                self.clients["hotel"],
                "reserve_slot",
                side_effect=requests.ConnectionError("offline"),
            ),
            patch.object(self.clients["hotel"], "get_operation", return_value=None),
        ):
            reserve_best_match(self.clients)
        self.assertIn("in-memory journal lasts only for this call", output.getvalue())
        self.assertIn("RecoveryJournal(path)", output.getvalue())
        self.assertEqual(self.journal.all(), [])

    def test_legacy_journal_gets_a_stable_band_compensation_key(self) -> None:
        workflow = self.pending_hotel()
        old_path = self.directory / "legacy-journal.db"
        with contextlib.closing(sqlite3.connect(old_path)) as connection:
            connection.execute("""CREATE TABLE workflows (
                workflow_id TEXT PRIMARY KEY, scope TEXT NOT NULL, slot_id INTEGER NOT NULL,
                state TEXT NOT NULL, hotel_operation_id TEXT NOT NULL, band_operation_id TEXT NOT NULL,
                hotel_release_id TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            connection.execute(
                "INSERT INTO workflows(workflow_id,scope,slot_id,state,hotel_operation_id,band_operation_id,hotel_release_id) VALUES (?,?,?,?,?,?,?)",
                (
                    workflow.workflow_id,
                    workflow.scope,
                    workflow.slot_id,
                    workflow.state,
                    workflow.hotel_operation_id,
                    workflow.band_operation_id,
                    workflow.hotel_release_id,
                ),
            )
            connection.commit()
        with RecoveryJournal(old_path) as migrated:
            key = migrated.all()[0].band_release_id
            self.assertEqual(len(key), 32)
        with RecoveryJournal(old_path) as reopened:
            self.assertEqual(reopened.all()[0].band_release_id, key)

    def test_automatic_matching_preserves_unrelated_manual_reservations(self) -> None:
        self.clients["hotel"].reserve_slot(5)
        self.clients["band"].reserve_slot(4)
        reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.held("hotel"), [1, 5])
        self.assertEqual(self.held("band"), [1, 4])

    def test_existing_match_does_not_clean_up_unrelated_reservations(self) -> None:
        self.clients["hotel"].reserve_slot(1)
        self.clients["band"].reserve_slot(1)
        self.clients["hotel"].reserve_slot(5)
        self.clients["band"].reserve_slot(4)
        reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.held("hotel"), [1, 5])
        self.assertEqual(self.held("band"), [1, 4])
        self.assertEqual(self.journal.all(), [])

    def test_candidate_race_rolls_back_only_its_own_hotel_then_tries_next(self) -> None:
        competitor = ReservationApi(
            self.clients["band"].base_url, "band-demo-user-2", retries=1, delay=0
        )
        original = self.clients["band"].session.request
        raced = False

        def racing_request(method, url, **kwargs):
            nonlocal raced
            if method == "POST" and url.endswith("/1") and not raced:
                raced = True
                competitor.reserve_slot(1)
            return original(method, url, **kwargs)

        self.clients["hotel"].reserve_slot(5)
        with patch.object(
            self.clients["band"].session, "request", side_effect=racing_request
        ):
            reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.held("hotel"), [2, 5])
        self.assertEqual(self.held("band"), [2])
        self.assertEqual(competitor.get_slots_held(), [{"id": "1"}])
        self.assertEqual(
            [item.state for item in self.journal.all()], ["aborted", "matched"]
        )
        competitor.session.close()

    def test_lost_band_response_and_receipt_outage_survive_client_restart(self) -> None:
        original = self.clients["band"].session.request

        def lost_response(method, url, **kwargs):
            if "/operation/" in url:
                raise requests.ConnectionError(
                    "receipt endpoint temporarily unreachable"
                )
            response = original(method, url, **kwargs)
            if method == "POST":
                raise requests.ReadTimeout("response lost after commit")
            return response

        with patch.object(
            self.clients["band"].session, "request", side_effect=lost_response
        ):
            reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.journal.all()[0].state, "band_pending")
        self.assertEqual(self.held("hotel"), [1])
        self.assertEqual(self.held("band"), [1])
        self.restart_server()
        self.restart_journal()
        summary = recover_pending(self.clients, self.journal)
        self.assertEqual((summary.matched, summary.unresolved), (1, 0))
        self.assertEqual(self.journal.all()[0].state, "matched")
        self.assertEqual(self.held("hotel"), [1])
        self.assertEqual(self.held("band"), [1])

    def test_candidate_race_recovers_lost_commit_and_first_receipt_response(
        self,
    ) -> None:
        """A transient receipt outage must not require another mutation."""
        original = self.clients["band"].session.request
        writes = []
        receipt_reads = []
        lost_operation = None

        with ReservationApi(
            self.clients["band"].base_url, "band-demo-user-2", retries=1, delay=0
        ) as competitor:

            def racing_request(method, url, **kwargs):
                nonlocal lost_operation
                if method == "POST":
                    writes.append(url.rsplit("/", 1)[-1])
                    if url.endswith("/1"):
                        competitor.reserve_slot(1)
                response = original(method, url, **kwargs)
                if method == "POST" and url.endswith("/2"):
                    lost_operation = kwargs["headers"]["Idempotency-Key"]
                    response.close()
                    raise requests.ReadTimeout("committed band response lost")
                if lost_operation and url.endswith("/operation/" + lost_operation):
                    receipt_reads.append(lost_operation)
                    if len(receipt_reads) == 1:
                        response.close()
                        raise requests.ConnectionError("first receipt response lost")
                return response

            self.clients["hotel"].reserve_slot(5)
            with patch.object(
                self.clients["band"].session, "request", side_effect=racing_request
            ):
                reserve_best_match(self.clients, journal=self.journal)

            self.assertEqual(self.held("hotel"), [2, 5])
            self.assertEqual(self.held("band"), [2])
            self.assertEqual(competitor.get_slots_held(), [{"id": "1"}])
            self.assertEqual(
                [item.state for item in self.journal.all()], ["aborted", "matched"]
            )
            self.assertEqual(writes, ["1", "2"])
            # Two confirmation reads, then the existing current-pair check.
            self.assertEqual(receipt_reads, [lost_operation] * 3)

    def test_lost_hotel_response_is_confirmed_then_compensated_on_restart(self) -> None:
        original = self.clients["hotel"].session.request

        def lost_response(method, url, **kwargs):
            if "/operation/" in url:
                raise requests.ConnectionError("receipt unavailable")
            response = original(method, url, **kwargs)
            if method == "POST":
                raise requests.ReadTimeout("lost after hotel commit")
            return response

        with patch.object(
            self.clients["hotel"].session, "request", side_effect=lost_response
        ):
            reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.journal.all()[0].state, "hotel_pending")
        self.assertEqual(self.held("hotel"), [1])
        self.restart_journal()
        summary = recover_pending(self.clients, self.journal)
        self.assertEqual((summary.aborted, summary.unresolved), (1, 0))
        self.assertEqual(self.held("hotel"), [])
        self.assertEqual(self.held("band"), [])

    def test_failed_compensation_is_retained_and_retried_after_restart(self) -> None:
        self.clients["band"].reserve_slot(4)
        self.clients["band"].reserve_slot(5)
        original = self.clients["hotel"].session.request

        def broken_delete(method, url, **kwargs):
            if method == "DELETE":
                raise requests.ConnectionError("compensation API unreachable")
            return original(method, url, **kwargs)

        with patch.object(
            self.clients["hotel"].session, "request", side_effect=broken_delete
        ):
            reserve_best_match(self.clients, journal=self.journal)
        workflow = self.journal.all()[0]
        self.assertEqual(workflow.state, "compensating")
        self.assertEqual(self.held("hotel"), [1])
        self.restart_journal()
        summary = recover_pending(self.clients, self.journal)
        self.assertEqual((summary.aborted, summary.unresolved), (1, 0))
        self.assertEqual(self.held("hotel"), [])
        self.assertEqual(self.held("band"), [4, 5])
        receipt = self.clients["hotel"].get_operation(workflow.hotel_release_id)
        self.assertEqual(receipt["status"], 200)
        self.assertEqual(recover_pending(self.clients, self.journal).aborted, 0)

    def test_unreachable_service_retains_uncertain_state_and_blocks_new_attempts(
        self,
    ) -> None:
        self.pending_hotel()
        with patch.object(
            self.clients["hotel"].session,
            "request",
            side_effect=requests.ConnectionError("unreachable"),
        ):
            summary = recover_pending(self.clients, self.journal)
            self.assertEqual(summary.unresolved, 1)
            reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(len(self.journal.all()), 1)
        self.assertEqual(self.journal.all()[0].state, "hotel_pending")
        self.assertEqual(self.held("hotel"), [1])

    def test_authentication_failure_does_not_erase_a_previously_uncertain_write(
        self,
    ) -> None:
        self.pending_hotel()
        self.server.tokens["hotel"].remove("hotel-demo-user")
        try:
            summary = recover_pending(self.clients, self.journal)
            self.assertEqual(summary.unresolved, 1)
            self.assertEqual(self.journal.all()[0].state, "hotel_pending")
        finally:
            self.server.tokens["hotel"].add("hotel-demo-user")
        self.assertEqual(self.held("hotel"), [1])
        self.assertEqual(recover_pending(self.clients, self.journal).aborted, 1)
        self.assertEqual(self.held("hotel"), [])

    def test_lost_compensation_response_does_not_remove_a_later_manual_replacement(
        self,
    ) -> None:
        self.clients["band"].reserve_slot(4)
        self.clients["band"].reserve_slot(5)
        original = self.clients["hotel"].session.request
        delete_committed = False

        def lost_delete_response(method, url, **kwargs):
            nonlocal delete_committed
            if "/operation/" in url and delete_committed:
                raise requests.ConnectionError("receipt unavailable after delete")
            response = original(method, url, **kwargs)
            if method == "DELETE":
                delete_committed = True
                raise requests.ReadTimeout("delete committed but response lost")
            return response

        with patch.object(
            self.clients["hotel"].session, "request", side_effect=lost_delete_response
        ):
            reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.journal.all()[0].state, "compensating")
        self.assertEqual(self.held("hotel"), [])
        self.clients["hotel"].reserve_slot(1, operation_id="later-manual-reservation")
        self.restart_journal()
        self.assertEqual(recover_pending(self.clients, self.journal).aborted, 1)
        self.assertEqual(self.held("hotel"), [1])

    def test_interrupt_after_hotel_commit_is_recovered_without_reserving_band(
        self,
    ) -> None:
        original = self.clients["hotel"].session.request

        def interrupt(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST":
                raise ProcessInterrupted()
            return response

        with (
            patch.object(
                self.clients["hotel"].session, "request", side_effect=interrupt
            ),
            self.assertRaises(ProcessInterrupted),
        ):
            reserve_best_match(self.clients, journal=self.journal)
        self.assertEqual(self.journal.all()[0].state, "hotel_pending")
        self.restart_journal()
        self.assertEqual(recover_pending(self.clients, self.journal).aborted, 1)
        self.assertEqual(self.held("hotel"), [])
        self.assertEqual(self.held("band"), [])

    def test_interrupt_after_band_commit_preserves_both_reservations(self) -> None:
        original = self.clients["band"].session.request

        def interrupt(method, url, **kwargs):
            response = original(method, url, **kwargs)
            if method == "POST":
                raise ProcessInterrupted()
            return response

        with (
            patch.object(
                self.clients["band"].session, "request", side_effect=interrupt
            ),
            self.assertRaises(ProcessInterrupted),
        ):
            reserve_best_match(self.clients, journal=self.journal)
        self.restart_journal()
        self.assertEqual(recover_pending(self.clients, self.journal).matched, 1)
        self.assertEqual(self.held("hotel"), [1])
        self.assertEqual(self.held("band"), [1])

    def test_intent_without_sent_request_is_closed_by_same_key_and_compensated(
        self,
    ) -> None:
        workflow = self.journal.create(client_scope(self.clients), 1)
        self.assertIsNone(
            self.clients["hotel"].get_operation(workflow.hotel_operation_id)
        )
        self.restart_journal()
        self.assertEqual(recover_pending(self.clients, self.journal).aborted, 1)
        reserve = self.clients["hotel"].get_operation(workflow.hotel_operation_id)
        release = self.clients["hotel"].get_operation(workflow.hotel_release_id)
        self.assertEqual((reserve["status"], release["status"]), (200, 200))
        self.assertEqual(self.held("hotel"), [])

    def test_same_owner_replacement_is_never_deleted_by_old_compensation(self) -> None:
        workflow = self.pending_hotel()
        self.clients["hotel"].release_slot(1)
        self.clients["hotel"].reserve_slot(1, operation_id="manual-replacement")
        self.restart_journal()
        summary = recover_pending(self.clients, self.journal)
        self.assertEqual((summary.aborted, summary.unresolved), (1, 0))
        self.assertEqual(self.held("hotel"), [1])
        receipt = self.clients["hotel"].get_operation(workflow.hotel_release_id)
        self.assertEqual(receipt["status"], 409)
        self.assertEqual(receipt["body"]["error"], "stale_reservation")
        with contextlib.closing(sqlite3.connect(self.database)) as connection:
            operation = connection.execute(
                "SELECT operation_id FROM reservations WHERE service='hotel' AND slot_id=1"
            ).fetchone()[0]
        self.assertEqual(operation, "manual-replacement")

    def test_other_account_cannot_recover_or_cancel_this_workflow(self) -> None:
        self.pending_hotel()
        others = {
            service: ReservationApi(
                client.base_url, f"{service}-demo-user-2", retries=1, delay=0
            )
            for service, client in self.clients.items()
        }
        self.assertEqual(recover_pending(others, self.journal).aborted, 0)
        self.assertEqual(self.journal.all()[0].state, "hotel_pending")
        self.assertEqual(self.held("hotel"), [1])
        for client in others.values():
            client.session.close()

    def test_journal_contains_no_raw_tokens_or_transport_error_text(self) -> None:
        self.pending_hotel()
        with patch.object(
            self.clients["hotel"].session,
            "request",
            side_effect=requests.ConnectionError(
                "Authorization: Bearer hotel-demo-user"
            ),
        ):
            recover_pending(self.clients, self.journal)
        with contextlib.closing(sqlite3.connect(self.journal_path)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn("hotel-demo-user", dump)
        self.assertNotIn("band-demo-user", dump)
        self.assertNotIn("Authorization", dump)
        self.assertNotIn("127.0.0.1", dump)
        self.assertEqual(self.journal.all()[0].last_error, "OperationUncertainError")

    def test_second_planner_cannot_compensate_an_active_planners_work(self) -> None:
        self.pending_hotel()
        code = """
import sys
sys.path.insert(0, sys.argv[1])
from booking import recover_pending
from recovery import RecoveryJournal, WorkflowBusyError
from reservationapi import ReservationApi
clients = {name: ReservationApi(sys.argv[index], name+'-demo-user', retries=1, delay=0)
           for index, name in ((3, 'hotel'), (4, 'band'))}
try:
    with RecoveryJournal(sys.argv[2]) as journal:
        with journal.locked():
            result = recover_pending(clients, journal, announce=False)
            print('ABORTED', result.aborted)
except WorkflowBusyError:
    print('BUSY')
    sys.exit(17)
"""
        command = [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            code,
            str(ROOT / "Hotel"),
            str(self.journal_path),
            self.clients["hotel"].base_url,
            self.clients["band"].base_url,
        ]
        with self.journal.locked():
            child = subprocess.run(
                command, check=False, text=True, capture_output=True, timeout=10
            )
            self.assertEqual(child.returncode, 17, child.stderr)
            self.assertIn("BUSY", child.stdout)
            self.assertEqual(self.held("hotel"), [1])
        child = subprocess.run(
            command, check=False, text=True, capture_output=True, timeout=10
        )
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertIn("ABORTED 1", child.stdout)
        self.assertEqual(self.held("hotel"), [])

    def test_process_exit_automatically_releases_the_journal_lock(self) -> None:
        code = """
import sys
sys.path.insert(0, sys.argv[1])
from recovery import RecoveryJournal
with RecoveryJournal(sys.argv[2]) as journal:
    with journal.locked():
        print('LOCKED', flush=True)
        sys.stdin.read(1)
"""
        child = subprocess.Popen(
            [
                sys.executable,
                "-X",
                "utf8",
                "-B",
                "-c",
                code,
                str(ROOT / "Hotel"),
                str(self.journal_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            with self.assertRaises(WorkflowBusyError), self.journal.locked():
                self.fail("The second planner acquired an active lock")
        finally:
            child.kill()
            child.communicate(timeout=5)
        with self.journal.locked():
            workflow = self.journal.create(client_scope(self.clients), 1)
        self.assertEqual(workflow.state, "hotel_pending")

    def test_cli_startup_runs_recovery_before_the_menu(self) -> None:
        self.pending_hotel()
        with (
            patch.dict(os.environ, {"BOOKING_JOURNAL_PATH": str(self.journal_path)}),
            patch("booking.create_clients", return_value=self.clients),
            patch("builtins.input", return_value="0"),
        ):
            main()
        self.assertEqual(self.held("hotel"), [])
        self.assertEqual(self.journal.all()[0].state, "aborted")

    def test_malformed_receipt_cannot_authorize_destructive_recovery(self) -> None:
        self.pending_hotel()
        with (
            patch.object(self.clients["hotel"], "get_operation", return_value=["bad"]),
            patch.object(
                self.clients["hotel"],
                "reserve_slot",
                side_effect=requests.ConnectionError("unreachable"),
            ),
        ):
            self.assertEqual(recover_pending(self.clients, self.journal).unresolved, 1)
        self.assertEqual(self.held("hotel"), [1])


if __name__ == "__main__":
    unittest.main()
