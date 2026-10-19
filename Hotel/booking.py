#!/usr/bin/env python3
"""Interactive wedding planner for matching hotel and band slots."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

from exceptions import (
    BadRequestError,
    BadSlotError,
    InvalidTokenError,
    NotProcessedError,
    ReservationLimitError,
    SlotUnavailableError,
    StaleReservationError,
)
from recovery import RecoveryJournal, Workflow, WorkflowBusyError, client_scope
from reservationapi import ReservationApi

ROOT = Path(__file__).resolve().parent


def load_config(path: Path = ROOT / "api.ini") -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if not config.read(path, encoding="utf-8"):
        raise FileNotFoundError(f"Configuration file not found: {path}")
    return config


def create_clients(
    config: configparser.ConfigParser,
) -> dict[str, ReservationApi]:
    retries = config.getint("global", "retries", fallback=3)
    delay = config.getfloat("global", "delay", fallback=0.5)
    timeout = config.getfloat("global", "timeout", fallback=10.0)
    return {
        service: ReservationApi(
            config[service]["url"],
            os.getenv(f"{service.upper()}_API_KEY", config[service]["key"]),
            retries,
            delay,
            timeout,
        )
        for service in ("hotel", "band")
    }


def slot_ids(slots: list[dict[str, str]]) -> list[int]:
    return sorted(int(slot["id"]) for slot in slots)


def current_reservations(clients: dict[str, ReservationApi]) -> dict[str, list[int]]:
    return {
        service: slot_ids(client.get_slots_held())
        for service, client in clients.items()
    }


def show_reservations(clients: dict[str, ReservationApi]) -> None:
    held = current_reservations(clients)
    print("\n====== Current Reservations ======")
    for service in ("hotel", "band"):
        print(f"{service.title()}: {held[service] or 'None'}")
    print("==================================")


def show_available(clients: dict[str, ReservationApi], limit: int = 20) -> None:
    print("\n====== Available Slots ======")
    for service, client in clients.items():
        available = slot_ids(client.get_slots_available())
        print(f"{service.title()}: {available[:limit]}")
    print("=============================")


def choose_service(
    clients: dict[str, ReservationApi],
) -> tuple[str, ReservationApi] | None:
    choice = input("Choose service (1=Hotel, 2=Band, 0=Back): ").strip()
    if choice == "0":
        return None
    names = {"1": "hotel", "2": "band"}
    if choice not in names:
        print("Invalid service.")
        return None
    name = names[choice]
    return name, clients[name]


def reserve_manually(clients: dict[str, ReservationApi]) -> None:
    selected = choose_service(clients)
    if not selected:
        return
    service, client = selected
    held = slot_ids(client.get_slots_held())
    if len(held) >= 2:
        print(f"{service.title()} reservation limit reached; cancel one first.")
        return
    available = slot_ids(client.get_slots_available())
    print(f"First 20 available {service} slots: {available[:20]}")
    slot_id = input("Slot number: ").strip()
    result = client.reserve_slot(slot_id)
    print(f"Reserved {service} slot {result['id']}.")


def cancel_manually(clients: dict[str, ReservationApi]) -> None:
    selected = choose_service(clients)
    if not selected:
        return
    service, client = selected
    held = slot_ids(client.get_slots_held())
    print(f"Current {service} reservations: {held or 'None'}")
    if not held:
        return
    slot_id = input("Slot number to cancel: ").strip()
    result = client.release_slot(slot_id)
    print(result["message"])


def matching_slots(clients: dict[str, ReservationApi]) -> list[int]:
    hotel = set(slot_ids(clients["hotel"].get_slots_available()))
    band = set(slot_ids(clients["band"].get_slots_available()))
    return sorted(hotel & band)


def show_matching(clients: dict[str, ReservationApi], limit: int = 5) -> None:
    matches = matching_slots(clients)
    print(f"Matching slots: {matches[:limit] if matches else 'None'}")


def cleanup_reservations(clients: dict[str, ReservationApi]) -> None:
    held = current_reservations(clients)
    common = sorted(set(held["hotel"]) & set(held["band"]))
    keep_common = common[0] if common else None

    for service, reservations in held.items():
        keep = (
            keep_common
            if keep_common in reservations
            else (reservations[0] if reservations else None)
        )
        for slot_id in reservations:
            if slot_id != keep:
                clients[service].release_slot(slot_id)
                print(f"Released unnecessary {service} slot {slot_id}.")
    print("Cleanup complete.")


@dataclass(frozen=True)
class RecoverySummary:
    matched: int = 0
    aborted: int = 0
    unresolved: int = 0


@dataclass
class _Outcome:
    result: str
    error: Exception | None = None
    status: int | None = None
    reason: str = ""


_REJECTED = (
    BadRequestError,
    BadSlotError,
    InvalidTokenError,
    NotProcessedError,
    ReservationLimitError,
    SlotUnavailableError,
)


def _receipt_outcome(
    client: ReservationApi, operation_id: str, method: str, slot_id: int
) -> _Outcome | None:
    try:
        # A lost mutation response followed by one transient receipt failure
        # can still be resolved without submitting another mutation. Receipt GET
        # attempts share the client's existing monotonic request budget.
        receipt = client.get_operation(operation_id, retries=max(2, client.retries))
    except Exception:  # noqa: BLE001 - a failed lookup leaves the outcome uncertain
        return None
    if not isinstance(receipt, dict) or not receipt:
        return None
    # Treat malformed or mismatched receipts as uncertain, never as permission
    # to remove a reservation. No credentials or exception text enter the log.
    if (
        receipt.get("operation_id") != operation_id
        or receipt.get("method") != method
        or str(receipt.get("slot_id")) != str(slot_id)
        or not isinstance(receipt.get("status"), int)
    ):
        return None
    status = receipt["status"]
    if 200 <= status < 300:
        if method == "POST" and receipt.get("reservation_operation_id") != operation_id:
            return None
        return _Outcome("success", status=status)
    if 400 <= status < 500:
        body = receipt.get("body")
        reason = str(body.get("error", "")) if isinstance(body, dict) else ""
        return _Outcome("rejected", status=status, reason=reason)
    return None


def _request_outcome(
    client: ReservationApi,
    workflow: Workflow,
    operation_id: str,
    *,
    release: bool = False,
    resolve_first: bool = False,
    expected_operation_id: str | None = None,
) -> _Outcome:
    method = "DELETE" if release else "POST"
    if resolve_first:
        receipt = _receipt_outcome(client, operation_id, method, workflow.slot_id)
        if receipt is not None:
            return receipt
    try:
        if release:
            client.release_slot(
                workflow.slot_id,
                operation_id=operation_id,
                expected_operation_id=expected_operation_id
                or workflow.hotel_operation_id,
            )
        else:
            client.reserve_slot(workflow.slot_id, operation_id=operation_id)
        return _Outcome("success")
    except _REJECTED as error:
        receipt = _receipt_outcome(client, operation_id, method, workflow.slot_id)
        if receipt is not None:
            receipt.error = error
            return receipt
        # An authentication rejection or a legacy endpoint's 404 does not
        # establish the outcome of an earlier request whose response was lost.
        return _Outcome("unknown", error)
    except Exception as error:  # noqa: BLE001 - preserve uncertain writes for recovery
        receipt = _receipt_outcome(client, operation_id, method, workflow.slot_id)
        return receipt or _Outcome("unknown", error)


def _transition(
    journal: RecoveryJournal,
    workflow: Workflow,
    state: str,
    detail: str,
    error: Exception | None = None,
) -> None:
    workflow.state = state
    workflow.last_error = type(error).__name__ if error else ""
    journal.save(workflow, detail)


def _release_owned(
    clients: dict[str, ReservationApi],
    journal: RecoveryJournal,
    workflow: Workflow,
    service: str,
) -> bool:
    operation_id = (
        workflow.hotel_release_id if service == "hotel" else workflow.band_release_id
    )
    creation_id = (
        workflow.hotel_operation_id
        if service == "hotel"
        else workflow.band_operation_id
    )
    journal.save(workflow, f"{service}_release_intent")
    outcome = _request_outcome(
        clients[service],
        workflow,
        operation_id,
        release=True,
        resolve_first=True,
        expected_operation_id=creation_id,
    )
    # 404 means the original is gone. A guarded 409 means the slot now belongs
    # to a different reservation, which must be left untouched. A malformed
    # key conflict is not treated as a stale-reservation result.
    gone = outcome.result == "rejected" and (
        isinstance(outcome.error, StaleReservationError)
        or outcome.status == 404
        or (outcome.status == 409 and outcome.reason == "stale_reservation")
    )
    if outcome.result == "success" or gone:
        journal.save(workflow, f"{service}_compensation_confirmed")
        return True
    _transition(
        journal,
        workflow,
        workflow.state,
        f"{service}_compensation_unresolved",
        outcome.error,
    )
    return False


def _compensate(
    clients: dict[str, ReservationApi], journal: RecoveryJournal, workflow: Workflow
) -> bool:
    _transition(journal, workflow, "compensating", "hotel_compensation_required")
    if _release_owned(clients, journal, workflow, "hotel"):
        _transition(journal, workflow, "aborted", "compensation_complete")
        return True
    return False


def _compensate_pair(
    clients: dict[str, ReservationApi], journal: RecoveryJournal, workflow: Workflow
) -> bool:
    _transition(journal, workflow, "pair_compensating", "pair_compensation_required")
    hotel = _release_owned(clients, journal, workflow, "hotel")
    band = _release_owned(clients, journal, workflow, "band")
    if hotel and band:
        _transition(journal, workflow, "aborted", "pair_compensation_complete")
        return True
    return False


def _reservation_current(
    client: ReservationApi, operation_id: str, slot_id: int
) -> bool | None:
    try:
        receipt = client.get_operation(operation_id)
    except Exception:  # noqa: BLE001 - a failed read cannot authorize compensation
        return None
    if (
        not isinstance(receipt, dict)
        or receipt.get("operation_id") != operation_id
        or receipt.get("method") != "POST"
        or str(receipt.get("slot_id")) != str(slot_id)
        or receipt.get("status") != 200
        or receipt.get("reservation_operation_id") != operation_id
        or not isinstance(receipt.get("reservation_current"), bool)
    ):
        return None
    return receipt["reservation_current"]


def _finish_pair(
    clients: dict[str, ReservationApi], journal: RecoveryJournal, workflow: Workflow
) -> bool:
    _transition(journal, workflow, "pair_check", "current_reservations_check")
    hotel = _reservation_current(
        clients["hotel"], workflow.hotel_operation_id, workflow.slot_id
    )
    band = _reservation_current(
        clients["band"], workflow.band_operation_id, workflow.slot_id
    )
    if hotel is None or band is None:
        journal.save(workflow, "current_reservation_check_unresolved")
        return False
    if hotel and band:
        _transition(journal, workflow, "matched", "current_matching_pair_confirmed")
        return True
    _compensate_pair(clients, journal, workflow)
    return False


def _report_pending(journal: RecoveryJournal, message: str) -> None:
    print(message)
    if journal.path is None:
        print(
            "This in-memory journal lasts only for this call. Use the CLI or pass "
            "RecoveryJournal(path) to retain unresolved work for restart recovery."
        )


def _recover_one(
    clients: dict[str, ReservationApi], journal: RecoveryJournal, workflow: Workflow
) -> None:
    if workflow.state == "hotel_pending":
        outcome = _request_outcome(
            clients["hotel"], workflow, workflow.hotel_operation_id, resolve_first=True
        )
        if outcome.result == "unknown":
            _transition(
                journal,
                workflow,
                "hotel_pending",
                "hotel_outcome_unknown",
                outcome.error,
            )
            return
        if outcome.result == "rejected":
            _transition(journal, workflow, "aborted", "hotel_reserve_rejected")
            return
        _transition(journal, workflow, "hotel_reserved", "hotel_reserve_confirmed")
    if workflow.state == "hotel_reserved":
        # A restart before the band intent aborts this interrupted candidate.
        # It does not create a new band booking without the user's action.
        _compensate(clients, journal, workflow)
    elif workflow.state == "band_pending":
        hotel_current = _reservation_current(
            clients["hotel"], workflow.hotel_operation_id, workflow.slot_id
        )
        if hotel_current is None:
            journal.save(workflow, "hotel_current_reservation_unknown")
            return
        outcome = _request_outcome(
            clients["band"], workflow, workflow.band_operation_id, resolve_first=True
        )
        if outcome.result == "success":
            if hotel_current:
                _finish_pair(clients, journal, workflow)
            else:
                _compensate_pair(clients, journal, workflow)
        elif outcome.result == "rejected":
            _compensate(clients, journal, workflow)
        else:
            _transition(
                journal, workflow, "band_pending", "band_outcome_unknown", outcome.error
            )
    elif workflow.state == "compensating":
        _compensate(clients, journal, workflow)
    elif workflow.state == "pair_check":
        _finish_pair(clients, journal, workflow)
    elif workflow.state == "pair_compensating":
        _compensate_pair(clients, journal, workflow)


def recover_pending(
    clients: dict[str, ReservationApi],
    journal: RecoveryJournal,
    *,
    announce: bool = True,
) -> RecoverySummary:
    """Resolve this account's attempts; retain all uncertain outcomes for retry."""
    with journal.locked():
        counts = {"matched": 0, "aborted": 0, "unresolved": 0}
        for workflow in journal.pending(client_scope(clients)):
            _recover_one(clients, journal, workflow)
            key = (
                workflow.state
                if workflow.state in ("matched", "aborted")
                else "unresolved"
            )
            counts[key] += 1
        summary = RecoverySummary(**counts)
        if announce and any(counts.values()):
            print(
                f"Recovery: {summary.matched} matched, {summary.aborted} rolled back, "
                f"{summary.unresolved} unresolved."
            )
            if summary.unresolved:
                print(
                    "Unresolved attempts remain recorded; retry when the API is reachable."
                )
        return summary


