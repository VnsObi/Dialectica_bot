"""
Multicall3 — Batch multiple on-chain view calls into a single RPC request.

Uses the canonical Multicall3 contract deployed on Base (and most EVM chains)
at ``0xcA11bde05977b3631167028862bE2a173976CA11``.

Primary use case: fetching round state for 10+ claims in one RPC call instead
of N individual ``eth_call``s, preventing Infura/Alchemy rate limiting.

Reference: https://github.com/mds1/multicall
"""

from __future__ import annotations

from typing import Any
from loguru import logger
from web3 import Web3
from eth_abi.abi import encode, decode

# ─── Constants ──────────────────────────────────────────────────────────────────

# Multicall3 is deployed at the same address on every EVM chain
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Minimal Multicall3 ABI: aggregate3(Call3[] calls) → Result[]
# Call3:  { target: address, allowFailure: bool, callData: bytes }
# Result: { success: bool, returnData: bytes }
MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

# ─── Selector for Rounds.rounds(uint256) ───────────────────────────────────────
# keccak256("rounds(uint256)")[:4]
ROUNDS_SELECTOR = Web3.keccak(text="rounds(uint256)")[:4]

# Selector for Treasury.balanceOf(address)
BALANCE_OF_SELECTOR = Web3.keccak(text="balanceOf(address)")[:4]

# Selector for Collections.getCoreParams(uint256)
GET_CORE_PARAMS_SELECTOR = Web3.keccak(text="getCoreParams(uint256)")[:4]


# ─── Multicall Helper ──────────────────────────────────────────────────────────

