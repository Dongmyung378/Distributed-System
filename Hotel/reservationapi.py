"""HTTP client for the hotel and band reservation services."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urljoin

import requests
from exceptions import (
    BadRequestError,
    BadSlotError,
    InvalidTokenError,
    NotProcessedError,
    ReservationLimitError,
    SlotUnavailableError,
)

CLIENT_ERRORS = {
    400: BadRequestError,
    401: InvalidTokenError,
    403: BadSlotError,
    404: NotProcessedError,
    409: SlotUnavailableError,
    451: ReservationLimitError,
}


class ReservationApi:
    """Small, retrying client for the reservation HTTP contract."""

    def __init__(
        self,
        base_url: str,
        token: str,
        retries: int = 3,
        delay: float = 0.5,
        timeout: float = 10.0,
    ) -> None:
        if retries < 1:
            raise ValueError("retries must be at least 1")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.retries = retries
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def service_name(self) -> str:
        return "Hotel" if "/hotel/" in self.base_url.lower() else "Band"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _reason(response: requests.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.reason or f"HTTP {response.status_code}"
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])
        return response.reason or f"HTTP {response.status_code}"

    def _send_request(self, method: str, endpoint: str) -> Any:
        url = urljoin(self.base_url, endpoint.lstrip("/"))
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    timeout=self.timeout,
                )

                if 200 <= response.status_code < 300:
                    return response.json() if response.content else {}

                reason = self._reason(response)
                error_type = CLIENT_ERRORS.get(response.status_code)
                if error_type:
                    raise error_type(reason)

                if response.status_code < 500:
                    response.raise_for_status()

                last_error = requests.HTTPError(
                    f"{response.status_code} Server Error: {reason}",
                    response=response,
                )
            except tuple(CLIENT_ERRORS.values()):
                raise
            except requests.RequestException as error:
                last_error = error

            if attempt < self.retries:
                wait = self.delay * attempt
                print(
                    f"[{self.service_name}] request failed "
                    f"({attempt}/{self.retries}); retrying in {wait:.1f}s"
                )
                time.sleep(wait)

        raise requests.HTTPError(
            f"[{self.service_name}] request failed after {self.retries} attempts: "
            f"{last_error}"
        ) from last_error

    def get_slots_available(self) -> list[dict[str, str]]:
        return self._send_request("GET", "reservation/available")

    def get_slots_held(self) -> list[dict[str, str]]:
        return self._send_request("GET", "reservation")

    def release_slot(self, slot_id: str | int) -> dict[str, str]:
        return self._send_request("DELETE", f"reservation/{slot_id}")

    def reserve_slot(self, slot_id: str | int) -> dict[str, str]:
        return self._send_request("POST", f"reservation/{slot_id}")
