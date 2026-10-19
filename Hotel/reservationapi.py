"""HTTP client for the hotel and band reservation services."""

from __future__ import annotations

import math
import re
import time
import uuid
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests
from exceptions import (
    BadRequestError,
    BadSlotError,
    IdempotencyConflictError,
    InvalidTokenError,
    NotProcessedError,
    OperationUncertainError,
    ReservationLimitError,
    SlotUnavailableError,
    StaleReservationError,
)

OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

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
        *,
        max_elapsed: float = 60.0,
    ) -> None:
        if (
            isinstance(retries, bool)
            or not isinstance(retries, int)
            or not 1 <= retries <= 10
        ):
            raise ValueError("retries must be an integer between 1 and 10")
        for name, value in (
            ("delay", delay),
            ("timeout", timeout),
            ("max_elapsed", max_elapsed),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be finite")
        if not 0 <= delay <= 60 or not 0 < timeout <= 60 or not 0 < max_elapsed <= 300:
            raise ValueError(
                "delay must be 0..60s, timeout 0..60s, and max_elapsed 0..300s"
            )
        parsed_url = urlsplit(base_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
            raise ValueError("base_url must be an HTTP or HTTPS URL")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token must not be empty")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.retries = retries
        self.delay = delay
        self.timeout = timeout
        self.max_elapsed = max_elapsed
        self.session = requests.Session()

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

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

    @staticmethod
    def _validate_operation_id(operation_id: str) -> str:
        if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(
            operation_id
        ):
            raise ValueError(
                "operation_id must contain 1..128 ASCII identifier characters"
            )
        return operation_id

    def _send_request(
        self,
        method: str,
        endpoint: str,
        *,
        operation_id: str | None = None,
        expected_operation_id: str | None = None,
        allow_missing: bool = False,
        retries: int | None = None,
    ) -> Any:
        attempt_limit = self.retries if retries is None else retries
        if (
            isinstance(attempt_limit, bool)
            or not isinstance(attempt_limit, int)
            or not 1 <= attempt_limit <= 10
        ):
            raise ValueError("retries must be an integer between 1 and 10")
        url = urljoin(self.base_url, endpoint.lstrip("/"))
        last_error: Exception | None = None
        headers = self._headers()
        if method in ("POST", "DELETE"):
            operation_id = self._validate_operation_id(
                uuid.uuid4().hex if operation_id is None else operation_id
            )
            headers["Idempotency-Key"] = operation_id
        if expected_operation_id is not None:
            headers["Expected-Reservation-Operation-Id"] = self._validate_operation_id(
                expected_operation_id
            )
        deadline = time.monotonic() + self.max_elapsed
        attempts = 0

        for attempt in range(1, attempt_limit + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            attempts = attempt
            response = None
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=min(self.timeout, remaining),
                )

                if 200 <= response.status_code < 300:
                    return response.json() if response.content else {}
                if allow_missing and response.status_code == 404:
                    return None

                reason = self._reason(response)
                error_type = CLIENT_ERRORS.get(response.status_code)
                if response.status_code == 409:
                    try:
                        code = response.json().get("error")
                    except (ValueError, AttributeError):
                        code = None
                    error_type = {
                        "idempotency_key_conflict": IdempotencyConflictError,
                        "stale_reservation": StaleReservationError,
                    }.get(code, SlotUnavailableError)
                if error_type:
                    raise error_type(reason, response=response)

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
            finally:
                if response is not None:
                    response.close()

            if attempt < attempt_limit:
                wait = self.delay * attempt
                if time.monotonic() + wait >= deadline:
                    break
                print(
                    f"[{self.service_name}] request failed "
                    f"({attempt}/{attempt_limit}); retrying in {wait:.1f}s"
                )
                time.sleep(wait)

        message = f"[{self.service_name}] request failed after {attempts} attempts: {last_error}"
        if operation_id is not None:
            raise OperationUncertainError(
                message, operation_id=operation_id, method=method, last_error=last_error
            ) from last_error
        raise requests.HTTPError(message) from last_error

    def get_slots_available(self) -> list[dict[str, str]]:
        return self._send_request("GET", "reservation/available")

    def get_slots_held(self) -> list[dict[str, str]]:
        return self._send_request("GET", "reservation")

    def get_operation(
        self, operation_id: str, *, retries: int | None = None
    ) -> dict | None:
        """Read a receipt, optionally overriding attempts within one time budget."""
        operation_id = self._validate_operation_id(operation_id)
        return self._send_request(
            "GET",
            f"operation/{quote(operation_id, safe='')}",
            allow_missing=True,
            retries=retries,
        )

    def release_slot(
        self,
        slot_id: str | int,
        *,
        operation_id: str | None = None,
        expected_operation_id: str | None = None,
    ) -> dict[str, str]:
        return self._send_request(
            "DELETE",
            f"reservation/{slot_id}",
            operation_id=operation_id,
            expected_operation_id=expected_operation_id,
        )

    def reserve_slot(
        self,
        slot_id: str | int,
        *,
        operation_id: str | None = None,
    ) -> dict[str, str]:
        return self._send_request(
            "POST", f"reservation/{slot_id}", operation_id=operation_id
        )
