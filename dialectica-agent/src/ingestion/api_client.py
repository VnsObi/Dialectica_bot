import aiohttp
from typing import List, Optional
from datetime import datetime, timedelta
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential

from ..utils.models import Claim, ClaimStatus, PaginatedClaimsResponse
from ..utils.config import settings

class DialecticaClient:
    def __init__(self):
        # Base URL updated via config to: https://dialectica.claims/api
        self.base_url = settings.DIALECTICA_API_URL
        self.mock_mode = settings.MOCK_MODE

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def fetch_claims(self, page: int = 1, limit: int = 10, status: str = "OPEN_FOR_BETTING") -> List[Claim]:
        """
        Fetches active claims from the API.
        Default: Page 1, Limit 10, Status=OPEN_FOR_BETTING.
        """
        if self.mock_mode:
            # logger.info("MOCK MODE: Returning dummy claim.")
            return [
                Claim(
                    id="mock-1",
                    text="Will Bitcoin hit $100k by Dec 31?",
                    status=ClaimStatus(status) if status in ClaimStatus.__members__ else ClaimStatus.OPEN_FOR_BETTING,
                    deadline=datetime.now() + timedelta(days=30),
                    collateral=100.0,
                    raw_data={"mock": True}
                )
            ]
            
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/claims/fetch"
                # Updated params to match Zod validation (status -> filter)
                params = {
                    "page": page,
                    "limit": limit,
                    "filter": "active", # Replaces "status": status
                    "sort": "created_at:desc"
                }
                
                logger.debug(f"Fetching claims from {url} with params {params}")
                
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        try:
                            # Parse using updated schema (items at root, pagination at root)
                            paginated_response = PaginatedClaimsResponse(**data)
                            logger.info(f"Fetched {len(paginated_response.items)} claims (Total: {paginated_response.total})")
                            
                            # Enforce Strict API Parsing: Only keep claims that are OPEN_FOR_BETTING
                            valid_claims = [c for c in paginated_response.items if c.status == ClaimStatus.OPEN_FOR_BETTING]
                            dropped_count = len(paginated_response.items) - len(valid_claims)
                            if dropped_count > 0:
                                logger.info(f"Dropped {dropped_count} claims not OPEN_FOR_BETTING. Kept {len(valid_claims)}")
                            
                            return valid_claims
                        except Exception as e:
                            logger.error(f"Failed to parse API response: {e}")
                            logger.debug("API response could not be parsed; raw response omitted to avoid logging claim data.")
                            return []
                    else:
                        logger.error(f"Failed to fetch claims: HTTP {response.status}")
                        return []
        except Exception as e:
            logger.error(f"Error fetching claims: {e}")
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def fetch_succeeded_claims(self, limit: int = 50) -> List[Claim]:
        """Fetches settled/resolved claims."""
        if self.mock_mode: return []
        
        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/claims/fetch"
                params = {
                    "page": 1,
                    "limit": limit,
                    "filter": "settled",  # or 'resolved', 'history' - based on user input
                    "sort": "created_at:desc"
                }
                logger.debug(f"Fetching settled claims from {url}")
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        paginated_response = PaginatedClaimsResponse(**data)
                        return paginated_response.items
                    return []
        except Exception as e:
            logger.error(f"Error fetching settled claims: {e}")
            return []

    async def fetch_claim(self, claim_id: str) -> Optional[Claim]:
        """Fetches a single claim by ID."""
        if self.mock_mode:
            return Claim(
                id=claim_id,
                text="Will Bitcoin hit $100k by Dec 31?",
                status=ClaimStatus.CHALLENGE_PERIOD, # Default mock status for testing
                deadline=datetime.now() + timedelta(days=30),
                collateral=100.0,
                raw_data={"mock": True}
            )

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/claims/fetch-one?id={claim_id}"
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        claim_data = data.get("claim", data) # Safely unwrap if it returns {"claim": {...}}
                        return Claim(**claim_data)
                    return None
        except Exception as e:
            logger.error(f"Error fetching claim {claim_id}: {e}")
            return None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def fetch_challenge_claims(self, limit: int = 50) -> List[Claim]:
        """Fetches claims currently in CHALLENGE_PERIOD."""
        if self.mock_mode:
            return []

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/claims/fetch"
                params = {
                    "page": 1,
                    "limit": limit,
                    "filter": "challenge",
                    "sort": "created_at:desc"
                }
                logger.debug(f"Fetching challenge-period claims from {url}")
                async with session.get(url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()
                        paginated_response = PaginatedClaimsResponse(**data)
                        logger.info(f"Fetched {len(paginated_response.items)} challenge-period claims")
                        return paginated_response.items
                    return []
        except Exception as e:
            logger.error(f"Error fetching challenge claims: {e}")
            return []
