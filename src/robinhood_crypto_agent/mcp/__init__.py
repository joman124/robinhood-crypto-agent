"""The boundary with the RobinHood MCP server.

Nothing in this package performs I/O. Claude Code holds the MCP connection; this
layer's job is to (a) encode the tool contract so an invalid payload is caught
before Claude ever calls a tool, and (b) parse the JSON that comes back.
"""

from .contract import (
    CRYPTO_TOOLS,
    ORDER_COLLARS,
    ToolContract,
    validate_crypto_order_args,
)
from .parse import (
    parse_accounts,
    parse_currency_pairs,
    parse_order_response,
    parse_positions,
    parse_quotes,
)

__all__ = [
    "CRYPTO_TOOLS",
    "ORDER_COLLARS",
    "ToolContract",
    "validate_crypto_order_args",
    "parse_accounts",
    "parse_currency_pairs",
    "parse_order_response",
    "parse_positions",
    "parse_quotes",
]
