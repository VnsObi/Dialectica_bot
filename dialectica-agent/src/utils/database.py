from datetime import datetime
from sqlalchemy import create_engine, Column, String, Float, DateTime, Integer, ForeignKey, Text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

Base = declarative_base()

class ClaimModel(Base):
    __tablename__ = 'claims'
    id = Column(String, primary_key=True)
    on_chain_id = Column(String, nullable=True) # Added for Dual-ID
    text = Column(Text, nullable=False)
    status = Column(String, nullable=False)
    current_status = Column(String, nullable=True)  # Latest API status (lifecycle tracking)
    round_id = Column(Integer, default=1)  # Current round number from API
    deadline = Column(DateTime, nullable=False)
    current_deadline = Column(DateTime, nullable=True)  # Latest round deadline
    created_at = Column(DateTime, default=datetime.utcnow)
    market_volume = Column(Float, default=0.0) # Tracking total betting volume
    decisions = relationship("DecisionModel", back_populates="claim")

class DecisionModel(Base):
    __tablename__ = 'decisions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    claim_id = Column(String, ForeignKey('claims.id'), nullable=False)
    stance = Column(String, nullable=False)
    confidence = Column(Float, nullable=False)
    rationale = Column(Text, nullable=False)  # Audit log
    timestamp = Column(DateTime, default=datetime.utcnow)
    claim = relationship("ClaimModel", back_populates="decisions")

class ActionModel(Base):
    __tablename__ = 'actions'
    tx_hash = Column(String, primary_key=True)
    claim_id = Column(String, ForeignKey('claims.id'), nullable=False)
    action_type = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    error_name = Column(String, nullable=True)  # Populated on failed TXs
    timestamp = Column(DateTime, default=datetime.utcnow)


class FailedTxModel(Base):
    """Audit table for every failed on-chain transaction."""
    __tablename__ = 'failed_txs'
    id = Column(Integer, primary_key=True, autoincrement=True)
    claim_id = Column(String, nullable=False)
    action_type = Column(String, nullable=False)      # VOTE, CHALLENGE, PAYOUT, etc.
    error_name = Column(String, nullable=False)        # NoBetting, InsufficientBalance, etc.
    error_detail = Column(Text, nullable=True)         # Full error message / params
    raw_revert_hex = Column(Text, nullable=True)       # Hex-encoded revert data
    timestamp = Column(DateTime, default=datetime.utcnow)


def init_db(db_url: str):
    if db_url.startswith("postgresql"):
        # For Postgres, enable connection pooling for concurrency
        engine = create_engine(db_url, pool_size=10, max_overflow=20)
    else:
        # SQLite
        connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        engine = create_engine(db_url, connect_args=connect_args)
        
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
