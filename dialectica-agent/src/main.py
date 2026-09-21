import asyncio
import time
import sys
import os
import argparse
from datetime import datetime, timezone

# Ensure src is in path so imports work
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(current_dir))

from loguru import logger
# Add generic Critical alert webhook trap
from src.utils.alerting import get_slack_sink
logger.add(get_slack_sink(), level="CRITICAL")
from src.utils.database import init_db, ClaimModel, DecisionModel, ActionModel
from src.utils.config import settings
from src.utils.models import ClaimStatus
from src.ingestion.api_client import DialecticaClient
from src.interpretation.llm import ClaimsInterpreter
from src.research.tools import CompositeTool
from src.decision.logic import DecisionEngine
from src.execution.wallet import Wallet
from src.execution.api_poller import ApiPoller
from src.execution.guards import validate_betting_eligibility, validate_challenge_eligibility, can_bet_batch

async def monitor_settlements(client, wallet, SessionLocal):
    """
    Audit & Challenge Loop: Fetches claims in CHALLENGE_PERIOD,
    cross-references with our predictions, and challenges when we
    disagree with high confidence.
    """
    logger.info("--- Running Settlement Monitor (Audit & Challenge) ---")
    try:
        challenge_claims = await client.fetch_challenge_claims()
        if not challenge_claims:
            logger.info("No claims in CHALLENGE_PERIOD.")
            return

        session = SessionLocal()
        try:
            for claim in challenge_claims:
                # Check if we already challenged this claim
                already_challenged = session.query(ActionModel).filter_by(
                    claim_id=claim.id,
                    action_type='CHALLENGE'
                ).first()
                if already_challenged:
                    continue

                # Look up our original decision
                original_decision = session.query(DecisionModel).filter(
                    DecisionModel.claim_id == claim.id,
                    DecisionModel.stance.in_(['TRUE', 'FALSE'])
                ).order_by(DecisionModel.timestamp.desc()).first()

                if not original_decision:
                    logger.debug(f"No prior decision for challenge claim {claim.id}. Skipping.")
                    continue

                # Extract proposed outcome from API raw_data
                proposed_outcome = "UNKNOWN"
                if claim.raw_data and 'outcome' in claim.raw_data:
                    outcome_val = claim.raw_data['outcome']
                    proposed_outcome = "TRUE" if outcome_val == 1 else "FALSE" if outcome_val == 2 else "UNKNOWN"
                elif claim.raw_data and 'rounds' in claim.raw_data:
                    # Try extracting from the latest round's proposed resolution
                    rounds = claim.raw_data.get('rounds', [])
                    if rounds:
                        latest = rounds[-1]
                        if 'proposedOutcome' in latest:
                            po = latest['proposedOutcome']
                            proposed_outcome = "TRUE" if po == 1 else "FALSE" if po == 2 else "UNKNOWN"

                if proposed_outcome == "UNKNOWN":
                    logger.debug(f"Cannot determine proposed outcome for claim {claim.id}. Skipping.")
                    continue

                bot_stance = original_decision.stance
                bot_confidence = original_decision.confidence

                # Discrepancy Trigger: bot disagrees AND confidence > 0.90
                if proposed_outcome != bot_stance and bot_confidence > 0.90:
                    # ── PRE-FLIGHT: validate_challenge_eligibility ──
                    try:
                        c_id_int = int(claim.on_chain_id or claim.id)
                        c_round = getattr(claim, 'round_number', 0) or 0
                        ch_ok, ch_reason = await validate_challenge_eligibility(
                            wallet=wallet,
                            claim_id=c_id_int,
                            collection_id=getattr(claim, 'collection_id', None),
                            round_index=c_round,
                            treasury_cache=None,
                        )
                        if not ch_ok:
                            logger.warning(
                                f"Challenge SKIPPED for claim {claim.id}: {ch_reason}"
                            )
                            continue
                    except Exception as cg_err:
                        logger.error(f"Challenge guard error for claim {claim.id}: {cg_err}")

                    logger.warning(
                        f"CHALLENGE TRIGGERED for Claim {claim.id}: "
                        f"Proposed={proposed_outcome}, Bot={bot_stance} ({bot_confidence:.0%})"
                    )

                    evidence_text = original_decision.rationale or "Automated dispute by Dialectica Bot"
                    tx_hash = await wallet.challenge_claim(claim.id, evidence=evidence_text)

                    if tx_hash:
                        logger.info(f"Challenge executed for Claim {claim.id}: {tx_hash}")
                        db_action = ActionModel(
                            tx_hash=tx_hash,
                            claim_id=claim.id,
                            action_type="CHALLENGE",
                            amount=0.0,
                            timestamp=datetime.utcnow()
                        )
                        session.add(db_action)
                        session.commit()
                else:
                    if proposed_outcome != bot_stance:
                        logger.info(
                            f"Claim {claim.id}: Disagree (Proposed={proposed_outcome}, Bot={bot_stance}) "
                            f"but confidence too low ({bot_confidence:.0%}). No challenge."
                        )
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Error in monitor_settlements: {e}")