class Multicall:
    """
    Thin wrapper around the Multicall3 contract.

    Usage::

        mc = Multicall(w3)

        # Build calls
        calls = mc.build_rounds_calls(
            rounds_address, [(claim1, 0), (claim2, 1), ...]
        )

        # Execute in one RPC round-trip
        results = mc.execute(calls)

        # Parse
        for ok, data in results:
            if ok:
                parsed = mc.decode_round_data(data)
    """

    def __init__(self, w3: Web3):
        self.w3 = w3
        self.contract = w3.eth.contract(
            address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
            abi=MULTICALL3_ABI,
        )

    # ── Call Builders ───────────────────────────────────────────────────────

    @staticmethod
    def _encode_rounds_call(rounds_address: str, round_id: int) -> tuple:
        """Build a single (target, allowFailure, callData) tuple for rounds(roundId).

        Passes roundId as a hex string to the contract to handle large 128-bit shifts.
        """
        # Convert round_id to 32-byte hex string
        hex_round_id = hex(round_id)[2:].zfill(64)
        encoded_id = bytes.fromhex(hex_round_id)
        calldata = ROUNDS_SELECTOR + encoded_id
        return (
            Web3.to_checksum_address(rounds_address),
            True,   # allowFailure — don't revert the whole batch if one fails
            calldata,
        )

    @staticmethod
    def _encode_balance_call(treasury_address: str, user_address: str) -> tuple:
        """Build a (target, allowFailure, callData) tuple for balanceOf(user)."""
        calldata = BALANCE_OF_SELECTOR + encode(["address"], [user_address])
        return (
            Web3.to_checksum_address(treasury_address),
            True,
            calldata,
        )

    @staticmethod
    def _encode_core_params_call(collections_address: str, collection_id: int) -> tuple:
        """Build a (target, allowFailure, callData) tuple for getCoreParams(collectionId)."""
        calldata = GET_CORE_PARAMS_SELECTOR + encode(["uint256"], [collection_id])
        return (
            Web3.to_checksum_address(collections_address),
            True,
            calldata,
        )

    # ── High-Level Builders ─────────────────────────────────────────────────

    def build_rounds_calls(
        self,
        rounds_address: str,
        claim_round_pairs: list[tuple[int, int]],
    ) -> list[tuple]:
        """
        Build Multicall3 call tuples for currentRoundId and then rounds(roundId) lookups.

        Parameters
        ----------
        rounds_address : str
            Address of the Rounds contract.
        claim_round_pairs : list of (claim_id, round_index)
            Each pair is encoded as ``(claimId << 128) | roundIndex``.
        """
        from .guards import encode_round_id

        calls = []
        for claim_id, round_index in claim_round_pairs:
            round_id = encode_round_id(claim_id, round_index)
            calls.append(self._encode_rounds_call(rounds_address, round_id))
        return calls

    def build_preflight_calls(
        self,
        rounds_address: str,
        treasury_address: str,
        collections_address: str,
        user_address: str,
        claim_round_pairs: list[tuple[int, int]],
        collection_id: int = 1,
    ) -> list[tuple]:
        """
        Build a combined batch for the canBet pre-flight:
          1. Treasury.balanceOf(user)        — index 0
          2. Collections.getCoreParams(cid)  — index 1
          3..N. Rounds.rounds(roundId)       — one per claim

        Returns list of call tuples in that order.
        """
        calls = []
        # Treasury balance
        calls.append(self._encode_balance_call(treasury_address, user_address))
        # getCoreParams
        calls.append(self._encode_core_params_call(collections_address, collection_id))
        # Round data for each claim
        calls.extend(self.build_rounds_calls(rounds_address, claim_round_pairs))
        return calls

    # ── Execution ───────────────────────────────────────────────────────────

    def execute(self, calls: list[tuple]) -> list[tuple[bool, bytes]]:
        """
        Send all calls via Multicall3.aggregate3 in a single ``eth_call``.

        Returns a list of ``(success: bool, returnData: bytes)`` in the same
        order as the input ``calls``.
        """
        if not calls:
            return []

        try:
            results = self.contract.functions.aggregate3(calls).call()
            logger.debug(f"Multicall3: {len(calls)} calls → {sum(r[0] for r in results)} succeeded")
            return [(r[0], bytes(r[1])) for r in results]
        except Exception as exc:
            logger.error(f"Multicall3 aggregate3 failed: {exc}")
            return [(False, b"")] * len(calls)

    # ── Decoders ────────────────────────────────────────────────────────────

    @staticmethod
    def decode_round_data(return_data: bytes) -> dict | None:
        """
        Decode bytes from rounds(uint256) → dict.

        Returns::
            {
                "bettingAmount": int,
                "bettingDeadline": int,
                "poolAmount": int,
                "result": int,
                "isClosed": bool,
            }
        """
        try:
            values = decode(
                ["uint256", "uint256", "uint256", "uint8", "bool"],
                return_data,
            )
            return {
                "bettingAmount": values[0],
                "bettingDeadline": values[1],
                "poolAmount": values[2],
                "result": values[3],
                "isClosed": values[4],
            }
        except Exception:
            return None

    @staticmethod
    def decode_balance(return_data: bytes) -> int:
        """Decode balanceOf return → raw uint256."""
        try:
            (val,) = decode(["uint256"], return_data)
            return val
        except Exception:
            return 0

    @staticmethod
    def decode_core_params(return_data: bytes) -> dict | None:
        """
        Decode getCoreParams return → dict.

        Returns::
            {
                "isPermissioned": bool,
                "status": int,
                "claimPrice": int,
                "minBet": int,
            }
        """
        try:
            values = decode(
                ["bool", "uint8", "uint256", "uint256"],
                return_data,
            )
            return {
                "isPermissioned": values[0],
                "status": values[1],
                "claimPrice": values[2],
                "minBet": values[3],
            }
        except Exception:
            return None

    # ── Convenience: Batch Read Round States ────────────────────────────────

    def read_rounds_batch(
        self,
        rounds_address: str,
        claim_round_pairs: list[tuple[int, int]],
    ) -> list[dict | None]:
        """
        Fetch round data for multiple claims in one RPC call.

        Returns a list (same order as input) of decoded round dicts,
        or None for failed reads.
        """
        calls = self.build_rounds_calls(rounds_address, claim_round_pairs)
        results = self.execute(calls)

        decoded = []
        for success, data in results:
            if success and len(data) >= 160:  # 5 * 32 bytes
                decoded.append(self.decode_round_data(data))
            else:
                decoded.append(None)
        return decoded

    # ── Convenience: Batch Pre-flight for N Claims ──────────────────────────

    def preflight_batch(
        self,
        rounds_address: str,
        treasury_address: str,
        collections_address: str,
        user_address: str,
        claim_round_pairs: list[tuple[int, int]],
        collection_id: int = 1,
    ) -> tuple[int, dict | None, list[dict | None]]:
        """
        Single RPC call that returns:
            (treasury_balance_raw, core_params_dict, [round_data_dict, ...])

        Perfect for the canBet pre-flight: read treasury + minBet + all round
        states in one shot.
        """
        calls = self.build_preflight_calls(
            rounds_address, treasury_address, collections_address,
            user_address, claim_round_pairs, collection_id,
        )
        results = self.execute(calls)

        if len(results) < 2:
            return 0, None, []

        # Index 0 → treasury balance
        treasury_raw = 0
        if results[0][0]:
            treasury_raw = self.decode_balance(results[0][1])

        # Index 1 → core params
        core_params = None
        if results[1][0]:
            core_params = self.decode_core_params(results[1][1])

        # Index 2+ → round data
        round_results = []
        for success, data in results[2:]:
            if success and len(data) >= 160:
                round_results.append(self.decode_round_data(data))
            else:
                round_results.append(None)

        return treasury_raw, core_params, round_results
