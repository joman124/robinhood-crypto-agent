"""Robinhood crypto trading agent: strategy, risk, and audit logic.

Execution against Robinhood itself always happens through the
`robinhood-trading` MCP server, called by Claude in an interactive session —
nothing in this package talks to Robinhood directly. See CLAUDE.md and
docs/architecture.md.
"""

__version__ = "0.1.0"
