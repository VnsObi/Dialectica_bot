from datetime import datetime
from pathlib import Path
from typing import List
import os
from openai import OpenAI
from pydantic import BaseModel, Field
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential
from ..utils.models import Claim, ResearchPlan, Evidence, Decision, Operator, Predicate
from ..utils.config import settings

# Load Oracle System Prompt from file (once at module level)
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_ORACLE_PROMPT_PATH = _PROMPTS_DIR / "oracle_system.txt"

def _load_prompt(path: Path) -> str:
    """Loads a prompt from a text file. Raises if file is missing."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").strip()

ORACLE_SYSTEM_PROMPT = _load_prompt(_ORACLE_PROMPT_PATH)
logger.info(f"Loaded Oracle system prompt from {_ORACLE_PROMPT_PATH} ({len(ORACLE_SYSTEM_PROMPT)} chars)")

class VerificationResult(BaseModel):
    stance: str = Field(description="The final stance: 'TRUE', 'FALSE', or 'NO_BET'")
    confidence: float = Field(description="Certainty of the STANCE (0.0 to 1.0). If you are 95% sure the claim is FALSE, output 0.95 (not 0.05).")
    rationale: str = Field(description="A concise 1-2 sentence summary of the verdict and the single strongest piece of evidence supporting it. This is a short executive summary, not the full analysis.")
    oracle_adjudication_paragraph: str = Field(description="The full, detailed Oracle-style analytical paragraph as specified in the system prompt. This is your primary output: one dense, continuous paragraph (100-200 words) following the five-phase argument architecture (Verdict-First Opening, Primary Evidence Anchor, Specificity Cascade, Counter-Evidence Handling, Terminal Lock). Do NOT summarize or truncate.")
    is_quote_or_number: bool = Field(description="True if the claim attributes a specific quote or specific number to a person/entity.", default=False)
    has_primary_source: bool = Field(description="True if a primary source (video, official transcript, direct report) confirms the exact quote/number.", default=False)
    is_secondary_only: bool = Field(description="True if the evidence is only secondary (hearsay, third-party reports) or circumstantial for the specific quote/number.", default=False)


class DecisionEngine:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key)
        else:
            self.client = None
            logger.warning("OPENAI_API_KEY missing in DecisionEngine. RAG features will be disabled.")

    def evaluate(self, claim: Claim, plan: ResearchPlan, evidence_list: List[Evidence]) -> Decision:
        try:
            # MVP: Check the first predicate against the first piece of evidence
            if not plan.predicates:
                 return Decision(claim_id=claim.id, on_chain_id=claim.on_chain_id, stance="NO_BET", confidence=0.0, rationale="No plan/predicates.")
                
            # Support multiple evidence items per predicate
            # For simplicity in V1, grab the first relevant evidence 
            predicate = plan.predicates[0]
            
            # New Logic: TRUTH_VERIFICATION (Search-Based)
            if predicate.metric == "TRUTH_VERIFICATION":
                return self._evaluate_truth(claim, predicate, evidence_list)

            # Existing Logic: PRICE
            if not evidence_list:
                return Decision(
                    claim_id=claim.id,
                    on_chain_id=claim.on_chain_id,
                    stance="NO_BET",
                    confidence=0.0,
                    rationale="Insufficient evidence."
                )

            evidence = evidence_list[0]
            
            if not evidence.structured_data or "current_value" not in evidence.structured_data:
                 return Decision(
                    claim_id=claim.id,
                    on_chain_id=claim.on_chain_id,
                    stance="NO_BET",
                    confidence=0.0,
                    rationale="Evidence lacks structured data."
                )

            current_val = evidence.structured_data["current_value"]
            target_val = predicate.target_value
            op = predicate.operator
            
            # 1. Check Condition
            condition_met = False
            if op == Operator.GREATER_THAN_EQ:
                condition_met = current_val >= target_val
            elif op == Operator.GREATER_THAN:
                condition_met = current_val > target_val
            elif op == Operator.LESS_THAN_EQ:
                condition_met = current_val <= target_val
            elif op == Operator.LESS_THAN:
                condition_met = current_val < target_val
            elif op == Operator.EQUAL:
                condition_met = current_val == target_val

            # 2. Assign Confidence
            # Rules:
            # - If condition is MET -> TRUE with High Confidence (it happened).
            # - If condition is NOT MET -> NO_BET (could happen later), unless deadline is very close.
            
            stance = "NO_BET"
            confidence = 0.0
            rationale = f"Current value {current_val} vs Target {target_val} ({op}). Condition Met: {condition_met}."

            if condition_met:
                stance = "TRUE"
                confidence = 1.0
                rationale += " Target achieved."
            else:
                # If not met, do we vote FALSE? Only if time is up.
                time_remaining = claim.deadline - datetime.utcnow()
                if time_remaining.days < 2:
                    stance = "FALSE"
                    confidence = 0.9
                    rationale += " Target missed and deadline imminent."
                else:
                    stance = "NO_BET"
                    confidence = 0.5
                    rationale += " Target not met yet, but time remains."
                    
            return Decision(
                claim_id=claim.id,
                on_chain_id=claim.on_chain_id,
                stance=stance,
                confidence=confidence,
                rationale=rationale
            )
            
        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            return Decision(claim_id=claim.id, on_chain_id=claim.on_chain_id, stance="NO_BET", confidence=0.0, rationale="Error in numeric evaluation")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _evaluate_truth(self, claim: Claim, predicate: Predicate, evidence_list: List[Evidence]) -> Decision:
            """
            Uses an LLM (RAG) to determine if search snippets confirm or refute the statement.
            """
            if not evidence_list:
                 return Decision(claim_id=claim.id, on_chain_id=claim.on_chain_id, stance="NO_BET", confidence=0.0, rationale="No search results found.")

            # Combine all snippets into a context block
            all_text = ""
            for ev in evidence_list:
                if ev.structured_data and "raw_snippets" in ev.structured_data:
                    for snippet in ev.structured_data["raw_snippets"]:
                        # Includes title and body for context
                        all_text += f"Source: {snippet.get('title', 'Unknown')}\nContent: {snippet.get('body', '')}\n---\n"
            
            if not all_text.strip():
                return Decision(claim_id=claim.id, on_chain_id=claim.on_chain_id, stance="NO_BET", confidence=0.0, rationale="Search results were empty.")

            # Fallback if OpenAI is not available
            if not self.client:
                 return Decision(claim_id=claim.id, on_chain_id=claim.on_chain_id, stance="NO_BET", confidence=0.0, rationale="OpenAI client not initialized for RAG.")

            # Load Oracle System Prompt from file
            system_prompt = ORACLE_SYSTEM_PROMPT

            user_content = (
                f"Evaluate this claim based on the gathered research: {predicate.statement}\n\nResearch:\n{all_text}"
                "\n\n---\nREMINDER: The 'oracle_adjudication_paragraph' field MUST be a single, dense, "
                "continuous paragraph of 100-200 words following the five-phase argument architecture "
                "from your system prompt (Verdict-First Opening → Primary Evidence Anchor → Specificity "
                "Cascade → Counter-Evidence Handling → Terminal Lock). Do NOT truncate or summarize."
            )

            try:
                logger.info(f"Submitting Evidence to LLM for Claim {claim.id}...")
                response = self.client.beta.chat.completions.parse(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    response_format=VerificationResult,
                )
                
                result = response.choices[0].message.parsed
                
                # Application of Secondary Evaluation Layer Logic
                final_confidence = result.confidence
                final_stance = result.stance

                logger.info(f"LLM RAG Decision: {final_stance} ({final_confidence})")
                
                return Decision(
                    claim_id=claim.id,
                    on_chain_id=claim.on_chain_id,
                    stance=final_stance,
                    confidence=final_confidence,
                    rationale=result.rationale,
                    reasoning=result.oracle_adjudication_paragraph
                )

            except Exception as e:
                logger.error(f"LLM RAG Evaluation failed: {e}")
                return Decision(claim_id=claim.id, on_chain_id=claim.on_chain_id, stance="NO_BET", confidence=0.0, rationale=f"RAG Error: {e}", reasoning="RAG Error")
