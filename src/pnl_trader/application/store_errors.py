"""Application-level failures exposed by state-store ports."""


class StateVersionConflict(RuntimeError):
    """A write was based on a state version that is no longer current."""


class ActiveIntentConflict(RuntimeError):
    """A symbol already has an unfinished trading intent."""


class ReservationLimitExceeded(RuntimeError):
    """Active local margin reservations exceed the approved account limit."""


class IntentConflict(RuntimeError):
    """An idempotency key was reused for different intent parameters."""


class IntentTransitionError(RuntimeError):
    """An intent status change is missing or out of order."""
