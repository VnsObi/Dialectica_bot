from enum import Enum
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

class Operator(str, Enum):
    GREATER_THAN = ">"
    LESS_THAN = "<"
    GREATER_THAN_EQ = ">="
    LESS_THAN_EQ = "<="
    EQUAL = "=="

class Predicate(BaseModel):
    asset: str
    metric: str
    operator: Operator = Field(..., description="Comparison operator")
    target_value: float
    currency: str
    statement: Optional[str] = None  # New field for factual verification
    optimized_search_query: Optional[str] = None  # SEO-optimized keyword query for search

class Plan(BaseModel):
    claim_id: str
    predicates: List[Predicate]
    data_sources: List[str]
    deadline_iso: Optional[datetime]
