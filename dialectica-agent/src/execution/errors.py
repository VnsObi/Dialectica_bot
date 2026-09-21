"""
Custom error decoding for Dialectica smart contract reverts.

Solidity custom errors are ABI-encoded: the first 4 bytes of the revert data
are the selector (keccak256 of the error signature), followed by ABI-encoded
parameters.

We decode three known errors:
  • NoBetting()              — selector 0x (no params)
  • RoundNotActive()         — selector 0x (no params)
  • InsufficientBalance()    — selector 0x (no params)
  • InvalidBetAmount(uint256 minBet, uint256 maxBet)
"""

from web3 import Web3
from eth_abi import decode as abi_decode
from loguru import logger


# ─── Error Registry ─────────────────────────────────────────────────────────

def _selector(sig: str) -> bytes:
    """Compute the 4-byte selector for a Solidity error signature."""
    return Web3.keccak(text=sig)[:4]


# Pre-compute selectors at import time
ERROR_SELECTORS: dict[bytes, str] = {
    _selector("NoBetting()"): "NoBetting",
    _selector("RoundNotActive()"): "RoundNotActive",
    _selector("InsufficientBalance()"): "InsufficientBalance",
    _selector("InsufficientBalance(uint256)"): "InsufficientBalanceAmount",  # 0x92665351
    _selector("InvalidBetAmount(uint256,uint256)"): "InvalidBetAmount",
    _selector("InvalidClaimStatus()"): "InvalidClaimStatus",
    _selector("OngoingBetting()"): "OngoingBetting",
    _selector("OngoingChallenge()"): "OngoingChallenge",
    _selector("NoChallenge()"): "NoChallenge",
}

# Parameter types for errors that carry data
ERROR_PARAMS: dict[str, list[str]] = {
    "InvalidBetAmount": ["uint256", "uint256"],  # (minBet, maxBet)
    "InsufficientBalanceAmount": ["uint256"],      # (currentBalance)
}


# ─── Custom Exceptions ──────────────────────────────────────────────────────

class ContractRevertError(Exception):
    """Base class for decoded contract reverts."""

    def __init__(self, error_name: str, raw_data: bytes | None = None, params: dict | None = None):
        self.error_name = error_name
        self.raw_data = raw_data
        self.params = params or {}
        super().__init__(f"{error_name}({self.params})" if self.params else error_name)


class NoBettingError(ContractRevertError):
    """Betting window is closed for this claim/round."""

    def __init__(self, raw_data: bytes | None = None):
        super().__init__("NoBetting", raw_data)


class RoundNotActiveError(ContractRevertError):
    """The round is no longer active."""

    def __init__(self, raw_data: bytes | None = None):
        super().__init__("RoundNotActive", raw_data)


class InsufficientBalanceError(ContractRevertError):
    """Treasury balance is too low to place this bet.

    Matches both ``InsufficientBalance()`` (0xf4d678b8) and the
    parameterised ``InsufficientBalance(uint256)`` (0x92665351).
    """

    def __init__(self, current_balance: int | None = None, raw_data: bytes | None = None):
        self.current_balance = current_balance
        params = {"currentBalance": current_balance} if current_balance is not None else {}
        super().__init__("InsufficientBalance", raw_data, params)


class InvalidBetAmountError(ContractRevertError):
    """Bet amount is outside the allowed range. Carries (minBet, maxBet)."""

    def __init__(self, min_bet: int = 0, max_bet: int = 0, raw_data: bytes | None = None):
        self.min_bet = min_bet
        self.max_bet = max_bet
        super().__init__(
            "InvalidBetAmount",
            raw_data,
            {"minBet": min_bet, "maxBet": max_bet},
        )


# ─── Exception Map ──────────────────────────────────────────────────────────

_EXCEPTION_MAP: dict[str, type] = {
    "NoBetting": NoBettingError,
    "RoundNotActive": RoundNotActiveError,
    "InsufficientBalance": InsufficientBalanceError,
    "InsufficientBalanceAmount": InsufficientBalanceError,  # 0x92665351 variant
    "InvalidBetAmount": InvalidBetAmountError,
}


# ─── Decoder ────────────────────────────────────────────────────────────────

def decode_revert(revert_data: bytes | str) -> ContractRevertError | None:
    """
    Attempt to decode raw EVM revert data into a typed exception.

    Returns None if the data doesn't match any known error selector.
    Accepts hex strings (0x-prefixed) or raw bytes.
    """
    if isinstance(revert_data, str):
        revert_data = bytes.fromhex(revert_data.removeprefix("0x"))

    if len(revert_data) < 4:
        return None

    selector = revert_data[:4]
    error_name = ERROR_SELECTORS.get(selector)

    if error_name is None:
        return None

    # Decode parameters if present
    param_types = ERROR_PARAMS.get(error_name)

    if error_name == "InvalidBetAmount" and param_types and len(revert_data) > 4:
        try:
            decoded = abi_decode(param_types, revert_data[4:])
            return InvalidBetAmountError(
                min_bet=decoded[0], max_bet=decoded[1], raw_data=revert_data
            )
        except Exception as exc:
            logger.warning(f"Failed to decode {error_name} params: {exc}")
            return InvalidBetAmountError(raw_data=revert_data)

    # InsufficientBalance(uint256) → 0x92665351
    if error_name == "InsufficientBalanceAmount" and len(revert_data) > 4:
        try:
            decoded = abi_decode(["uint256"], revert_data[4:])
            return InsufficientBalanceError(
                current_balance=decoded[0], raw_data=revert_data
            )
        except Exception as exc:
            logger.warning(f"Failed to decode InsufficientBalance(uint256) params: {exc}")
            return InsufficientBalanceError(raw_data=revert_data)

    exc_cls = _EXCEPTION_MAP.get(error_name, ContractRevertError)
    if exc_cls in (NoBettingError, RoundNotActiveError, InsufficientBalanceError):
        return exc_cls(raw_data=revert_data)

    return ContractRevertError(error_name, revert_data)


def extract_revert_data(exception: Exception) -> bytes | None:
    """
    Extract raw revert bytes from a Web3 ContractLogicError or similar.

    Web3.py raises ContractLogicError with the message containing the revert
    reason, or wraps it in the general Exception with hex data.  We also try
    to pull from the `data` attribute if present.
    """
    # web3.exceptions.ContractLogicError stores hex in .data or message
    if hasattr(exception, "data") and exception.data:
        data = exception.data
        if isinstance(data, str):
            return bytes.fromhex(data.removeprefix("0x"))
        if isinstance(data, bytes):
            return data

    # Fallback: scan the string representation for a hex blob
    msg = str(exception)
    # Look for 0x-prefixed hex in the message
    for part in msg.split():
        if part.startswith("0x") and len(part) > 10:
            try:
                return bytes.fromhex(part.removeprefix("0x"))
            except ValueError:
                continue

    return None


def decode_exception(exception: Exception) -> ContractRevertError | None:
    """
    Convenience: extract revert data from an exception, then decode it.
    Returns None if no known error is found.
    """
    raw = extract_revert_data(exception)
    if raw is None:
        # Also try matching the error name from the string message
        msg = str(exception)
        for name in ERROR_SELECTORS.values():
            if name in msg:
                exc_cls = _EXCEPTION_MAP.get(name, ContractRevertError)
                if name == "InvalidBetAmount":
                    return InvalidBetAmountError()
                return exc_cls()
        return None
    return decode_revert(raw)
