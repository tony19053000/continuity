"""Typed error hierarchy.

Every error that reaches a client carries a stable machine-readable `code` and a
message safe to display. Internal detail — stack traces, file paths, SQL, model
output — stays in the logs and never enters a response body
(`03_SECURITY_ACCESS.md` §2).
"""

from __future__ import annotations

from typing import Any


class ContinuityError(Exception):
    """Base class for every deliberate Continuity failure.

    `code` is the stable identifier clients may branch on. `detail` is optional
    structured context that is safe to expose; anything unsafe belongs in the
    log record, not here.
    """

    code: str = "internal_error"
    status_code: int = 500
    message: str = "An internal error occurred."

    def __init__(self, message: str | None = None, **detail: Any) -> None:
        self.message = message or self.message
        self.detail = detail
        super().__init__(self.message)

    def to_response(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            body["detail"] = self.detail
        return body


# --- Client-caused -------------------------------------------------------


class ValidationFailed(ContinuityError):
    code = "validation_failed"
    status_code = 422
    message = "The request was not valid."


class NotFound(ContinuityError):
    code = "not_found"
    status_code = 404
    message = "The requested resource does not exist."


class AuthenticationRequired(ContinuityError):
    code = "authentication_required"
    status_code = 401
    message = "Authentication is required."


class PermissionDenied(ContinuityError):
    code = "permission_denied"
    status_code = 403
    message = "You do not have access to this resource."


# --- Configuration and integration state --------------------------------


class IntegrationNotConfigured(ContinuityError):
    """An optional integration was used before it was configured.

    Deliberately distinct from a generic 500: the operator must add
    configuration, and the message names what is missing without revealing any
    value.
    """

    code = "integration_not_configured"
    status_code = 503
    message = "This feature requires configuration that is not present."

    def __init__(self, integration: str, reason: str) -> None:
        super().__init__(
            f"{integration} is not configured.",
            integration=integration,
            reason=reason,
        )


# --- Workflow ------------------------------------------------------------


class IllegalTransition(ContinuityError):
    """A run was asked to move to a state not reachable from its current one.

    This is a programming error or a race, never something a user can cause.
    Raising it protects the state machine's guarantee (`02_ARCHITECTURE.md` §8).
    """

    code = "illegal_transition"
    status_code = 409
    message = "That state transition is not allowed."

    def __init__(self, from_state: str, to_state: str) -> None:
        super().__init__(
            f"Cannot transition from {from_state} to {to_state}.",
            from_state=from_state,
            to_state=to_state,
        )


class ApprovalRequired(ContinuityError):
    """A protected action was attempted without a stored APPROVED record."""

    code = "approval_required"
    status_code = 409
    message = "This action requires human approval that has not been granted."


class PolicyDenied(ContinuityError):
    """The policy engine classified an action as DENY.

    Always audited by the caller before it is raised
    (`03_SECURITY_ACCESS.md` §4).
    """

    code = "policy_denied"
    status_code = 403
    message = "This action is not permitted."
