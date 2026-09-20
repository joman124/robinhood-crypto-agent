"""Exception hierarchy.

Every failure mode that should stop an order from reaching Robinhood raises a
subclass of :class:`AgentError`. The CLI turns these into a non-zero exit and a
one-line reason, so a mistake fails loudly instead of producing a plausible but
wrong order payload.
"""

from __future__ import annotations


class AgentError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(AgentError):
    """Configuration is missing, malformed, or violates a hard code ceiling."""


class ContractViolation(AgentError):
    """An MCP tool payload would violate the RobinHood tool contract.

    Raised *before* a payload is handed to Claude, so a malformed order is
    never submitted in the first place.
    """


class InsufficientHistory(AgentError):
    """Not enough observed price history to evaluate an indicator honestly."""


class KillSwitchEngaged(AgentError):
    """The kill switch is on; no proposal may be turned into an order."""


class ApprovalError(AgentError):
    """A human approval is missing, ambiguous, or does not match the proposal."""


class AuditError(AgentError):
    """The audit log is unreadable, or a write would corrupt it."""