def reserve_best_match(
    clients: dict[str, ReservationApi], *, journal: RecoveryJournal | None = None
) -> None:
    """Reserve the earliest available pair without deleting unrelated holdings."""
    if journal is None:
        with RecoveryJournal() as temporary_journal:
            reserve_best_match(clients, journal=temporary_journal)
        return
    with journal.locked():
        recover_pending(clients, journal)
        if journal.pending(client_scope(clients)):
            print(
                "Resolve the pending booking before starting another automatic attempt."
            )
            return
        held = current_reservations(clients)
        already_matched = sorted(set(held["hotel"]) & set(held["band"]))
        if already_matched:
            print(f"Already matched on slot {already_matched[0]}.")
            return
        candidates = matching_slots(clients)
        if not candidates:
            print("No matching slot is currently available.")
            return

        for slot_id in candidates:
            workflow = journal.create(client_scope(clients), slot_id)
            hotel = _request_outcome(
                clients["hotel"], workflow, workflow.hotel_operation_id
            )
            if hotel.result == "unknown":
                _transition(
                    journal,
                    workflow,
                    "hotel_pending",
                    "hotel_outcome_unknown",
                    hotel.error,
                )
                _report_pending(
                    journal, "Hotel outcome is unresolved and recorded for recovery."
                )
                return
            if hotel.result == "rejected":
                _transition(journal, workflow, "aborted", "hotel_reserve_rejected")
                if isinstance(hotel.error, SlotUnavailableError) or hotel.status == 409:
                    continue
                if hotel.error:
                    raise hotel.error
                print("Hotel reservation was rejected.")
                return

            _transition(journal, workflow, "hotel_reserved", "hotel_reserve_confirmed")
            _transition(journal, workflow, "band_pending", "band_reserve_intent")
            band = _request_outcome(
                clients["band"], workflow, workflow.band_operation_id
            )
            if band.result == "unknown":
                _transition(
                    journal,
                    workflow,
                    "band_pending",
                    "band_outcome_unknown",
                    band.error,
                )
                _report_pending(
                    journal,
                    "Band outcome is unresolved; both results are recorded for recovery.",
                )
                return
            if band.result == "rejected":
                if not _compensate(clients, journal, workflow):
                    _report_pending(
                        journal,
                        "Hotel rollback is unresolved and recorded for recovery.",
                    )
                    return
                if isinstance(band.error, SlotUnavailableError) or band.status == 409:
                    continue
                if band.error:
                    raise band.error
                print(
                    "Band reservation was rejected; hotel reservation was rolled back."
                )
                return
            if _finish_pair(clients, journal, workflow):
                print(f"Reserved matching hotel and band slot {slot_id}.")
            elif workflow.state == "aborted":
                print(
                    "The matching reservations changed; this attempt was safely rolled back."
                )
            else:
                _report_pending(
                    journal,
                    "Current reservations are unresolved and recorded for recovery.",
                )
            return
        print("Matching slots changed before they could be reserved; try again.")


