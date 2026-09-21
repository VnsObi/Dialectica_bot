import aiohttp
import asyncio
import os
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential
from src.utils.models import Predicate, Evidence
from src.utils.config import settings

class ResearchTool(ABC):
    @abstractmethod
    async def check(self, predicate: Predicate) -> Optional[Evidence]:
        pass

class CoingeckoTool(ResearchTool):
    def _map_asset_to_id(self, asset: str) -> str:
        # Simple mapping for MVP. In prod, use a comprehensive list or search API.
        if not asset:
            return "bitcoin"
            
        mapping = {
            "BITCOIN": "bitcoin", "BTC": "bitcoin",
            "ETHEREUM": "ethereum", "ETH": "ethereum",
            "SOLANA": "solana", "SOL": "solana",
            "BINANCECOIN": "binancecoin", "BNB": "binancecoin",
            "RIPPLE": "ripple", "XRP": "ripple",
            "CARDANO": "cardano", "ADA": "cardano",
            "AVALANCHE": "avalanche-2", "AVAX": "avalanche-2",
            "DOGECOIN": "dogecoin", "DOGE": "dogecoin",
            "POLKADOT": "polkadot", "DOT": "polkadot",
            "POLYGON": "matic-network", "MATIC": "matic-network",
            "CHAINLINK": "chainlink", "LINK": "chainlink",
            "SHIBA_INU": "shiba-inu", "SHIB": "shiba-inu",
            "LITECOIN": "litecoin", "LTC": "litecoin",
            "SUI": "sui",
            "PEPE": "pepe",
            "APTOS": "aptos", "APT": "aptos",
            "TONCOIN": "the-open-network", "TON": "the-open-network",
            "ALGORAND": "algorand", "ALGO": "algorand",
            "ARBITRUM": "arbitrum", "ARB": "arbitrum",
            "FANTOM": "fantom", "FTM": "fantom",
            "OPTIMISM": "optimism", "OP": "optimism",
            "CELESTIA": "celestia", "TIA": "celestia",
            "COSMOS": "cosmos", "ATOM": "cosmos",
            "INJECTIVE": "injective-protocol", "INJ": "injective-protocol"
        }
        return mapping.get(asset.upper(), asset.lower())

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def check(self, predicate: Predicate) -> Optional[Evidence]:
        if predicate.metric != "PRICE":
            return None
        
        # Mock Mode Interception
        if settings.MOCK_MODE:
            # logger.info(f"MOCK MODE: Returning mock price for {predicate.asset}")
            current_price = 95000.0 if predicate.asset in ["BITCOIN", "BTC"] else 0.0
            return Evidence(
                url="https://api.coingecko.com/api/v3/simple/price?ids=bitcoin",
                summary=f"Current {predicate.asset} price is ${current_price}. Target is ${predicate.target_value}.",
                structured_data={"current_value": current_price}
            )

        # Real API Implementation
        coin_id = self._map_asset_to_id(predicate.asset)
        currency = predicate.currency.lower()
        url = "https://api.coingecko.com/api/v3/simple/price"
        
        # CoinGecko expects 'vs_currencies'
        params = {
            "ids": coin_id,
            "vs_currencies": currency
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params) as response:
                    # 1. Rate Limit Handling (HTTP 429)
                    if response.status == 429:
                        logger.warning(f"CoinGecko Rate Limit Hit (429). Skipping {predicate.asset}.")
                        return None
                    
                    if response.status != 200:
                        logger.error(f"CoinGecko API Error: {response.status}")
                        return None

                    data = await response.json()
                    # e.g. {"bitcoin": {"usd": 96543.21}}

                    if coin_id not in data or currency not in data[coin_id]:
                        logger.warning(f"CoinGecko: Price data not found for {coin_id} in {currency}")
                        return None
                    
                    current_price = float(data[coin_id][currency])
                    
                    return Evidence(
                        url=str(response.url),
                        summary=f"Current {predicate.asset.upper()} price is ${current_price} (Source: CoinGecko).",
                        structured_data={"current_value": current_price}
                    )

        except Exception as e:
            logger.error(f"Error fetching CoinGecko price: {e}")
            return None

class SearchTool(ResearchTool):
    """Calls the Tavily Search API for LLM-optimized search results."""
    TAVILY_API_URL = "https://api.tavily.com/search"

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def check(self, predicate: Predicate) -> Optional[Evidence]:
        if predicate.metric != "TRUTH_VERIFICATION":
            return None

        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            logger.error("TAVILY_API_KEY is missing from environment. Search disabled.")
            return None

        query = predicate.optimized_search_query or f"{predicate.asset} {predicate.statement}"
        logger.info(f"Executing Tavily search: '{query}' for {predicate.asset}")

        payload = {
            "api_key": api_key,
            "query": query,
            "search_depth": "advanced",
            "max_results": 5,
            "include_raw_content": False,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(self.TAVILY_API_URL, json=payload) as response:
                    if response.status != 200:
                        logger.error(f"Tavily API error: HTTP {response.status}")
                        return None

                    data = await response.json()
                    results = data.get("results", [])

                    if not results:
                        logger.warning(f"No Tavily results for: {query}")
                        return None

                    # Normalize to the same shape the Oracle expects
                    raw_snippets = [
                        {
                            "title": r.get("title", "Unknown"),
                            "href": r.get("url", ""),
                            "body": r.get("content", ""),
                        }
                        for r in results
                    ]

                    summary_text = "\n\n".join(
                        f"Source: {s['title']} ({s['href']})\nSnippet: {s['body']}"
                        for s in raw_snippets
                    )

                    return Evidence(
                        url="tavily_search",
                        summary=f"Search Results for '{predicate.statement}':\n{summary_text}",
                        structured_data={"raw_snippets": raw_snippets},
                    )

        except Exception as e:
            logger.error(f"SearchTool (Tavily) error: {e}")
            return None

class CompositeTool(ResearchTool):
    """
    Intelligently routes requests to the correct specialized tool based on the predicate.
    """
    def __init__(self):
        self.coingecko = CoingeckoTool()
        self.search = SearchTool()

    async def check(self, predicate: Predicate) -> Optional[Evidence]:
        if predicate.metric == "PRICE":
            return await self.coingecko.check(predicate)
        elif predicate.metric == "TRUTH_VERIFICATION":
            return await self.search.check(predicate)
        else:
            logger.warning(f"No research tool available for metric: {predicate.metric}")
            return None