async def process_active_bets(client, interpreter, tool, engine, wallet, SessionLocal):
    """
    Watches active bets for state changes (Challenge/Payout).
    """
    session = SessionLocal()
    try:
        # Get all claims we have voted on
        active_claim_ids = session.query(ActionModel.claim_id)\
            .filter(ActionModel.action_type == 'VOTE')\
            .distinct()\
            .all()
        
        for (claim_id,) in active_claim_ids:
            logger.info(f"Checking active bet for Claim {claim_id}")

            # Fetch up-to-date claim status
            claim = await client.fetch_claim(claim_id)
            if not claim:
                logger.warning(f"Could not fetch claim {claim_id} (404/500 HTTP Error). Suppressing deletion to protect records.")
                continue

            # Only track claims with status OPEN_FOR_BETTING or CHALLENGE_PERIOD
            if claim.status not in [ClaimStatus.OPEN_FOR_BETTING, ClaimStatus.CHALLENGE_PERIOD, "OPEN_FOR_BETTING", "CHALLENGE_PERIOD", "CLOSED"]:
                logger.info(f"Skipping claim {claim_id} with status {claim.status}.")
                continue

            # CHALLENGE LOGIC
            if claim.status == "CHALLENGE_PERIOD":
                # Check if we already challenged
                already_challenged = session.query(ActionModel).filter_by(
                    claim_id=claim_id, 
                    action_type='CHALLENGE'
                ).first()
                
                if not already_challenged:
                    logger.info(f"Claim {claim_id} is in CHALLENGE_PERIOD. Reviewing outcome...")
                    
                    # 1. Fetch our original decision stance
                    original_decision = session.query(DecisionModel).filter(
                        DecisionModel.claim_id == claim.id,
                        DecisionModel.stance.in_(['TRUE', 'FALSE'])
                    ).order_by(DecisionModel.timestamp.desc()).first()
                    
                    if not original_decision:
                        logger.warning(f"No original decision found for Claim {claim_id}. Skipping challenge.")
                        continue

                    # 2. Re-run pipeline to confirm our belief (in case new info emerged)
                    # For strict "Discrepancy Trigger", we rely on our original high confidence + market mismatch.
                    # But verifying again adds robustness.
                    
                    # 3. Simulate getting Market Outcome (MVP: Assuming FALSE if we bet TRUE and it's disputed)
                    # To do this for real, we need `claim.outcome` from the API.
                    # Let's assume `claim.raw_data` has the outcome or we can infer it.
                    # As per instruction: "if market_outcome != bot_stance AND bot_confidence > 0.85"
                    
                    # Fetch outcome from raw_data if available (e.g. 'outcome' field)
                    # If not available in mock/MVP, we act on the Discrepancy instruction literally:
                    # We need to KNOW the market outcome.
                    if 'outcome' in claim.raw_data:
                         market_outcome_val = claim.raw_data['outcome'] # e.g. 1 (TRUE), 2 (FALSE)
                         market_outcome = "TRUE" if market_outcome_val == 1 else "FALSE" if market_outcome_val == 2 else "UNKNOWN"
                    else:
                         # Default to Unknown, skip challenge if we can't see the outcome
                         market_outcome = "UNKNOWN"

                    bot_stance = original_decision.stance
                    bot_confidence = original_decision.confidence
                    
                    # Discrepancy Trigger
                    # Only challenge if we KNOW the market is wrong and we were very sure (>0.85)
                    should_challenge = False
                    
                    if market_outcome != "UNKNOWN" and market_outcome != bot_stance and bot_confidence > 0.85:
                        logger.warning(f"DISCREPANCY DETECTED for Claim {claim_id}: Market says {market_outcome}, Bot says {bot_stance} ({bot_confidence}).")
                        should_challenge = True
                    
                    if should_challenge:
                         # ── PRE-FLIGHT: validate_challenge_eligibility ──
                         try:
                             ab_cid = int(claim.on_chain_id or claim.id)
                             ab_rnd = getattr(claim, 'round_number', 0) or 0
                             ab_ok, ab_reason = await validate_challenge_eligibility(
                                 wallet=wallet,
                                 claim_id=ab_cid,
                                 collection_id=getattr(claim, 'collection_id', None),
                                 round_index=ab_rnd,
                             )
                             if not ab_ok:
                                 logger.warning(
                                     f"Challenge SKIPPED for claim {claim_id}: {ab_reason}"
                                 )
                                 continue
                         except Exception as abg_err:
                             logger.error(f"Challenge guard error: {abg_err}")

                         logger.info(f"High confidence discrepancy ({bot_confidence}). Initiating challenge...")
                         
                         reasoning_evidence = original_decision.rationale
                         # Truncate to fit string limit if necessary, or pass full url list
                         
                         tx_hash = await wallet.challenge_claim(claim.id, evidence=reasoning_evidence)
                         
                         if tx_hash:
                             logger.info(f"Challenge executed: {tx_hash}")
                             # Persist CHALLENGE
                             db_action = ActionModel(
                                 tx_hash=tx_hash,
                                 claim_id=claim.id,
                                 action_type="CHALLENGE",
                                 amount=0.0, # Cost logic to be added
                                 timestamp=datetime.utcnow()
                             )
                             session.add(db_action)
                             session.commit()

            # PAYOUT / REFUND LOGIC
            elif claim.status == "CLOSED":
                 # Check if we already claimed payout or refund
                 already_paid = session.query(ActionModel).filter_by(
                     claim_id=claim_id, 
                     action_type='PAYOUT'
                 ).first()
                 already_refunded = session.query(ActionModel).filter_by(
                     claim_id=claim_id,
                     action_type='REFUND'
                 ).first()
                 
                 if not already_paid and not already_refunded:
                     logger.info(f"Claim {claim_id} is CLOSED. Checking settlement outcome...")
                     
                     # Check outcome: 0=void/refund, 1=TRUE, 2=FALSE
                     outcome = None
                     if claim.raw_data:
                         outcome = claim.raw_data.get('outcome')
                         if outcome is None and 'rounds' in claim.raw_data and claim.raw_data['rounds']:
                             outcome = claim.raw_data['rounds'][-1].get('outcome')

                     if outcome == 0:
                         # VOID: Trigger refund
                         logger.info(f"Claim {claim_id} voided (outcome=0). Requesting refund...")
                         tx_hash = await wallet.refund_claim(claim.id, session=session)
                         action_type = "REFUND"
                     else:
                         # WIN or LOSS: Trigger payout distribution
                         logger.info(f"Claim {claim_id} settled (outcome={outcome}). Distributing payouts...")
                         tx_hash = await wallet.distribute_payouts(claim.id, session=session)
                         action_type = "PAYOUT"
                     
                     if tx_hash:
                         logger.info(f"{action_type} executed: {tx_hash}")
                         db_action = ActionModel(
                             tx_hash=tx_hash,
                             claim_id=claim.id,
                             action_type=action_type,
                             amount=0.0, 
                             timestamp=datetime.utcnow()
                         )
                         session.add(db_action)
                           
                         # Only drop claim from the DB after successful payout/refund
                         num_deleted_decisions = session.query(DecisionModel).filter(DecisionModel.claim_id == claim.id).delete()
                         num_deleted_claims = session.query(ClaimModel).filter(ClaimModel.id == claim.id).delete()
                         
                         session.commit()
                         logger.info(f"Successfully dropped claim {claim.id} from local tracking DB. (Claims={num_deleted_claims}, Decisions={num_deleted_decisions})")

    except Exception as e:
        logger.error(f"Error in process_active_bets: {e}")
    finally:
        session.close()

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    args = parser.parse_args()

    logger.info(f"Starting Dialectica Agent (Mock Mode: {settings.MOCK_MODE})")
    if not settings.MOCK_MODE:
        logger.info("PRODUCTION MODE ACTIVE: Real LLM and Real Parsing enabled.")
        logger.info(f"Environment Check:")
        logger.info(f"  - CLAIM: {settings.CLAIMS_ADDRESS}")
        logger.info(f"  - TREASURY: {settings.TREASURY_ADDRESS}")
        logger.info(f"  - USDC: {settings.USDC_ADDRESS}")
        logger.info(f"  - COLLECTIONS: {settings.COLLECTIONS_ADDRESS}")
        logger.info(f"  - ROUNDS: {settings.ROUNDS_ADDRESS}")

        # Explicit Fail-Safe Check (Double verification)
        required_vars = [
            settings.CLAIMS_ADDRESS,
            settings.TREASURY_ADDRESS,
            settings.USDC_ADDRESS,
            settings.COLLECTIONS_ADDRESS,
            settings.ROUNDS_ADDRESS
        ]
        if any(v is None for v in required_vars):
             logger.critical("CRITICAL: One or more required Smart Contract addresses are missing!")
             sys.exit(1)

    
    # Initialize Database
    try:
        SessionLocal = init_db(settings.DATABASE_URL)
        logger.info("Database initialized.")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        return


    # Initialize Client and Interpreter
    client = DialecticaClient()
    interpreter = ClaimsInterpreter()
    tool = CompositeTool()
    engine = DecisionEngine()
    wallet = Wallet()
    api_poller = ApiPoller(wallet, SessionLocal)

    # Start ApiPoller in background (monitors active claims and process payouts)
    poller_task = asyncio.create_task(api_poller.start())

    # Watchdog to restart process if main loop stalls (e.g. hung RPC/LLM calls)
    last_poll_time = time.time()
    
    async def watchdog():
        nonlocal last_poll_time
        while True:
            await asyncio.sleep(10)
            if time.time() - last_poll_time > (2 * settings.POLL_INTERVAL) + 30: # +30s buffer
                logger.critical(f"WATCHDOG: Main loop stalled. Restarting process...")
                os._exit(1)
                
    watchdog_task = asyncio.create_task(watchdog())

    # Main Loop
    while True:
        try:
            # 0. Balance Check & Treasury Management
            balances = await wallet.get_balances()
            treasury_usdc = await wallet.get_treasury_balance()
            logger.info(f"Wallet: ETH {balances['eth']:.4f} | USDC {balances['usdc']:.2f}")
            logger.info(f"Treasury Betting Power: {treasury_usdc:.2f} USDC")

            # Ensure Treasury has minimum funds (from INITIAL_DEPOSIT_USDC, default 2.0)
            # Only if not in Mock Mode to avoid noise
            if not settings.MOCK_MODE:
                deposit_wei = int(settings.INITIAL_DEPOSIT_USDC * 10**6)
                await wallet.ensure_treasury_balance(deposit_wei)

            last_poll_time = time.time()
            logger.info("Polling for claims...")
            claims = []
            page = 1
            while True:
                logger.info(f"Fetching claims page {page}...")
                page_claims = await client.fetch_claims(page=page, limit=50)
                if not page_claims:
                    break
                claims.extend(page_claims)
                if len(page_claims) < 50:
                    break
                page += 1
            
            logger.info(f"Found {len(claims)} active claims.")
            
            # 1. Process New Claims
            # ── Batch pre-flight: filter all OPEN_FOR_BETTING claims via Multicall3 ──
            open_claims = [
                c for c in claims
                if c.status == ClaimStatus.OPEN_FOR_BETTING and treasury_usdc > 0.0
            ]
            
            # Sort claims by deadline to prioritize urgent bets (Gap 9)
            open_claims.sort(key=lambda c: getattr(c, 'current_deadline', None) or c.deadline)

            if treasury_usdc == 0.0:
                logger.critical(
                    "BETTING PAUSED (InsufficientBalance). "
                    "Skipping all claims until Treasury Deposit event."
                )
                open_claims = []

            # Group claims by collection_id
            claims_by_collection = {}
            for idx, c in enumerate(open_claims):
                cid = int(c.on_chain_id or c.id)
                ridx = getattr(c, 'round_number', 0) or 0
                col_id = getattr(c, 'collection_id', 1) or 1
                claims_by_collection.setdefault(col_id, []).append((idx, cid, ridx))

            guard_results = [None] * len(open_claims)
            
            for col_id, pairs_with_idx in claims_by_collection.items():
                batch_pairs = [(cid, ridx) for _, cid, ridx in pairs_with_idx]
                try:
                    batch_res = can_bet_batch(wallet, batch_pairs, col_id)
                    if batch_res:
                        for i, res in enumerate(batch_res):
                            guard_results[pairs_with_idx[i][0]] = res
                except Exception as batch_err:
                    logger.error(f"Batch guard failed for collection {col_id} ({batch_err}).")
            

            for idx, claim in enumerate(open_claims):
                logger.info(f"Processing Claim {claim.id}: {claim.text}")

                # Prevent duplicate bets: skip if already voted
                session = SessionLocal()
                already_voted = session.query(ActionModel).filter_by(
                    claim_id=claim.id,
                    action_type="VOTE"
                ).first()
                session.close()
                if already_voted:
                    logger.info(f"Already voted on claim {claim.id}. Skipping duplicate bet.")
                    continue

                # ── PRE-FLIGHT (batch result or fallback to single) ──
                if guard_results is not None and idx < len(guard_results):
                    if not guard_results[idx]:
                        logger.warning(
                            f"Claim {claim.id} SKIPPED by batch guard: {guard_results[idx].reason}"
                        )
                        continue
                else:
                    # Fallback: single-claim guard
                    try:
                        claim_id_int = int(claim.on_chain_id or claim.id)
                        round_idx = getattr(claim, 'round_number', 0) or 0
                        eligible, reason = await validate_betting_eligibility(
                            wallet=wallet,
                            claim_id=claim_id_int,
                            collection_id=getattr(claim, 'collection_id', None),
                            round_index=round_idx,
                            treasury_cache=treasury_usdc,
                        )
                        if not eligible:
                            logger.warning(
                                f"Claim {claim.id} SKIPPED by betting guard: {reason}"
                            )
                            continue
                    except Exception as guard_err:
                        logger.error(f"Betting guard error for claim {claim.id}: {guard_err}")

                 # PERSIST CLAIM
                session = SessionLocal()
                try:
                    exists = session.query(ClaimModel).filter_by(id=claim.id).first()
                    if not exists:
                        # Handle potential missing attributes safely
                        status_val = claim.status.value if hasattr(claim.status, 'value') else str(claim.status)
                        deadline_val = claim.deadline if hasattr(claim, 'deadline') else datetime.now(timezone.utc)
                        
                        db_claim = ClaimModel(
                            id=claim.id,
                            on_chain_id=claim.on_chain_id,
                            text=claim.text,
                            status=status_val,
                            deadline=deadline_val
                        )
                        session.add(db_claim)
                        session.commit()
                        
                    # 1. ONE-SHOT CHECK: Skip if we already decided TRUE/FALSE
                    existing_decision = session.query(DecisionModel).filter(
                        DecisionModel.claim_id == claim.id, 
                        DecisionModel.stance.in_(['TRUE', 'FALSE'])
                    ).first()
                    
                    if existing_decision:
                        logger.info(f"Claim {claim.id} already settled (Stance: {existing_decision.stance}). Skipping.")
                        continue
                        
                except Exception as e:
                    logger.error(f"Error persisting/checking claim: {e}")
                    session.rollback()
                finally:
                    session.close()

                # LOGIC PIPELINE
                plan = await interpreter.parse_claim(claim)
                logger.info(f"Interpreted Plan: {plan}")
                
                # Research
                evidences = []
                for predicate in plan.predicates:
                    evidence = await tool.check(predicate)
                    if evidence:
                        evidences.append(evidence)
                        logger.info(f"Evidence Found: {evidence.summary}")
                
                decision = engine.evaluate(claim, plan, evidences)
                logger.info(f"Decision: {decision.stance} ({decision.confidence*100}%) | Reason: {decision.reasoning}")
                
                # PERSIST DECISION
                session = SessionLocal()
                try:
                    db_decision = DecisionModel(
                        claim_id=claim.id,
                        stance=str(decision.stance),
                        confidence=float(decision.confidence),
                        rationale=str(decision.rationale)
                    )
                    session.add(db_decision)
                    session.commit()
                except Exception as e:
                    logger.error(f"Error persisting decision: {e}")
                    session.rollback()
                finally:
                    session.close()

                if decision.confidence >= settings.MIN_CONFIDENCE:
                    logger.info(f"Confidence meets threshold ({settings.MIN_CONFIDENCE}). Executing trade...")
                    tx_hash, amount_used = await wallet.broadcast_vote(
                        decision,
                        collection_id=getattr(claim, 'collection_id', None),
                        session=session
                    )
                    
                    if tx_hash:
                        logger.info(f"Action executed: {tx_hash}")
                        # PERSIST ACTION
                        session = SessionLocal()
                        try:
                            # Avoid duplicates from mock hash
                            if not session.query(ActionModel).filter_by(tx_hash=tx_hash).first():
                                db_action = ActionModel(
                                    tx_hash=tx_hash,
                                    claim_id=claim.id,
                                    action_type="VOTE",
                                    amount=amount_used,
                                    timestamp=datetime.utcnow()
                                )
                                session.add(db_action)
                                session.commit()
                                
                            # Check if decision was already persisted as TRUE/FALSE
                            # If for some reason we missed it or need to confirm "settled" status
                            # This is redundant given earlier persistence but ensures safety
                            
                        except Exception as e:
                            logger.error(f"Error persisting action: {e}")
                            session.rollback()
                        finally:
                            session.close()
                else:
                    logger.info(f"Confidence below threshold ({settings.MIN_CONFIDENCE}). Skipping execution.")
                    # Optional: Record DECISION_MADE here too if we want to stop re-analyzing low-confidence claims forever?
                    # Given the instruction "Optimize Persistence Logic (Stop Repetitive Analysis)", 
                    # it implies we should stop analyzing once we've made up our mind for this cycle.
                    # But if we don't persist it, next cycle will re-analyze.
                    # I will infer the user wants to stop re-analyzing success/fail cases primarily.
                    # But "The agent is still parsing and analyzing claims... even after it has already decided to vote on them."
                    # This implies successful decision -> stop.
                    # I will stick to the explicit instruction: "Even if the blockchain transaction fails... record ... DECISION_MADE". 
                    # It doesn't explicitly say "If low confidence, record DECISION_MADE". I'll leave low-confidence alone to allow re-evaluation unless instructed.
                    logger.info(f"Confidence below threshold ({settings.MIN_CONFIDENCE}). Skipping execution.")

            # 2. Audit & Challenge Settlement Monitor
            logger.info("Running Settlement Monitor...")
            await monitor_settlements(
                client=client,
                wallet=wallet,
                SessionLocal=SessionLocal
            )

            # 3. Process Active Bets (Legacy Challenge / Payout)
            logger.info("Running Post-Bet Watcher...")
            await process_active_bets(
                client=client,
                interpreter=interpreter,
                tool=tool,
                engine=engine,
                wallet=wallet,
                SessionLocal=SessionLocal
            )

            if args.once:
                logger.info("Single cycle complete. Exiting.")
                break

            logger.info(f"Sleeping for {settings.POLL_INTERVAL} seconds...")
            await asyncio.sleep(settings.POLL_INTERVAL)
            
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            await asyncio.sleep(5)
            
    # Graceful shutdown for the poller task and watchdog
    poller_task.cancel()
    watchdog_task.cancel()
    try:
        await poller_task
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
