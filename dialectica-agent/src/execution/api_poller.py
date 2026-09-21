import asyncio
import aiohttp
from loguru import logger
from ..utils.database import ClaimModel, ActionModel, DecisionModel
from ..utils.config import settings

class ApiPoller:
    """
    Polls the Dialectica API for status updates on claims we've bet on,
    bypassing the need to scan blocks via RPC.
    """
    
    POLL_SECONDS = 60

    def __init__(self, wallet, SessionLocal):
        self.wallet = wallet
        self.SessionLocal = SessionLocal
        self.base_url = settings.DIALECTICA_API_URL

    async def start(self):
        logger.info(f"ApiPoller started | polling interval={self.POLL_SECONDS}s")
        while True:
            try:
                await self.poll_active_claims()
                await self._sweep_pending_payouts()
            except Exception as exc:
                logger.error(f"ApiPoller error: {exc}")
            await asyncio.sleep(self.POLL_SECONDS)

    async def poll_active_claims(self):
        """
        Look up claims we bet on that aren't closed yet.
        Call the Dialectica API to see if they settled.
        """
        session = self.SessionLocal()
        try:
            # Find claims we made a decision on but have not been closed in our local DB
            active_claims = (
                session.query(ClaimModel)
                .join(DecisionModel, DecisionModel.claim_id == ClaimModel.id)
                .filter(
                    DecisionModel.stance.in_(["TRUE", "FALSE"]),
                    ~ClaimModel.current_status.in_(["CLOSED", "PAYOUT_CLAIMED", "REFUNDED"])
                )
                .distinct()
                .all()
            )

            if not active_claims:
                return

            for claim in active_claims:
                claim_id = claim.id
                url = f"{self.base_url}/claims/fetch-one?id={claim_id}"
                
                # Reuse session
                if not hasattr(self, '_session') or self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
                
                async with self._session.get(url) as response:
                        if response.status == 200:
                            data = await response.json()
                            status = data.get("status")
                            
                            # 1: Open, 2: Reveal, 3: Challenge, 4: Closed, 5: Inconclusive
                            
                            if status == 3:
                                if claim.current_status != "AWAITING_CHALLENGE":
                                    logger.info(f"ApiPoller: Claim {claim_id} entered Challenge Period. Evaluating Market Defense.")
                                    # Phase 2 Challenge Protocol
                                    await self._evaluate_challenge(session, claim, data)
                                    # Update before evaluating to avoid duplicate triggers
                                    claim.current_status = "AWAITING_CHALLENGE"
                                    session.commit()
                                    
                            elif status == 4:
                                if claim.current_status != "CLOSED":
                                    logger.info(f"ApiPoller: Claim {claim_id} is SETTLED (status=4). Updating DB.")
                                    claim.current_status = "CLOSED"
                                    session.commit()
                                    
                            elif status == 5:
                                if claim.current_status != "CLOSED":
                                    logger.info(f"ApiPoller: Claim {claim_id} is INCONCLUSIVE (status=5). Updating DB.")
                                    claim.current_status = "CLOSED"
                                    session.commit()
                        else:
                            logger.warning(f"ApiPoller: Failed to fetch {claim_id}: {response.status}")

        except Exception as e:
            logger.error(f"Error in poll_active_claims: {e}")
        finally:
            session.close()

    async def _evaluate_challenge(self, session, claim, api_data):
        """
        Phase 2: Challenge Protocol.
        If our Oracle is highly confident (>95%) but the market resolves against us,
        trigger a challenge using bounty configs.
        """
        try:
            decision = session.query(DecisionModel).filter(DecisionModel.claim_id == claim.id).first()
            if not decision: return
            
            # Extract market outcome from API (latest round)
            rounds = api_data.get("rounds", [])
            if not rounds: return
            latest_round = rounds[-1]
            market_result = latest_round.get("result") # 1 = TRUE, 2 = FALSE
            
            market_stance = "TRUE" if market_result == 1 else ("FALSE" if market_result == 2 else None)
            if not market_stance: return
            
            # Compare market outcome vs our predicted outcome
            # Check confidence > 0.95 or > 95 depending on scaling. Assuming 0-1 range.
            if market_stance != decision.stance and decision.confidence >= 0.95:
                logger.warning(f"Mismatched outcome on Claim {claim.id}! Market: {market_stance}, Our Stance: {decision.stance} (Conf: {decision.confidence}). Executing Challenge!")
                
                # Fetch round indices & timeline params
                collection_id = int(api_data.get("collectionId", 1))
                round_index = len(rounds) - 1 # API round arrays are ordered
                
                params = self.wallet.fetch_timeline_params(collection_id, round_index)
                bounty_amount = params.get("bounty_amount", 0)
                
                # Check treasury balance is sufficient for bounty
                treasury_balance = await self.wallet.get_treasury_balance()
                bounty_usdc = bounty_amount / 10**6
                
                if treasury_balance >= bounty_usdc:
                    logger.info(f"Treasury balance ({treasury_balance} USDC) covers bounty ({bounty_usdc} USDC). Launching Challenge.")
                    tx_hash = await self.wallet.challenge_claim(str(claim.id))
                    if tx_hash:
                        db_action = ActionModel(
                            claim_id=claim.id,
                            action_type="CHALLENGE",
                            tx_hash=tx_hash,
                            amount=bounty_amount / 10**6
                        )
                        session.add(db_action)
                        session.commit()
                else:
                    logger.critical(f"Not enough treasury balance for Challenge! Need {bounty_usdc} USDC, have {treasury_balance} USDC.")
        except Exception as e:
            logger.error(f"Error evaluating challenge for {claim.id}: {e}")

    async def _sweep_pending_payouts(self):
        """
        Find claims CLOSED without PAYOUT or REFUND, and process them.
        Updates ClaimModel.current_status to PAYOUT_CLAIMED.
        """
        session = self.SessionLocal()
        try:
            pending_claims = (
                session.query(ClaimModel)
                .join(ActionModel, ActionModel.claim_id == ClaimModel.id)
                .filter(
                    ActionModel.action_type == "VOTE",
                    ClaimModel.current_status == "CLOSED",
                )
                .distinct()
                .all()
            )

            if not pending_claims:
                return
                    
            for claim in pending_claims:
                display_id = claim.id
                claim_id_int = int(claim.on_chain_id or claim.id)
                round_index = list(claim.decisions)[0].claim.round_id if claim.decisions else 0
                
                # Fetch from on-chain state to confirm
                from .guards import encode_round_id, _raw_call_rounds
                round_id = encode_round_id(claim_id_int, round_index)
                
                try:
                    round_data = _raw_call_rounds(self.wallet.w3, self.wallet.rounds_address, round_id)
                    result = round_data[3]
                    
                    is_inconclusive = (result == 0) # Inconclusive / Voided mapped to 0 usually
                    
                    # Also need to check if we WON the bet to run distributePayouts!
                    decision = session.query(DecisionModel).filter(DecisionModel.claim_id == claim.id).first()
                    our_stance = 1 if decision.stance == "TRUE" else 2
                    
                    if is_inconclusive:
                        logger.info(f"Payout sweep: Claim {display_id} is INCONCLUSIVE -> refund")
                        tx_hash = await self.wallet.refund_claim(str(display_id))
                        if tx_hash:
                            db_action = ActionModel(claim_id=display_id, action_type="REFUND", tx_hash=tx_hash, amount=0.0)
                            session.add(db_action)
                            claim.current_status = "PAYOUT_CLAIMED"
                            session.query(DecisionModel).filter(DecisionModel.claim_id == display_id).delete()
                            session.query(ClaimModel).filter(ClaimModel.id == display_id).delete()
                            session.commit()
                            logger.info(f"Payout sweep: REFUND successful for {display_id}. Dropped from DB | tx={tx_hash}")
                        
                    elif result == our_stance:
                        logger.info(f"Payout sweep: Claim {display_id} SETTLED + WON -> distribute Payouts")
                        tx_hash = await self.wallet.distribute_payouts(str(display_id))
                        if tx_hash:
                            db_action = ActionModel(claim_id=display_id, action_type="PAYOUT", tx_hash=tx_hash, amount=0.0)
                            session.add(db_action)
                            claim.current_status = "PAYOUT_CLAIMED"
                            session.query(DecisionModel).filter(DecisionModel.claim_id == display_id).delete()
                            session.query(ClaimModel).filter(ClaimModel.id == display_id).delete()
                            session.commit()
                            logger.info(f"Payout sweep: PAYOUT successful for {display_id}. Dropped from DB | tx={tx_hash}")
                        
                    else:
                        logger.info(f"Payout sweep: Claim {display_id} SETTLED + LOST -> Mark as PAYOUT_CLAIMED (Nothing to claim)")
                        claim.current_status = "PAYOUT_CLAIMED"
                        session.commit()
                except Exception as e:
                    logger.error(f"Error checking round state for {display_id}: {e}")

        except Exception as e:
            logger.error(f"Payout sweep error: {e}")
            session.rollback()
        finally:
            session.close()

