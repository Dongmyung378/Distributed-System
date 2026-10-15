"""Reservation API client errors."""

from requests.exceptions import HTTPError, RequestException


class BadRequestError(RequestException):
    """The request was malformed (HTTP 400)."""


class InvalidTokenError(RequestException):
    """The authentication token was invalid or missing (HTTP 401)."""


class BadSlotError(RequestException):
    """The requested slot does not exist (HTTP 403)."""


class NotProcessedError(RequestException):
    """The reservation is not held by this user (HTTP 404)."""


class SlotUnavailableError(RequestException):
    """Another user already holds the slot (HTTP 409)."""


class ReservationLimitError(RequestException):
    """The user already holds the maximum reservations (HTTP 451)."""


class IdempotencyConflictError(SlotUnavailableError):
    """An operation key was reused for a different request (HTTP 409)."""


class StaleReservationError(SlotUnavailableError):
    """A guarded cancellation targets a different reservation (HTTP 409)."""


class OperationUncertainError(HTTPError):
    """A mutating request exhausted retries without a conclusive response."""

    def __init__(self, message, *, operation_id, method, last_error=None):
        super().__init__(message)
        self.operation_id = operation_id
        self.method = method
        self.last_error = last_error
