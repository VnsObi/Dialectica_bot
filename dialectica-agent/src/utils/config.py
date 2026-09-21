import os
import yaml
from dotenv import load_dotenv

load_dotenv()

class Config:
    def __init__(self):
        self.settings = self._load_yaml()
        self.PRIVATE_KEY = os.getenv("PRIVATE_KEY")
        self.RPC_URL = os.getenv("RPC_URL")
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        self.DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/agent.db")
        self.DIALECTICA_API_URL = os.getenv("DIALECTICA_API_URL", "")
        self.DIALECTICA_PUBLIC_KEY = os.getenv("DIALECTICA_PUBLIC_KEY", "")
        
        # Chain Constants
        # Use RPC_URL first (common), then BASE_RPC_URL. No public fallback.
        self.BASE_RPC_URL = os.getenv("RPC_URL") or os.getenv("BASE_RPC_URL") or ""
        
        # Smart Contract Addresses (Strictly Required for Production)
        self.USDC_ADDRESS = os.getenv("USDC_ADDRESS")
        self.TREASURY_ADDRESS = os.getenv("TREASURY_ADDRESS")
        self.CLAIMS_ADDRESS = os.getenv("CLAIMS_ADDRESS")
        self.COLLECTIONS_ADDRESS = os.getenv("COLLECTIONS_ADDRESS")
        self.ROUNDS_ADDRESS = os.getenv("ROUNDS_ADDRESS")
        
        # Betting Configuration
        self.BET_AMOUNT_USDC = float(os.getenv("BET_AMOUNT_USDC", self.settings.get("bet_amount_usdc", 10.0)))
        
        # Treasury Configuration (lowered from 2.0 → 0.5 to prevent bot hanging
        # on "Insufficient Wallet USDC for Treasury Deposit" when balance is thin)
        self.INITIAL_DEPOSIT_USDC = float(os.getenv("INITIAL_DEPOSIT_USDC", self.settings.get("initial_deposit_usdc", 0.5)))

        # YAML overrides
        # Force MOCK_MODE check from env first, or default to False to force production
        mock_env = os.getenv("MOCK_MODE")
        if mock_env is not None:
            self.MOCK_MODE = mock_env.lower() == "true"
        else:
            self.MOCK_MODE = self.settings.get("mock_mode", False)
        
        # Fail-Safe: Crash if critical addresses are missing in Production
        if not self.MOCK_MODE:
            missing = []
            if not self.PRIVATE_KEY: missing.append("PRIVATE_KEY")
            if not self.BASE_RPC_URL: missing.append("BASE_RPC_URL")
            if not self.DIALECTICA_API_URL: missing.append("DIALECTICA_API_URL")
            if not self.USDC_ADDRESS: missing.append("USDC_ADDRESS")
            if not self.TREASURY_ADDRESS: missing.append("TREASURY_ADDRESS")
            if not self.CLAIMS_ADDRESS: missing.append("CLAIMS_ADDRESS")
            if not self.COLLECTIONS_ADDRESS: missing.append("COLLECTIONS_ADDRESS")
            if not self.ROUNDS_ADDRESS: missing.append("ROUNDS_ADDRESS")
            
            if missing:
                raise ValueError(f"CRITICAL: Missing required environment variables for Mainnet: {', '.join(missing)}")
        
        self.MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", self.settings.get("min_confidence", 0.75)))
        self.POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", self.settings.get("poll_interval", 60)))

    def _load_yaml(self):
        try:
            # Try loading relative to this file
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            config_path = os.path.join(base_dir, "config", "settings.yaml")
            with open(config_path, "r") as f:
                return yaml.safe_load(f) or {}
        except FileNotFoundError:
            # Fallback to existing logic if needed
            try:
                with open("dialectica-agent/config/settings.yaml", "r") as f:
                    return yaml.safe_load(f) or {}
            except FileNotFoundError:
                return {}

settings = Config()
