from enum import Enum
from datetime import datetime, timezone
from typing import Optional, List, Any
from pydantic import BaseModel, Field, model_validator

class ClaimStatus(str, Enum):
    OPEN_FOR_BETTING = "OPEN_FOR_BETTING"
    REVEAL_PERIOD = "REVEAL_PERIOD"
    CHALLENGE_PERIOD = "CHALLENGE_PERIOD"
    CLOSED = "CLOSED"

class Claim(BaseModel):
    id: str
    on_chain_id: str
    collection_id: Optional[int] = None  # For getCoreParams(collectionId) lookup
    text: str
    status: ClaimStatus
    deadline: datetime
    collateral: float = 0.0
    round_number: int = 1
    current_deadline: Optional[datetime] = None  # Latest round deadline (may differ from initial)
    # Using default_factory to ensure a new timestamp is generated for each instance
    created_at: datetime = Field(default_factory=datetime.utcnow)
    raw_data: Optional[dict] = None

    @model_validator(mode='before')
    @classmethod
    def transform_api_response(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Check if this is a raw API response (e.g. has 'rounds' or 'frontStatement')
            if 'rounds' in data or 'frontStatement' in data:
                # Map Status ID to Enum
                status_raw = data.get("status")
                status_map = {
                    1: ClaimStatus.OPEN_FOR_BETTING,
                    2: ClaimStatus.REVEAL_PERIOD,
                    3: ClaimStatus.CHALLENGE_PERIOD,
                    4: ClaimStatus.CLOSED
                }
                status_val = status_map.get(status_raw, ClaimStatus.CLOSED)

                # Extract Deadline from rounds[0].bettingDeadline (Unix Timestamp)
                rounds = data.get("rounds", [])
                round_number = len(rounds) if rounds and isinstance(rounds, list) else 1
                if rounds and isinstance(rounds, list) and len(rounds) > 0:
                    # Use the LATEST round (last in list) for current state
                    latest_round = rounds[-1]
                    first_round = rounds[0]
                    deadline_ts = first_round.get("bettingDeadline")
                    current_deadline_ts = latest_round.get("bettingDeadline")
                    collateral_val = float(latest_round.get("totalAmount", 0))
                    
                    if deadline_ts:
                        deadline_val = datetime.fromtimestamp(int(deadline_ts), timezone.utc)
                    else:
                        deadline_val = datetime.now(timezone.utc)
                    
                    if current_deadline_ts:
                        current_deadline_val = datetime.fromtimestamp(int(current_deadline_ts), timezone.utc)
                    else:
                        current_deadline_val = deadline_val
                else:
                    deadline_val = datetime.now(timezone.utc)
                    current_deadline_val = deadline_val
                    collateral_val = 0.0

                # Extract and Convert createdAt (Unix Timestamp)
                created_at_val = datetime.now(timezone.utc)
                if data.get("createdAt"):
                    try:
                        created_at_val = datetime.fromtimestamp(int(data.get("createdAt")), timezone.utc)
                    except (ValueError, TypeError):
                        pass

                # Extract IDs
                short_id = str(data.get("id"))
                on_chain = str(data.get("roundId") or (rounds[0].get("id") if rounds and isinstance(rounds, list) else data.get("id")))

                # Form the transformed dictionary
                # Extract collectionId if available
                collection_id_val = data.get("collectionId") or data.get("collection_id")
                if collection_id_val is not None:
                    try:
                        collection_id_val = int(collection_id_val)
                    except (ValueError, TypeError):
                        collection_id_val = None

                return {
                    "id": short_id,
                    "on_chain_id": on_chain,
                    "collection_id": collection_id_val,
                    "text": data.get("frontStatement") or data.get("statement") or "No text provided",
                    "status": status_val,
                    "deadline": deadline_val,
                    "current_deadline": current_deadline_val,
                    "round_number": round_number,
                    "collateral": collateral_val,
                    "raw_data": data,
                    # Use parsed createdAt or default
                    "created_at": created_at_val
                }
        return data

class Decision(BaseModel):
    claim_id: str
    on_chain_id: str
    stance: str  # TRUE/FALSE/NO_BET
    confidence: float
    rationale: str
    reasoning: Optional[str] = None # Added for explicit reasoning logging
    timestamp: datetime = Field(default_factory=datetime.utcnow)

class Action(BaseModel):
    tx_hash: str
    claim_id: str
    action_type: str  # BET/CHALLENGE
    amount: float
    timestamp: datetime = Field(default_factory=datetime.utcnow)

# Added PaginatedClaimsResponse to ensure dependencies are met
class PaginatedClaimsResponse(BaseModel):
    items: List[Claim]
    total: int = 0
    page: int = 1
    limit: int = 10
    hasPrevious: bool = False
    hasNext: bool = False
    previous: Optional[str] = None
    next: Optional[str] = None
    
# Keep existing classes not mentioned in prompt
class Operator(str, Enum):
    GREATER_THAN = ">"
    LESS_THAN = "<"
    EQUAL = "=="
    GREATER_THAN_EQ = ">="
    LESS_THAN_EQ = "<="

class Predicate(BaseModel):
    asset: str
    metric: str
    operator: Operator
    target_value: float
    currency: str
    statement: Optional[str] = None # Added for TRUTH_VERIFICATION compatibility
    optimized_search_query: Optional[str] = None # SEO-optimized keyword query for search

class ResearchPlan(BaseModel):
    claim_id: str
    predicates: List[Predicate]
    data_sources: List[str]
    deadline_iso: datetime

class Evidence(BaseModel):
    url: str
    summary: str
    structured_data: Optional[dict] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
