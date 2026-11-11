"""Errors. Every failure is explicit; nothing fails silently."""

class T12Error(Exception):
    """Base error. Message is what the operator sees."""


class StateError(T12Error):
    """Invalid phase transition or run state."""


class BudgetError(T12Error):
    """A time, operation, or repair budget is exhausted."""


class EvidenceError(T12Error):
    """Evidence is missing, stale, tampered with, or mis-bound."""


class RunnerError(T12Error):
    """A command could not be launched or behaved unexpectedly."""


class GitError(T12Error):
    """A git invariant failed."""
