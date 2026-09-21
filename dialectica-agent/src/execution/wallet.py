from web3 import Web3
from web3.exceptions import ContractCustomError
from datetime import datetime
from loguru import logger
from ..utils.config import settings
from ..utils.models import Decision
from ..execution.encryption import encrypt_vote
from ..execution.errors import (
    decode_exception,
    ContractRevertError,
    NoBettingError,
    RoundNotActiveError,
    InsufficientBalanceError,
    InvalidBetAmountError,
)
from ..execution.guards import encode_round_id

class Wallet:
    def __init__(self):
        # Base RPC URL — sourced from .env (BASE_RPC_URL / RPC_URL).
        # Public fallback removed: private Alchemy endpoint prevents 503s.
        rpc_url = settings.BASE_RPC_URL
        if not rpc_url:
            raise RuntimeError(
                "BASE_RPC_URL is not set. Add your Alchemy/Infura URL to .env. "
                "Public https://mainnet.base.org is disabled due to 503 rate limits."
            )
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        
        self.account = None
        if settings.PRIVATE_KEY and not settings.MOCK_MODE:
            try:
                self.account = self.w3.eth.account.from_key(settings.PRIVATE_KEY)
            except Exception as e:
                logger.warning(f"Could not load account from private key: {e}")

        # ── Circuit breaker: paused on InsufficientBalance, resumed on Deposit event ──
        self.betting_paused: bool = False

        # ── Cached min bet from collections.getCoreParams (USDC raw, 6 decimals) ──
        self._cached_min_bet: int | None = None

    @property
    def usdc_address(self):
        # Return checksummed address for safety
        addr = settings.USDC_ADDRESS
        if addr and self.w3.is_address(addr):
            return self.w3.to_checksum_address(addr)
        return addr

    @property
    def treasury_address(self):
        return settings.TREASURY_ADDRESS

    @property
    def claims_address(self):
        return settings.CLAIMS_ADDRESS

    @property
    def collections_address(self):
        return settings.COLLECTIONS_ADDRESS

    @property
    def rounds_address(self):
        return settings.ROUNDS_ADDRESS

    @property
    def usdc(self):
        # Minimal ABI for ERC20
        # Included allowance(address owner, address spender)
        abi = [
            {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
            {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
            {"constant": True, "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}
        ]
        return self.w3.eth.contract(address=self.usdc_address, abi=abi)

    @property
    def claims(self):
        # ABI for Claims Contract
        abi = [
            {
                "inputs": [
                    {"internalType": "uint256", "name": "claimId", "type": "uint256"},
                    {"internalType": "uint256", "name": "amount", "type": "uint256"},
                    {"internalType": "bytes", "name": "encryptedVote", "type": "bytes"},
                    {"internalType": "string", "name": "evidence", "type": "string"}
                ],
                "name": "placeBet", 
                "outputs": [], 
                "stateMutability": "nonpayable", 
                "type": "function"
            },
            {
                "inputs": [
                    {"internalType": "uint256", "name": "claimId", "type": "uint256"}
                ],
                "name": "distributePayouts",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "inputs": [
                    {"internalType": "uint256", "name": "claimId", "type": "uint256"},
                    {"internalType": "address[]", "name": "recipients", "type": "address[]"}
                ],
                "name": "refund",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "inputs": [
                    {"internalType": "uint256", "name": "claimId", "type": "uint256"}
                ],
                "name": "challenge",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "anonymous": False,
                "inputs": [
                    {"indexed": True, "internalType": "uint256", "name": "claimId", "type": "uint256"},
                    {"indexed": False, "internalType": "uint8", "name": "outcome", "type": "uint8"}
                ],
                "name": "ClaimSettled",
                "type": "event"
            },
            {"inputs": [], "name": "NoBetting", "type": "error"},
            {"inputs": [], "name": "RoundNotActive", "type": "error"},
            {"inputs": [], "name": "InvalidClaimStatus", "type": "error"},
            {"inputs": [], "name": "InsufficientBalance", "type": "error"},
            {"inputs": [{"internalType": "uint256", "name": "minBet", "type": "uint256"}, {"internalType": "uint256", "name": "maxBet", "type": "uint256"}], "name": "InvalidBetAmount", "type": "error"},
            {"inputs": [], "name": "EvidenceTooLong", "type": "error"},
            {"inputs": [], "name": "IncorrectEncryptionLength", "type": "error"},
            {"inputs": [], "name": "OngoingBetting", "type": "error"},
            {"inputs": [], "name": "OngoingChallenge", "type": "error"},
            {"inputs": [], "name": "NoChallenge", "type": "error"}
        ]
        return self.w3.eth.contract(address=self.claims_address, abi=abi)

    @property
    def rounds(self):
        # ABI for Rounds Contract
        abi = [
            {"inputs": [{"internalType": "uint256", "name": "claimId", "type": "uint256"}, {"internalType": "uint8", "name": "stance", "type": "uint8"}], "name": "placeVote", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
            {"inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "name": "rounds", "outputs": [{"internalType": "uint256", "name": "bettingAmount", "type": "uint256"}, {"internalType": "uint256", "name": "bettingDeadline", "type": "uint256"}, {"internalType": "uint256", "name": "poolAmount", "type": "uint256"}, {"internalType": "uint8", "name": "result", "type": "uint8"}, {"internalType": "bool", "name": "isClosed", "type": "bool"}], "stateMutability": "view", "type": "function"},
            {"anonymous": False, "inputs": [{"indexed": True, "internalType": "uint256", "name": "claimId", "type": "uint256"}, {"indexed": False, "internalType": "uint8", "name": "result", "type": "uint8"}], "name": "RoundSettled", "type": "event"},
            {"anonymous": False, "inputs": [{"indexed": True, "internalType": "uint256", "name": "claimId", "type": "uint256"}, {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"}, {"indexed": True, "internalType": "address", "name": "player", "type": "address"}], "name": "BetPlaced", "type": "event"},
            {"inputs": [], "name": "NoBetting", "type": "error"},
            {"inputs": [], "name": "RoundNotActive", "type": "error"},
            {"inputs": [], "name": "InvalidClaimStatus", "type": "error"},
            {"inputs": [], "name": "OngoingBetting", "type": "error"},
            {"inputs": [], "name": "OngoingChallenge", "type": "error"},
            {"inputs": [], "name": "NoChallenge", "type": "error"}
        ]
        return self.w3.eth.contract(address=self.rounds_address, abi=abi)

    @property
    def collections(self):
        """ABI for Collections contract — getCoreParams(collectionId) and new timeout variables."""
        abi = [
            {
                "inputs": [
                    {"internalType": "uint256", "name": "collectionId", "type": "uint256"}
                ],
                "name": "getCoreParams",
                "outputs": [
                    {"internalType": "bool", "name": "isPermissioned", "type": "bool"},
                    {"internalType": "uint8", "name": "status", "type": "uint8"},
                    {"internalType": "uint256", "name": "claimPrice", "type": "uint256"},
                    {"internalType": "uint256", "name": "minBet", "type": "uint256"}
                ],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [
                    {"internalType": "uint256", "name": "collectionId", "type": "uint256"},
                    {"internalType": "uint256", "name": "index", "type": "uint256"}
                ],
                "name": "getRoundDuration",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [
                    {"internalType": "uint256", "name": "collectionId", "type": "uint256"},
                    {"internalType": "uint256", "name": "index", "type": "uint256"}
                ],
                "name": "getChallengeDuration",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [{"internalType": "uint256", "name": "collectionId", "type": "uint256"}],
                "name": "getDisputeDuration",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [
                    {"internalType": "uint256", "name": "collectionId", "type": "uint256"},
                    {"internalType": "uint256", "name": "index", "type": "uint256"}
                ],
                "name": "getBountyAmount",
                "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]
        return self.w3.eth.contract(address=self.collections_address, abi=abi)

    def fetch_min_bet(self, collection_id: int | None = None) -> int:
        """
        Query collections.getCoreParams(collectionId) to get the live minBet.
        Returns raw USDC amount (6 decimals).  Falls back to settings.BET_AMOUNT_USDC.
        """
        if collection_id is None:
            collection_id = 1  # default collection

        try:
            result = self.collections.functions.getCoreParams(collection_id).call()
            # result: [bool isPermissioned, uint8 status, uint256 claimPrice, uint256 minBet]
            status = result[1]
            min_bet = result[3]
            logger.info(
                f"getCoreParams(collection={collection_id}): "
                f"status={status}, minBet={min_bet / 10**6} USDC"
            )
            self._cached_min_bet = min_bet
            return min_bet
        except Exception as exc:
            logger.warning(f"getCoreParams failed (collection={collection_id}): {exc}")
            return self._cached_min_bet or int(settings.BET_AMOUNT_USDC * 10**6)

    def fetch_timeline_params(self, collection_id: int = 1, round_index: int = 0) -> dict:
        """
        Query the individual collection getters to determine duration of phases.
        """
        try:
            round_duration = self.collections.functions.getRoundDuration(collection_id, round_index).call()
            challenge_duration = self.collections.functions.getChallengeDuration(collection_id, round_index).call()
            dispute_duration = self.collections.functions.getDisputeDuration(collection_id).call()
            bounty_amount = self.collections.functions.getBountyAmount(collection_id, round_index).call()
            
            return {
                "round_duration": round_duration,
                "challenge_duration": challenge_duration,
                "dispute_duration": dispute_duration,
                "bounty_amount": bounty_amount
            }
        except Exception as exc:
            logger.error(f"Failed to fetch timeline params for collection {collection_id} index {round_index}: {exc}")
            return {
                "round_duration": 1800,
                "challenge_duration": 600,
                "dispute_duration": 129600,
                "bounty_amount": 0
            }

    def get_round_data(self, claim_id: int, round_index: int = 0) -> dict | None:
        """
        Read on-chain round state using the proper bit-shifted roundId.

        Uses raw eth_call with manual uint256 encoding to avoid
        UnsignedIntegerEncoder overflow on large bit-shifted roundIds.

        Returns dict with keys:
            bettingAmount, bettingDeadline, poolAmount, result, isClosed
        or None on failure.
        """
        round_id = encode_round_id(claim_id, round_index)
        try:
            from .guards import _raw_call_rounds
            raw = _raw_call_rounds(self.w3, self.rounds_address, round_id)
            data = {
                "bettingAmount": raw[0],
                "bettingDeadline": raw[1],
                "poolAmount": raw[2],
                "result": raw[3],
                "isClosed": raw[4],
            }
            logger.debug(
                f"Round data (claim={claim_id}, round={round_index}, "
                f"roundId={round_id}): deadline={data['bettingDeadline']}, "
                f"closed={data['isClosed']}"
            )
            return data
        except Exception as exc:
            logger.warning(
                f"get_round_data failed (claim={claim_id}, round={round_index}, "
                f"roundId={round_id}): {exc}"
            )
            return None

    async def get_balances(self):
        """Fetches ETH and USDC balances."""
        if not self.account:
            return {"eth": 0, "usdc": 0}

        eth_balance = 0
        usdc_balance = 0

        # Silent fallback logic
        try:
            eth_balance = self.w3.eth.get_balance(self.account.address)
        except Exception:
            pass # Keep logs clean

        try:
            # Dividing by 10**6 for USDC (6 decimals)
            usdc_balance = self.usdc.functions.balanceOf(self.account.address).call() / 10**6
        except Exception:
             pass # Keep logs clean
            
        return {
            "eth": float(self.w3.from_wei(eth_balance, 'ether')),
            "usdc": float(usdc_balance)
        }

    async def ensure_treasury_balance(self, required_amount: int) -> bool:
        """Checks if Treasury has enough funds; if not, deposits from Wallet."""
        if settings.MOCK_MODE:
            return True

        if not self.account:
            return False

        try:
            # 0. Check Existing Treasury Contract Balance
            # We must check if we already have funds deposited before adding more.
            treasury_abi = [
                {"inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "deposit", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
                {"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}
            ]
            treasury_contract = self.w3.eth.contract(address=self.treasury_address, abi=treasury_abi)
            
            try:
                current_treasury_balance = treasury_contract.functions.balanceOf(self.account.address).call()
                logger.info(f"Current Treasury Balance: {current_treasury_balance / 10**6} USDC")
                
                if current_treasury_balance >= required_amount:
                    logger.info("Treasury is sufficiently funded. Skipping deposit.")
                    return True
            except Exception as e:
                logger.warning(f"Could not fetch Treasury balance (assuming 0): {e}")

            # 1. Check Wallet USDC Balance
            try:
                wallet_usdc = self.usdc.functions.balanceOf(self.account.address).call()
            except Exception as e:
                logger.warning(f"Failed to check wallet USDC balance: {e}")
                return False

            # required_amount is in Wei (6 decimals)
            if wallet_usdc < required_amount:
                logger.warning(f"Insufficient Wallet USDC for Treasury Deposit. Have: {wallet_usdc/10**6}, Need: {required_amount/10**6}")
                return False

            # 2. Check Allowance for Treasury
            try:
                allowance = self.usdc.functions.allowance(self.account.address, self.treasury_address).call()
                if allowance < required_amount:
                   logger.info("Approving USDC for Treasury...")
                   # Inline Approval
                   latest_block = self.w3.eth.get_block('latest')
                   base_fee = latest_block['baseFeePerGas']
                   max_priority_fee = self.w3.eth.max_priority_fee
                   max_fee = int(base_fee + max_priority_fee)
                   
                   func_call = self.usdc.functions.approve(self.treasury_address, required_amount)
                   tx = func_call.build_transaction({
                        'from': self.account.address,
                       'gas': self._estimate_gas_with_buffer(func_call),
                        'nonce': self.w3.eth.get_transaction_count(self.account.address, 'pending'),
                        'maxFeePerGas': max_fee,
                        'maxPriorityFeePerGas': max_priority_fee,
                        'type': 2,
                        'chainId': self.w3.eth.chain_id
                   })
                   signed = self.account.sign_transaction(tx)
                   tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                   
                   # Wait for receipt AND confirmation delay
                   self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                   import asyncio
                   await asyncio.sleep(2) # RPC node sync buffer
                   logger.info("USDC Approved for Treasury (Receipt Confirmed).")
            except Exception as e:
                logger.warning(f"Failed during Treasury approval: {e}")
                return False 

            # 3. Deposit to Treasury
            # Minimal Treasury ABI for deposit
            treasury_abi = [{"inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}], "name": "deposit", "outputs": [], "stateMutability": "nonpayable", "type": "function"}]
            treasury_contract = self.w3.eth.contract(address=self.treasury_address, abi=treasury_abi)
            
            # Estimate Gas First
            try:
                estimate = treasury_contract.functions.deposit(required_amount).estimate_gas({'from': self.account.address})
                logger.info(f"Estimated Gas for Deposit: {estimate}")
            except Exception as est_err:
                logger.warning(f"Gas estimation failed: {est_err}. Proceeding anyway.")
            
            # Gas Strategy for Deposit (Standard)
            latest_block = self.w3.eth.get_block('latest')
            base_fee = latest_block['baseFeePerGas']
            priority_fee = self.w3.eth.max_priority_fee
            max_fee = int(base_fee * 1.2 + priority_fee)

            func_call = treasury_contract.functions.deposit(required_amount)
            tx = func_call.build_transaction({
                'from': self.account.address,
                'gas': self._estimate_gas_with_buffer(func_call),
                'nonce': self.w3.eth.get_transaction_count(self.account.address, 'pending'),
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'type': 2,
                'chainId': self.w3.eth.chain_id
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            logger.info(f"Treasury Deposit Sent: {tx_hash.hex()}")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                logger.info("Treasury Deposit Successful.")
                return True
            else:
                logger.error("Treasury Deposit Failed.")
                return False

        except Exception as e:
            logger.error(f"Treasury Deposit Logic Failed: {e}")
            return False

    async def check_allowance(self, amount: int) -> bool:
        """Checks if Main Claims contract has enough allowance to spend USDC."""
        import asyncio
        retries = 3
        spender_address = self.claims_address # Updated spender
        
        for i in range(retries):
            try:
                # Ensure RPC connection is active
                if not self.w3.is_connected():
                    logger.error("Web3 Provider not connected during allowance check.")
                    return False

                # Brief delay to be nice to RPC
                await asyncio.sleep(1)
                
                allowance = self.usdc.functions.allowance(self.account.address, spender_address).call()
                logger.info(f"Current USDC Allowance for Claims: {allowance} (Needed: {amount})")
                return allowance >= amount
            except Exception as e:
                logger.warning(f"Error checking allowance (Attempt {i+1}/{retries}): {e}")
                await asyncio.sleep(2)
        
        return False

    async def approve_usdc(self, amount: int) -> bool:
        """Approves USDC spending for the Claims contract."""
        spender_address = self.claims_address
        logger.info(f"Approving {amount} USDC for Claims contract...")
        try:
            # Gas strategy for approval
            latest_block = self.w3.eth.get_block('latest')
            base_fee = latest_block['baseFeePerGas']
            
            # Web3.py v6+ syntax for priority fee
            max_priority_fee = self.w3.eth.max_priority_fee
            
            # Ensure fees are integers
            max_fee = int(base_fee + max_priority_fee)
            
            func_call = self.usdc.functions.approve(spender_address, amount)
            tx = func_call.build_transaction({
                'from': self.account.address,
                'gas': self._estimate_gas_with_buffer(func_call),
                'nonce': self.w3.eth.get_transaction_count(self.account.address, 'pending'),
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': max_priority_fee,
                'type': 2, # EIP-1559
                'chainId': self.w3.eth.chain_id
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            logger.info(f"Approval TX sent: {tx_hash.hex()}")
            
            # Wait for receipt
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                logger.info("USDC Approval Successful.")
                return True
            else:
                logger.error("USDC Approval Failed.")
                return False
                
        except Exception as e:
            logger.error(f"Approval failed: {e}")
            return False

    async def broadcast_vote(self, decision: Decision, collection_id: int | None = None, session=None) -> tuple[str, int]:
        """
        Build, sign, and send a placeBet transaction.

        Handles Dialectica-specific contract reverts:
          • NoBetting / RoundNotActive  → mark claim EXPIRED, no retry
          • InsufficientBalance         → CRITICAL, pause all betting
          • InvalidBetAmount            → recalibrate from error params / getCoreParams
        
        Returns tx_hash on success, or None on failure.
        The ``_audit_failure`` helper persists every failure to the ``failed_txs`` table.
        """
        logger.info(f"Preparing to broadcast vote for decision: {decision.stance}")

        # ── Circuit Breaker ──
        if self.betting_paused:
            logger.critical(
                "BETTING PAUSED (InsufficientBalance). "
                "Waiting for Treasury Deposit event before resuming."
            )
            return (None, 0)
        
        # 1. Prepare Stance
        stance_str = decision.stance
        if stance_str not in ["TRUE", "FALSE"]:
             if str(stance_str).lower() == "true": stance_str = "TRUE"
             elif str(stance_str).lower() == "false": stance_str = "FALSE"
             else:
                logger.warning(f"Skipping vote for invalid decision stance: {decision.stance}")
                return (None, 0)
        
        # 2. Mock Mode Bypass
        if settings.MOCK_MODE:
            logger.info(f"MOCK MODE: Voting {decision.stance} on Claim {decision.claim_id}")
            import asyncio
            await asyncio.sleep(0.5)
            return ("0xmocktransactionhash404040", 10000000)
        
        if not self.account:
            logger.error("Cannot broadcast transaction: No active account/private key.")
            return (None, 0)

        try:
            logger.info(f"DEBUG: decision.claim_id={decision.claim_id}, decision.on_chain_id={decision.on_chain_id}")
            if not decision.on_chain_id:
                logger.error(f"Missing on_chain_id in decision for claim {decision.claim_id}")
                return (None, 0)
            target_id = decision.on_chain_id
            claim_id_int = int(target_id)
        except ValueError:
            logger.error("Invalid claim ID format.")
            return (None, 0)

        # 3. Market Odds & Pool Size Analysis (Gap 10)
        min_bet_raw = self.fetch_min_bet(collection_id)
        base_amount_wei = max(int(settings.BET_AMOUNT_USDC * 10**6), min_bet_raw)
        
        from .guards import encode_round_id, _raw_call_rounds
        round_id = encode_round_id(claim_id_int, 0)
        pool_amount = 0
        try:
            round_info = _raw_call_rounds(self.w3, self.rounds_address, round_id)
            pool_amount = round_info[2]
        except Exception as e:
            logger.warning(f"Could not fetch pool size for Gap 10 logic: {e}")

        multiplier = 1.0
        if decision.confidence >= 0.95 and pool_amount > 100 * 10**6:
            multiplier = 1.5
            logger.info(f"High confidence ({decision.confidence}) and healthy pool ({pool_amount/1e6:.2f} USDC) detected. Scaling bet x1.5")
        
        amount_wei = int(base_amount_wei * multiplier)
        logger.info(f"Final Bet amount: {amount_wei / 10**6:.2f} USDC (minBet={min_bet_raw / 10**6:.2f}, pool={pool_amount / 10**6:.2f})")
        
        # 4. Check Allowance for Claims Contract
        if not await self.check_allowance(amount_wei):
            if not await self.approve_usdc(amount_wei):
                logger.error("Aborting vote due to failed approval.")
                return (None, 0)
        
        # 5. Build Vote Transaction
        try:

            # 5b. Encrypt Vote
            vote_integer = 1 if stance_str == "TRUE" else 2
            assert vote_integer in (1, 2), f"Binary encoding violation: vote_integer={vote_integer}"
            logger.info(f"Encrypting encoded vote: {vote_integer} (derived from {stance_str})")
            
            encrypted_vote = encrypt_vote(vote_integer)
            
            # 5c. Prepare Evidence (Reasoning)
            evidence = decision.reasoning or "No reasoning provided."
            if len(evidence) > 2000:
                evidence = evidence[:1997] + "..."

            # Gas Strategy
            latest_block = self.w3.eth.get_block('latest')
            base_fee = latest_block['baseFeePerGas']
            
            priority_fee_wei = int(0.01 * 10**9) # 0.01 Gwei
            multiplier = 1.5 
            
            try:
                 # Use bit-shifted roundId for proper on-chain lookup.
                 # Manual encoding avoids UnsignedIntegerEncoder overflow.
                 round_id = encode_round_id(claim_id_int, 0)
                 from .guards import _raw_call_rounds
                 round_info = _raw_call_rounds(self.w3, self.rounds_address, round_id)
                 if round_info and round_info[1] > 0:
                     betting_deadline = round_info[1]
                     time_remaining = betting_deadline - datetime.utcnow().timestamp()
                     
                     if time_remaining < 300 and time_remaining > 0:
                         logger.warning(f"URGENT: Deadline in {time_remaining:.0f}s. Using AGGRESSIVE gas.")
                         multiplier = 2.0
                         priority_fee_wei = int(0.1 * 10**9)
            except Exception:
                 pass

            max_fee = int(base_fee * multiplier) + priority_fee_wei

            # 6. Call Claims.placeBet
            logger.info(f"Submitting placeBet to Claims Contract: ID={claim_id_int}, Amt={amount_wei}")
            
            func_call = self.claims.functions.placeBet(
                claim_id_int,
                amount_wei,
                encrypted_vote,
                evidence
            )
            tx = func_call.build_transaction({
                'from': self.account.address,
                'gas': self._estimate_gas_with_buffer(func_call),
                'nonce': self.w3.eth.get_transaction_count(self.account.address, 'pending'),
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee_wei,
                'type': 2,
                'chainId': self.w3.eth.chain_id
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_hex = tx_hash.hex()
            
            logger.info(f"Bet TX Sent: {tx_hex}")
            
            # Wait for receipt
            try:
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
                
                if receipt.status == 1:
                    gas_used = receipt['gasUsed']
                    effective_gas_price = receipt['effectiveGasPrice']
                    cost_eth = Web3.from_wei(gas_used * effective_gas_price, 'ether')
                    logger.info(f"Bet Confirmed! TX: {tx_hex} | Gas Used: {gas_used} | Cost: {cost_eth} ETH")
                    return (tx_hex, amount_wei)
                else:
                    # ── Reverted TX: try to decode the revert reason ──
                    logger.error(f"Bet Transaction Reverted. TX: {tx_hex}")
                    self._handle_revert(
                        claim_id=str(decision.claim_id),
                        tx_hex=tx_hex,
                        receipt=receipt,
                        collection_id=collection_id,
                    )
                    return (None, 0) 
                    
            except Exception as wait_err:
                # TX was sent but receipt failed — try error decode on the exception
                decoded = decode_exception(wait_err)
                if decoded:
                    self._handle_decoded_error(decoded, str(decision.claim_id), collection_id, session=session)
                else:
                    logger.error(
                        f"Transaction sent but receipt wait failed: {wait_err}. TX: {tx_hex}"
                    )
                    self._audit_failure(
                        claim_id=str(decision.claim_id),
                        action_type="VOTE",
                        error_name="RECEIPT_TIMEOUT",
                        error_detail=str(wait_err),
                    )
                return (None, 0)
            
        except ContractCustomError as e:
            # ── Catch custom contract errors native to web3 ──
            error_msg = str(e)
            
            # 1. State Errors -> Abandon Claim
            if any(err in error_msg for err in ["NoBetting", "RoundNotActive", "InvalidClaimStatus", "OngoingBetting", "OngoingChallenge", "NoChallenge"]):
                logger.warning(f"Market closed or invalid state for Claim {claim_id_int}. Marking as EXPIRED.")
                self._mark_claim_expired(str(decision.claim_id))
                return (None, 0)
            
            # 2. Financial Errors -> Circuit Breaker
            elif "InsufficientBalance" in error_msg:
                logger.critical(f"Insufficient Treasury Balance. Pausing bot.")
                self.betting_paused = True
                return (None, 0)
            
            # 3. Parameter Errors -> Dynamic Adjust
            elif "InvalidBetAmount" in error_msg:
                logger.error(f"Bet rejected: InvalidBetAmount. Forcing getCoreParams refresh.")
                self.fetch_min_bet(collection_id)
                return (None, 0)
            
            # 4. Payload Errors -> Abort
            elif any(err in error_msg for err in ["EvidenceTooLong", "IncorrectEncryptionLength"]):
                logger.error(f"Payload formatting error on Claim {claim_id_int}. Aborting.")
                return (None, 0)
            
            else:
                logger.error(f"Unhandled Contract Error: {error_msg}")
                self._audit_failure(
                    claim_id=str(decision.claim_id),
                    action_type="VOTE",
                    error_name="ContractCustomError",
                    error_detail=error_msg,
                )
                return (None, 0)
                
        except Exception as e:
            # ── Catch other build/send errors (e.g. gas estimation reverts) ──
            decoded = decode_exception(e)
            if decoded:
                self._handle_decoded_error(decoded, str(decision.claim_id), collection_id, session=session)
            else:
                logger.error(f"Vote Transaction Error: {e}")
                self._audit_failure(
                    claim_id=str(decision.claim_id),
                    action_type="VOTE",
                    error_name="TX_BUILD_ERROR",
                    error_detail=str(e),
                )
            return (None, 0)

    # ── Error Recovery Helpers ──────────────────────────────────────────────

    def _handle_revert(
        self,
        claim_id: str,
        tx_hex: str,
        receipt,
        collection_id: int | None = None,
        session=None
    ):
        """
        After a reverted receipt, replay the call to extract revert data,
        decode it, and route to the appropriate handler.
        """
        try:
            # Attempt eth_call replay to get revert data
            tx_data = self.w3.eth.get_transaction(tx_hex)
            self.w3.eth.call(
                {
                    "to": tx_data["to"],
                    "from": tx_data["from"],
                    "data": tx_data["input"],
                    "value": tx_data.get("value", 0),
                },
                receipt["blockNumber"],
            )
        except Exception as replay_err:
            decoded = decode_exception(replay_err)
            if decoded:
                self._handle_decoded_error(decoded, claim_id, collection_id, session=session)
                return

        # If replay didn't throw, log as unknown revert
        logger.error(f"Reverted TX {tx_hex} for claim {claim_id}: unknown reason")
        self._audit_failure(
            claim_id=claim_id,
            action_type="VOTE",
            error_name="UNKNOWN_REVERT",
            error_detail=f"TX: {tx_hex}",
        )

    def _handle_decoded_error(
        self,
        error: ContractRevertError,
        claim_id: str,
        collection_id: int | None = None,
        session=None
    ):
        """Route a decoded ContractRevertError to the correct recovery path."""
        error_name = error.error_name
        raw_hex = error.raw_data.hex() if error.raw_data else None

        if isinstance(error, (NoBettingError, RoundNotActiveError)):
            # ── NoBetting / RoundNotActive → mark EXPIRED, no retry ──
            logger.warning(
                f"{error_name} for claim {claim_id}. "
                "Marking as EXPIRED — do not retry."
            )
            self._mark_claim_expired(claim_id, session=session)
            self._audit_failure(
                claim_id=claim_id,
                action_type="VOTE",
                error_name=error_name,
                raw_hex=raw_hex,
            )
            
        elif error_name in ["InvalidClaimStatus", "OngoingBetting", "OngoingChallenge", "NoChallenge"]:
            # ── Phase 2 State Errors → mark EXPIRED, no retry ──
            logger.warning(
                f"{error_name} on claim {claim_id}. "
                "Invalid state or window closed. Marking as EXPIRED."
            )
            self._mark_claim_expired(claim_id, session=session)
            self._audit_failure(
                claim_id=claim_id,
                action_type="PHASE_2",
                error_name=error_name,
                raw_hex=raw_hex,
            )

        elif isinstance(error, InsufficientBalanceError):
            # ── InsufficientBalance → CRITICAL, pause all betting ──
            logger.critical(
                f"INSUFFICIENT TREASURY BALANCE for claim {claim_id}. "
                "ALL BETTING PAUSED until a Treasury Deposit event is detected."
            )
            self.betting_paused = True
            self._audit_failure(
                claim_id=claim_id,
                action_type="VOTE",
                error_name="InsufficientBalance",
                raw_hex=raw_hex,
            )

        elif isinstance(error, InvalidBetAmountError):
            # ── InvalidBetAmount → recalibrate from error params ──
            min_bet = error.min_bet
            max_bet = error.max_bet
            if min_bet > 0:
                logger.warning(
                    f"InvalidBetAmount for claim {claim_id}: "
                    f"contract requires minBet={min_bet / 10**6} USDC, "
                    f"maxBet={max_bet / 10**6} USDC. Recalibrating."
                )
                self._cached_min_bet = min_bet
            else:
                # Error didn't carry params — re-query getCoreParams
                logger.warning(
                    f"InvalidBetAmount for claim {claim_id} (no params). "
                    f"Refreshing from getCoreParams({collection_id or 1})."
                )
                self.fetch_min_bet(collection_id)

            self._audit_failure(
                claim_id=claim_id,
                action_type="VOTE",
                error_name="InvalidBetAmount",
                error_detail=f"minBet={min_bet}, maxBet={max_bet}",
                raw_hex=raw_hex,
            )

        else:
            # ── Unknown contract error ──
            logger.error(f"Unhandled contract error for claim {claim_id}: {error}")
            self._audit_failure(
                claim_id=claim_id,
                action_type="VOTE",
                error_name=error_name,
                error_detail=str(error),
                raw_hex=raw_hex,
            )

    def _mark_claim_expired(self, claim_id: str, session=None):
        """Update local DB status to EXPIRED for the current round."""
        from ..utils.database import init_db, ClaimModel
        try:
            is_local_session = False
            if session is None:
                SessionLocal = init_db(settings.DATABASE_URL)
                session = SessionLocal()
                is_local_session = True
                
            claim = session.query(ClaimModel).filter(
                (ClaimModel.id == claim_id) | (ClaimModel.on_chain_id == claim_id)
            ).first()
            if claim:
                claim.current_status = "EXPIRED"
                session.commit()
                logger.info(f"Claim {claim.id} marked as EXPIRED in DB.")
                
            if is_local_session:
                session.close()
        except Exception as exc:
            logger.error(f"Failed to mark claim {claim_id} as EXPIRED: {exc}")

    
    def _estimate_gas_with_buffer(self, contract_func, fallback=500000, buffer=1.3):
        try:
            est = contract_func.estimate_gas({'from': self.account.address})
            return int(est * buffer)
        except Exception as e:
            from loguru import logger
            logger.warning(f"Gas estimation failed: {e}. Using fallback.")
            return fallback

    def _audit_failure(
        self,
        claim_id: str,
        action_type: str,
        error_name: str,
        error_detail: str | None = None,
        raw_hex: str | None = None,
        session=None
    ):
        """Persist a failed transaction record for audit."""
        from ..utils.database import init_db, FailedTxModel
        try:
            is_local_session = False
            if session is None:
                SessionLocal = init_db(settings.DATABASE_URL)
                session = SessionLocal()
                is_local_session = True
                
            record = FailedTxModel(
                claim_id=claim_id,
                action_type=action_type,
                error_name=error_name,
                error_detail=error_detail,
                raw_revert_hex=raw_hex,
                timestamp=datetime.utcnow(),
            )
            session.add(record)
            session.commit()
            
            if is_local_session:
                session.close()
            logger.debug(f"Audit: {error_name} recorded for claim {claim_id}")
        except Exception as exc:
            logger.error(f"Failed to persist audit record: {exc}")

    async def challenge_claim(self, claim_id: str, evidence: str = "Automated dispute by Dialectica Bot", session=None) -> str:
        """Challenges a claim outcome with evidence."""
        logger.info(f"Preparing to challenge claim {claim_id} with evidence: {evidence[:50]}...")
        
        if settings.MOCK_MODE:
            logger.info(f"MOCK MODE: Challenge submitted for claim {claim_id}")
            import asyncio
            await asyncio.sleep(0.5)
            return "0xchallengetransactionhash202020"

        if not self.account:
            logger.error("Cannot challenge: No active account.")
            return None

        try:
            try:
                claim_id_int = int(claim_id)
            except ValueError:
                logger.error(f"Invalid claim ID: {claim_id}")
                return None

            latest_block = self.w3.eth.get_block('pending')
            base_fee = latest_block.get('baseFeePerGas', 10**9)
            priority_fee = self.w3.eth.max_priority_fee
            max_fee = int(base_fee * 1.5 + priority_fee)

            tx_nonce = self.w3.eth.get_transaction_count(self.account.address, 'pending')

            func_call = self.claims.functions.challenge(claim_id_int)
            tx = func_call.build_transaction({
                'from': self.account.address,
                'gas': self._estimate_gas_with_buffer(func_call),
                'nonce': tx_nonce,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'type': 2,
                'chainId': self.w3.eth.chain_id
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            logger.info(f"Challenge TX Sent: {tx_hash.hex()}")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                logger.info(f"Challenge successful for claim {claim_id}.")
                return tx_hash.hex()
            else:
                logger.error(f"Challenge reverted for claim {claim_id}.")
                return None
        except ContractCustomError as e:
            error_msg = str(e)
            if any(err in error_msg for err in ["InvalidClaimStatus", "OngoingBetting", "OngoingChallenge", "NoChallenge"]):
                logger.warning(f"Phase 2 error on {claim_id}: {error_msg}. Marking as EXPIRED.")
                self._mark_claim_expired(str(claim_id), session=session)
            else:
                logger.error(f"Unhandled Contract Error in challenge: {error_msg}")
            return None
        except Exception as e:
            decoded = decode_exception(e)
            if decoded:
                self._handle_decoded_error(decoded, str(claim_id), session=session)
            else:
                logger.error(f"Challenge failed for {claim_id}: {e}")
            return None

    async def distribute_payouts(self, claim_id: str, nonce: int = None, session=None) -> str:
        """Triggers payout distribution for a settled claim."""
        logger.info(f"Preparing to distribute payouts for claim: {claim_id}")
        
        if settings.MOCK_MODE:
            logger.info(f"MOCK MODE: Payouts distributed for claim {claim_id}")
            import asyncio
            await asyncio.sleep(0.5)
            return "0xpayouttransactionhash303030"

        if not self.account:
            logger.error("Cannot distribute payouts: No active account.")
            return None

        try:
            try:
                claim_id_int = int(claim_id)
            except ValueError:
                logger.error(f"Invalid claim ID: {claim_id}")
                return None
                
            tx_nonce = nonce if nonce is not None else self.w3.eth.get_transaction_count(self.account.address, 'pending')

            latest_block = self.w3.eth.get_block('pending')
            base_fee = latest_block.get('baseFeePerGas', 10**9)
            priority_fee = self.w3.eth.max_priority_fee
            max_fee = int(base_fee * 1.2 + priority_fee)

            func_call = self.claims.functions.distributePayouts(claim_id_int)
            tx = func_call.build_transaction({
                'from': self.account.address,
                'gas': self._estimate_gas_with_buffer(func_call),
                'nonce': tx_nonce,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'type': 2,
                'chainId': self.w3.eth.chain_id
            })
            
            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_hex = tx_hash.hex()
            logger.info(f"Distribute Payouts TX Sent: {tx_hex}")
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                logger.info(f"Payouts distributed successfully for claim {claim_id}.")
                return tx_hex
            else:
                logger.error(f"Distribute payouts reverted for claim {claim_id}.")
                return None
        except ContractCustomError as e:
            error_msg = str(e)
            if any(err in error_msg for err in ["InvalidClaimStatus", "OngoingBetting", "OngoingChallenge", "NoChallenge"]):
                logger.warning(f"Phase 2 error on {claim_id}: {error_msg}. Marking as EXPIRED.")
                self._mark_claim_expired(str(claim_id), session=session)
            else:
                logger.error(f"Unhandled Contract Error in payout: {error_msg}")
            return None
        except Exception as e:
            decoded = decode_exception(e)
            if decoded:
                self._handle_decoded_error(decoded, str(claim_id), session=session)
            else:
                logger.error(f"Distribute payouts failed: {e}")
            return None

    async def refund_claim(self, claim_id: str, nonce: int = None, session=None) -> str:
        """
        Calls claims.refund(claimId, [myAddress]) when a claim settles with outcome 0.
        This returns the original USDC stake to the Treasury.
        """
        logger.info(f"Requesting refund for voided claim {claim_id}...")

        if settings.MOCK_MODE:
            logger.info(f"MOCK MODE: Refund for claim {claim_id}")
            return "0xmock_refund_hash"

        if not self.account:
            logger.error("Cannot refund: No active account.")
            return None

        try:
            claim_id_int = int(claim_id)
            
            tx_nonce = nonce if nonce is not None else self.w3.eth.get_transaction_count(self.account.address, 'pending')

            latest_block = self.w3.eth.get_block('pending')
            base_fee = latest_block.get('baseFeePerGas', 10**9)
            priority_fee = self.w3.eth.max_priority_fee
            max_fee = int(base_fee * 1.2 + priority_fee)

            func_call = self.claims.functions.refund(claim_id_int, [self.account.address])
            tx = func_call.build_transaction({
                'from': self.account.address,
                'gas': self._estimate_gas_with_buffer(func_call),
                'nonce': tx_nonce,
                'maxFeePerGas': max_fee,
                'maxPriorityFeePerGas': priority_fee,
                'type': 2,
                'chainId': self.w3.eth.chain_id
            })

            signed_tx = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_hex = tx_hash.hex()
            logger.info(f"Refund TX Sent: {tx_hex}")

            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt.status == 1:
                logger.info(f"Refund successful for claim {claim_id}.")
                return tx_hex
            else:
                logger.error(f"Refund reverted for claim {claim_id}.")
                return None
        except ContractCustomError as e:
            error_msg = str(e)
            if any(err in error_msg for err in ["InvalidClaimStatus", "OngoingBetting", "OngoingChallenge", "NoChallenge"]):
                logger.warning(f"Phase 2 error on {claim_id}: {error_msg}. Marking as EXPIRED.")
                self._mark_claim_expired(str(claim_id), session=session)
            else:
                logger.error(f"Unhandled Contract Error in refund: {error_msg}")
            return None
        except Exception as e:
            decoded = decode_exception(e)
            if decoded:
                self._handle_decoded_error(decoded, str(claim_id), session=session)
            else:
                logger.error(f"Refund failed for {claim_id}: {e}")
            return None

    async def get_treasury_balance(self) -> float:
        """
        Returns the bot's USDC balance inside the Treasury contract (6 decimals).
        This is the actual "Betting Power".
        """
        if not self.account:
            return 0.0

        try:
            treasury_abi = [
                {"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}
            ]
            treasury_contract = self.w3.eth.contract(address=self.treasury_address, abi=treasury_abi)
            raw_balance = treasury_contract.functions.balanceOf(self.account.address).call()
            usdc_bal = raw_balance / 10**6
            if self.betting_paused and usdc_bal > 0:
                logger.info(f"Treasury balance enriched to {usdc_bal} USDC. Resuming betting circuit breaker.")
                self.betting_paused = False
            return usdc_bal
        except Exception as e:
            logger.warning(f"Could not fetch Treasury balance: {e}")
            return 0.0
