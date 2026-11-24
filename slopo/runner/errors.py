from __future__ import annotations


class RunError(RuntimeError):
    """Raised when the repository SlopO gate cannot run safely."""


class SkipRun(RuntimeError):
    """Raised when the opt-in local SlopO runtime is unavailable."""


class SignalExit(BaseException):
    """Raised after a termination signal so services can be cleaned up."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
