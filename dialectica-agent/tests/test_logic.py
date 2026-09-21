from datetime import datetime, timedelta

from src.decision.logic import DecisionEngine
from src.utils.models import Claim, Evidence, Operator, Predicate, ResearchPlan


def test_decision_engine_returns_no_bet_without_evidence(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engine = DecisionEngine()
    claim = Claim(
        id="1",
        on_chain_id="1",
        text="Will the test condition be true?",
        created_at=datetime.utcnow(),
        deadline=datetime.utcnow() + timedelta(days=2),
        status="OPEN_FOR_BETTING",
    )
    predicate = Predicate(
        asset="TEST",
        metric="PRICE",
        operator=Operator.GREATER_THAN,
        target_value=1.0,
        currency="USD",
    )
    plan = ResearchPlan(
        claim_id=claim.id,
        predicates=[predicate],
        data_sources=["test"],
        deadline_iso=claim.deadline,
    )

    decision = engine.evaluate(claim, plan, [])

    assert decision.stance == "NO_BET"
    assert decision.confidence == 0.0


def test_price_predicate_returns_true_when_target_is_met(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engine = DecisionEngine()
    claim = Claim(
        id="2",
        on_chain_id="2",
        text="Test price claim",
        created_at=datetime.utcnow(),
        deadline=datetime.utcnow() + timedelta(days=2),
        status="OPEN_FOR_BETTING",
    )
    predicate = Predicate(
        asset="TEST",
        metric="PRICE",
        operator=Operator.GREATER_THAN_EQ,
        target_value=10.0,
        currency="USD",
    )
    plan = ResearchPlan(
        claim_id=claim.id,
        predicates=[predicate],
        data_sources=["test"],
        deadline_iso=claim.deadline,
    )
    evidence = Evidence(
        url="https://example.invalid/test",
        summary="Test-only evidence",
        structured_data={"current_value": 10.0},
    )

    decision = engine.evaluate(claim, plan, [evidence])

    assert decision.stance == "TRUE"
    assert decision.confidence == 1.0
