"""
Pre-flight guard validators for Dialectica on-chain actions.

Translated from the Dialectica JS reference ``canBet`` / ``canChallenge``
snippets into Python.  Each guard is a strict pre-flight that MUST pass
before spending gas or LLM tokens.

Single-claim guards use direct RPC reads.
Batch mode (``can_bet_batch``) uses Multicall3 to validate N claims in
a single RPC call — critical for avoiding Infura/Alchemy rate limits.

    canBet:
        - Treasury balance >= minBet
        - Round is not closed
        - block.timestamp <= bettingDeadline
        - Circuit breaker not active

    canChallenge:
        - Treasury balance >= minBet (bounty)
        - Round is not closed
        - Reveal period has ended  (now >= bettingDeadline + revealPeriod)
        - Challenge period not over (now <= bettingDeadline + revealPeriod + challengePeriod)
        - Circuit breaker not active

Bit-shifting:
    roundId = (claimId << 128) | roundIndex
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from loguru import logger
from web3 import Web3
from eth_abi.abi import decode as abi_decode

ROUND_ID_MASK = (1 << 128) - 1

def encode_round_id(claim_id: int, round_index: int) -> int:
    """Pack (claimId, roundIndex) into a single uint256 roundId."""
    return (claim_id << 128) | (round_index & ROUND_ID_MASK)

def decode_round_id(round_id: int) -> tuple[int, int]:
    """Unpack a uint256 roundId into (claimId, roundIndex)."""
    claim_id = round_id >> 128
    round_index = round_id & ROUND_ID_MASK
    return claim_id, round_index

# ─── Safe rounds() reader ───────────────────────────────────────────────────────
# The composite roundId = (claimId << 128) | roundIndex can be ~2^131, which
# overflows eth_abi's UnsignedIntegerEncoder when passed through web3.py's
# contract abstraction (wallet.rounds.functions.rounds(round_id).call()).
#
# We bypass that by manually encoding the uint256 as 32-byte big-endian and
# issuing a raw eth_call.
# ────────────────────────────────────────────────────────────────────────────────

_ROUNDS_SELECTOR = Web3.keccak(text="rounds(uint256)")[:4]


def _raw_call_rounds(w3: Web3, rounds_address: str, round_id: int) -> tuple:
    """
    Call ``Rounds.rounds(uint256)`` via raw ``eth_call`` with manual
    uint256 encoding.  Returns the ABI-decoded tuple:
        (bettingAmount, bettingDeadline, poolAmount, result, isClosed)

    Raises on RPC failure so callers can handle it.
    """
    # Encode roundId as 32-byte hex string
    hex_round_id = hex(round_id)[2:].zfill(64)
    encoded_id = bytes.fromhex(hex_round_id)
    calldata = _ROUNDS_SELECTOR + encoded_id
    raw = w3.eth.call({
        "to": Web3.to_checksum_address(rounds_address),
        "data": calldata,
    })
    # (uint256 bettingAmount, uint256 bettingDeadline, uint256 poolAmount,
    #  uint8 result, bool isClosed)
    return abi_decode(
        ["uint256", "uint256", "uint256", "uint8", "bool"],
        raw,
    )

__all__ = [
    "can_bet",
    "can_challenge",
    "can_bet_batch",
    "validate_betting_eligibility",   # alias kept for backward compat
    "validate_challenge_eligibility", # alias kept for backward compat
]


# ─── Result container ──────────────────────────────────────────────────────────

@dataclass
class GuardResult:
    """Immutable pre-flight verdict."""
    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok

    def as_tuple(self) -> tuple[bool, str]:
        return self.ok, self.reason


PASS = GuardResult(ok=True, reason="OK")


def _fail(reason: str, tag: str = "") -> GuardResult:
    if tag:
        logger.warning(f"{tag} BLOCKED — {reason}")
    return GuardResult(ok=False, reason=reason)


# ─── canBet (single claim) ─────────────────────────────────────────────────────

async def can_bet(
    wallet,
    claim_id: int,
    collection_id: int | None = None,
    round_index: int = 0,
    treasury_cache: float | None = None,
) -> GuardResult:
    """
    JS-reference ``canBet`` translated to Python.

    Checks (in order):
        1. Circuit breaker (``betting_paused``)
        2. Treasury balance >= minBet
        3. Round ``isClosed == false``
        4. ``block.timestamp <= bettingDeadline``

    Parameters
    ----------
    wallet : Wallet
    claim_id : int           – raw claimId (not composite)
    collection_id : int|None – for getCoreParams; defaults to 1
    round_index : int        – current round (0 = initial)
    treasury_cache : float|None – skip RPC if provided
    """
    tag = f"[canBet claim={claim_id} r={round_index}]"

    # 1 — Circuit breaker
    if wallet.betting_paused:
        return _fail("Betting paused (InsufficientBalance circuit breaker)", tag)

    # 2 — Treasury >= minBet
    try:
        min_bet_raw = wallet.fetch_min_bet(collection_id)
        min_bet_usdc = min_bet_raw / 1e6

        treasury_usdc = treasury_cache if treasury_cache is not None else await wallet.get_treasury_balance()
        if treasury_usdc < min_bet_usdc:
            return _fail(
                f"Treasury ({treasury_usdc:.2f}) < minBet ({min_bet_usdc:.2f} USDC)", tag
            )
    except Exception as exc:
        return _fail(f"Treasury/minBet read failed: {exc}", tag)

    # 3-4 — On-chain round state (manual uint256 encoding to avoid overflow)
    try:
        round_id = encode_round_id(claim_id, round_index)
        rd = _raw_call_rounds(wallet.w3, wallet.rounds_address, round_id)
        # rd: (bettingAmount, bettingDeadline, poolAmount, result, isClosed)

        if rd[4]:  # isClosed
            return _fail(f"Round {round_index} is closed on-chain", tag)

        betting_deadline = rd[1]
        now_ts = int(time.time())
        if betting_deadline > 0 and now_ts > betting_deadline:
            return _fail(
                f"Betting deadline passed (deadline={betting_deadline}, "
                f"now={now_ts}, overdue={now_ts - betting_deadline}s)", tag
            )
    except Exception as exc:
        logger.warning(f"{tag} Could not read round data: {exc}. Proceeding cautiously.")

    logger.info(f"{tag} PASSED")
    return PASS


# ─── canChallenge (single claim) ───────────────────────────────────────────────

async def can_challenge(
    wallet,
    claim_id: int,
    collection_id: int | None = None,
    round_index: int = 0,
    treasury_cache: float | None = None,
) -> GuardResult:
    """
    JS-reference ``canChallenge`` translated to Python.

    Checks (in order):
        1. Circuit breaker
        2. Treasury balance >= minBet (bounty requirement)
        3. Round ``isClosed == false``
        4. Reveal period ended (challenge window open)
        5. Challenge deadline not passed

    Time windows (derived from getCoreParams):
        revealEnd      = bettingDeadline + revealPeriod
        challengeEnd   = revealEnd + challengePeriod
        canChallenge   = revealEnd <= now <= challengeEnd
    """
    tag = f"[canChallenge claim={claim_id} r={round_index}]"

    # 1 — Circuit breaker
    if wallet.betting_paused:
        return _fail("Betting paused (InsufficientBalance circuit breaker)", tag)

    # 2 — Treasury >= bountyAmount (explicit check)
    try:
        min_bet_raw = wallet.fetch_min_bet(collection_id)
        min_bet_usdc = min_bet_raw / 1e6

        treasury_usdc = treasury_cache if treasury_cache is not None else await wallet.get_treasury_balance()
        # Bounty check: ensure treasury covers bountyAmount (0.86 USDC)
        bounty_amount = 0.86
        if treasury_usdc < bounty_amount:
            return _fail(
                f"Treasury ({treasury_usdc:.2f}) < bountyAmount ({bounty_amount:.2f} USDC)", tag
            )
        if treasury_usdc < min_bet_usdc:
            return _fail(
                f"Treasury ({treasury_usdc:.2f}) < minBet ({min_bet_usdc:.2f} USDC)", tag
            )
    except Exception as exc:
        return _fail(f"Treasury/minBet/bounty read failed: {exc}", tag)

    # 3 — Round not closed (manual uint256 encoding to avoid overflow)
    betting_deadline = 0
    try:
        round_id = encode_round_id(claim_id, round_index)
        rd = _raw_call_rounds(wallet.w3, wallet.rounds_address, round_id)

        if rd[4]:  # isClosed
            return _fail(f"Round {round_index} already closed", tag)

        betting_deadline = rd[1]
    except Exception as exc:
        logger.warning(f"{tag} Could not read round data: {exc}. Proceeding cautiously.")

    # 4-5 — Challenge window
    if betting_deadline > 0:
        try:
            core = wallet.collections.functions.getCoreParams(collection_id or 1).call()
            # If getCoreParams doesn't return periods anymore (e.g., returns 4 items), skip estimation
            if len(core) >= 5:
                reveal_period = core[3]
                challenge_period = core[4]

                reveal_end = betting_deadline + reveal_period
                challenge_end = reveal_end + challenge_period
                now_ts = int(time.time())

                if now_ts < reveal_end:
                    return _fail(
                        f"Reveal period not ended (ends {reveal_end}, now {now_ts}, "
                        f"remaining {reveal_end - now_ts}s)", tag
                    )
                if now_ts > challenge_end:
                    return _fail(
                        f"Challenge deadline passed (ended {challenge_end}, "
                        f"now {now_ts}, overdue {now_ts - challenge_end}s)", tag
                    )
            else:
                logger.debug(f"{tag} getCoreParams returned {len(core)} items; skipping full challenge window estimation.")
        except Exception as exc:
            logger.warning(f"{tag} Could not estimate challenge window: {exc}. Proceeding cautiously.")

    logger.info(f"{tag} PASSED")
    return PASS


# ─── canBet Batch (Multicall3) ──────────────────────────────────────────────────

def can_bet_batch(
    wallet,
    claim_round_pairs: list[tuple[int, int]],
    collection_id: int = 1,
) -> list[GuardResult]:
    """
    Validate betting eligibility for N claims in a **single RPC call** via
    Multicall3.  Returns one ``GuardResult`` per input pair, in order.

    This prevents rate-limiting by Infura/Alchemy when the bot tracks 10+
    claims per cycle.

    Parameters
    ----------
    wallet : Wallet
        Must have ``w3``, ``rounds_address``, ``treasury_address``,
        ``collections_address``, ``account``.
    claim_round_pairs : list[(claim_id, round_index)]
    collection_id : int
    """
    from .multicall import Multicall

    if not claim_round_pairs:
        return []

    if wallet.betting_paused:
        return [_fail("Betting paused (circuit breaker)", "")] * len(claim_round_pairs)

    if not wallet.account:
        return [_fail("No wallet account", "")] * len(claim_round_pairs)

    mc = Multicall(wallet.w3)

    try:
        treasury_raw, core_params, round_data_list = mc.preflight_batch(
            rounds_address=wallet.rounds_address,
            treasury_address=wallet.treasury_address,
            collections_address=wallet.collections_address,
            user_address=wallet.account.address,
            claim_round_pairs=claim_round_pairs,
            collection_id=collection_id,
        )
    except Exception as exc:
        logger.error(f"Multicall preflight_batch failed: {exc}")
        # Financial execution must fail closed when eligibility cannot be verified.
        return [_fail("Pre-flight RPC check failed", "[canBet:batch]")] * len(claim_round_pairs)

    # Extract minBet from core_params
    min_bet_raw = core_params["minBet"] if core_params else (wallet._cached_min_bet or 0)
    treasury_usdc = treasury_raw / 1e6
    min_bet_usdc = min_bet_raw / 1e6

    # Cache the fetched values
    if core_params and core_params["minBet"] > 0:
        wallet._cached_min_bet = core_params["minBet"]

    now_ts = int(time.time())
    results: list[GuardResult] = []

    for i, (claim_id, round_index) in enumerate(claim_round_pairs):
        tag = f"[canBet:batch claim={claim_id} r={round_index}]"

        # Treasury check
        if treasury_usdc < min_bet_usdc:
            results.append(_fail(
                f"Treasury ({treasury_usdc:.2f}) < minBet ({min_bet_usdc:.2f} USDC)", tag
            ))
            continue

        rd = round_data_list[i] if i < len(round_data_list) else None

        if rd is None:
            logger.warning(f"{tag} No round data from Multicall. Rejecting execution.")
            results.append(_fail("Pre-flight round data unavailable", tag))
            continue

        if rd["isClosed"]:
            results.append(_fail(f"Round {round_index} is closed", tag))
            continue

        deadline = rd["bettingDeadline"]
        if deadline > 0 and now_ts > deadline:
            results.append(_fail(
                f"Betting deadline passed (deadline={deadline}, now={now_ts})", tag
            ))
            continue

        logger.info(f"{tag} PASSED")
        results.append(PASS)

    logger.info(
        f"canBet batch: {sum(1 for r in results if r.ok)}/{len(results)} eligible"
    )
    return results


# ─── Backward-compatible aliases ────────────────────────────────────────────────
# These match the signatures used in main.py from the prior session.

async def validate_betting_eligibility(
    wallet,
    claim_id: int,
    collection_id: int | None = None,
    round_index: int = 0,
    treasury_cache: float | None = None,
) -> tuple[bool, str]:
    """Backward-compatible wrapper → returns (bool, str)."""
    result = await can_bet(wallet, claim_id, collection_id, round_index, treasury_cache)
    return result.as_tuple()


async def validate_challenge_eligibility(
    wallet,
    claim_id: int,
    collection_id: int | None = None,
    round_index: int = 0,
    treasury_cache: float | None = None,
) -> tuple[bool, str]:
    """Backward-compatible wrapper → returns (bool, str)."""
    result = await can_challenge(wallet, claim_id, collection_id, round_index, treasury_cache)
    return result.as_tuple()