def print_menu() -> None:
    print(
        """
====== Wedding Planner ======
1) View current reservations
2) View first 20 available slots
3) Reserve a slot
4) Cancel a reservation
5) View first 5 matching slots
6) Automatically reserve the best match
7) Remove unnecessary reservations
0) Exit
============================="""
    )


def _run_menu(clients: dict[str, ReservationApi], journal: RecoveryJournal) -> None:
    actions = {
        "1": show_reservations,
        "2": show_available,
        "3": reserve_manually,
        "4": cancel_manually,
        "5": show_matching,
        "6": lambda active_clients: reserve_best_match(active_clients, journal=journal),
        "7": cleanup_reservations,
    }
    print("Wedding Planner connected.")
    recover_pending(clients, journal)
    while True:
        print_menu()
        choice = input("Choose an option: ").strip()
        if choice == "0":
            print("Goodbye.")
            return
        action = actions.get(choice)
        if not action:
            print("Invalid option.")
            continue
        try:
            action(clients)
        except Exception as error:  # noqa: BLE001 - keep the interactive CLI running
            print(f"Operation failed: {error}")


def main() -> None:
    config = load_config()
    clients = create_clients(config)
    configured_path = os.getenv(
        "BOOKING_JOURNAL_PATH",
        config.get("global", "journal", fallback="data/booking-journal.db"),
    )
    journal_path = Path(configured_path)
    if not journal_path.is_absolute():
        journal_path = ROOT / journal_path
    with RecoveryJournal(journal_path) as journal:
        try:
            # One active interactive planner per journal. A process crash
            # releases the OS lock automatically before startup recovery.
            with journal.locked():
                _run_menu(clients, journal)
        except WorkflowBusyError as error:
            print(f"Planner not started: {error}")


if __name__ == "__main__":
    main()
