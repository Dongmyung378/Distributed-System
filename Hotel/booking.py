#!/usr/bin/env python3
"""Interactive wedding planner for matching hotel and band slots."""

from __future__ import annotations

import configparser
import os
from pathlib import Path

from exceptions import SlotUnavailableError
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


def reserve_best_match(clients: dict[str, ReservationApi]) -> None:
    held = current_reservations(clients)
    already_matched = sorted(set(held["hotel"]) & set(held["band"]))
    if already_matched:
        print(f"Already matched on slot {already_matched[0]}.")
        cleanup_reservations(clients)
        return

    cleanup_reservations(clients)
    candidates = matching_slots(clients)
    if not candidates:
        print("No matching slot is currently available.")
        return

    for slot_id in candidates:
        try:
            clients["hotel"].reserve_slot(slot_id)
        except SlotUnavailableError:
            continue

        try:
            clients["band"].reserve_slot(slot_id)
        except SlotUnavailableError:
            clients["hotel"].release_slot(slot_id)
            continue
        except Exception:
            clients["hotel"].release_slot(slot_id)
            raise

        cleanup_reservations(clients)
        print(f"Reserved matching hotel and band slot {slot_id}.")
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


def main() -> None:
    clients = create_clients(load_config())
    actions = {
        "1": show_reservations,
        "2": show_available,
        "3": reserve_manually,
        "4": cancel_manually,
        "5": show_matching,
        "6": reserve_best_match,
        "7": cleanup_reservations,
    }
    print("Wedding Planner connected.")
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


if __name__ == "__main__":
    main()
