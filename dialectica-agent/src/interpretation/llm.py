import os
import asyncio
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential
from pydantic import BaseModel, Field
from openai import OpenAI, APIConnectionError, RateLimitError
from ..utils.models import Claim
from .models import Predicate, Operator, Plan

# Define the exact strict schema we expect from the LLM
class PredicateSchema(BaseModel):
    asset: str = Field(description="The financial asset or subject entity, e.g., 'BITCOIN', 'ETH', 'SINALOA_CARTEL'")
    metric: str = Field(description="The metric: 'PRICE', 'MARKET_CAP', or 'TRUTH_VERIFICATION' for news/facts")
    operator: str = Field(description="The comparison operator: '>', '<', '>=', '<=', '=='")
    target_value: float = Field(description="Numerical target (use 1.0 for TRUE in verification)")
    currency: str = Field(description="Fiat currency, or 'NONE' for facts")
    statement: str = Field(description="The core factual statement to verify if metric is TRUTH_VERIFICATION")
    optimized_search_query: str = Field(description="A dense, keyword-rich search engine query derived from the claim. Strip conversational words. Focus on specific names, dates, organizations, and core actions. Example: 'Elon Musk Twitter acquisition 2022 SEC filing' instead of 'Did Elon Musk buy Twitter?'")

class ClaimsInterpreter:
    def __init__(self):
        import sys
        try:
            self.api_key = os.getenv("OPENAI_API_KEY")
            if not self.api_key:
                raise ValueError("OPENAI_API_KEY is missing from environment.")
            self.client = OpenAI(api_key=self.api_key)
        except Exception as e:
            logger.error(str(e))
            self.client = None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def parse_claim(self, claim: Claim) -> Plan:
        """
        Uses gpt-4o-mini to parse a natural language claim into strict Predicate objects.
        Returns a Plan object.
        """
        # PRODUCTION MODE: No mock fallback.
        
        system_prompt = (
            "You are a meticulous financial and factual parsing agent. "
            "For PRICE claims (prices/market caps), strict numbers required. "
            "For NEWS/FACT claims (e.g. 'Did X happen?', 'Is Y arrested?'), use metric='TRUTH_VERIFICATION'. "
            "Extract the core subject as 'asset' and the factual claim as 'statement'. "
            "Set target_value=1.0 for TRUE. "
            "If a claim is purely subjective or ambiguous, refuse to parse. "
            "You MUST also generate an 'optimized_search_query': convert the core claim into a dense, "
            "keyword-rich search query. Strip out conversational words. Focus on specific names, dates, "
            "organizations, and core actions. Example: instead of 'Did Elon Musk buy Twitter?', "
            "produce 'Elon Musk Twitter acquisition 2022 SEC filing'."
        )

        try:
            # Use OpenAI's Structured Outputs to guarantee the exact Pydantic schema
            response = self.client.beta.chat.completions.parse(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": claim.text},
                ],
                response_format=PredicateSchema,
            )

            # Access parsed data
            parsed_data = response.choices[0].message.parsed
            
            # If the model refused or failed to match the schema
            if not parsed_data:
                logger.warning(f"LLM could not confidently parse claim: {claim.id}")
                return Plan(claim_id=claim.id, predicates=[], data_sources=[], deadline_iso=claim.deadline)

            # Map the string operator back to our internal Enum safely
            op_map = {
                ">": Operator.GREATER_THAN,
                "<": Operator.LESS_THAN,
                ">=": Operator.GREATER_THAN_EQ,
                "<=": Operator.LESS_THAN_EQ,
                "==": Operator.EQUAL,
                "=": Operator.EQUAL
            }
            
            operator_enum = op_map.get(parsed_data.operator)
            if not operator_enum:
                logger.warning(f"Invalid operator returned by LLM: {parsed_data.operator}")
                return Plan(claim_id=claim.id, predicates=[], data_sources=[], deadline_iso=claim.deadline)

            # Construct and return our internal Predicate model
            predicate = Predicate(
                asset=parsed_data.asset.upper(),
                metric=parsed_data.metric.upper(),
                operator=operator_enum,
                target_value=parsed_data.target_value,
                currency=parsed_data.currency.upper(),
                statement=parsed_data.statement if parsed_data.metric == 'TRUTH_VERIFICATION' else None,
                optimized_search_query=parsed_data.optimized_search_query if parsed_data.metric == 'TRUTH_VERIFICATION' else None
            )
            
            logger.info(f"Successfully parsed claim {claim.id} via OpenAI.")
            
            return Plan(
                claim_id=claim.id,
                predicates=[predicate],
                data_sources=["search" if parsed_data.metric == 'TRUTH_VERIFICATION' else "coingecko"], 
                deadline_iso=claim.deadline
            )

        except Exception as e:
            logger.error(f"OpenAI parsing failed for claim {claim.id}: {e}")
            # In production, we return empty plan on failure rather than crashing loop, but we log loud error.
            return Plan(claim_id=claim.id, predicates=[], data_sources=[], deadline_iso=claim.deadline)
