"""Reservation API client errors."""

from requests.exceptions import RequestException


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
