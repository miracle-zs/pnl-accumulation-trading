"""Application-level failures exposed by state-store ports."""


class ActiveIntentConflict(RuntimeError):
    """A symbol already has an unfinished trading intent."""


class IntentConflict(RuntimeError):
    """An order intent conflicts with the current state or prior execution."""
