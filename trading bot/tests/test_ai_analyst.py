import json
from unittest.mock import MagicMock
from bot.ai_analyst import (
    EntryScore, ExitDecision,
    parse_entry_response, parse_exit_response,
    score_entry, review_exit,
)

def _mock_claude(mocker, text: str):
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

def test_parse_entry_buy():
    raw = json.dumps({"conviction": 7, "position_pct": 4.5,
                      "rationale": "Strong", "entry": "buy", "risk_flags": ["lag"]})
    s = parse_entry_response(raw)
    assert s.conviction == 7
    assert s.position_pct == 4.5
    assert s.entry == "buy"
    assert s.risk_flags == ("lag",)

def test_parse_entry_skip():
    raw = json.dumps({"conviction": 2, "position_pct": 0,
                      "rationale": "Weak", "entry": "skip", "risk_flags": []})
    assert parse_entry_response(raw).entry == "skip"

def test_score_entry_returns_entry_score(mocker):
    payload = json.dumps({"conviction": 8, "position_pct": 5.0,
                          "rationale": "Good", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(disc, committees=["House Energy and Commerce"],
                         sector="Energy", lag_days=2, estimated_cost_pct=0.05)
    assert isinstance(result, EntryScore)
    assert result.conviction == 8

def test_parse_exit_hold():
    raw = json.dumps({"action": "hold", "rationale": "Momentum ok"})
    d = parse_exit_response(raw)
    assert d.action == "hold"

def test_review_exit_returns_exit_decision(mocker):
    payload = json.dumps({"action": "exit", "rationale": "Stop loss near"})
    _mock_claude(mocker, payload)
    result = review_exit("AAPL", 150.0, 125.0, 20)
    assert isinstance(result, ExitDecision)
    assert result.action == "exit"


from bot.researcher import ResearchReport


def _make_research(**overrides) -> ResearchReport:
    defaults = dict(
        ticker="XOM", company_name="Exxon Mobil", sector="Energy",
        market_cap=5e11, pe_trailing=12.0, pe_forward=10.0, pb_ratio=2.0,
        ps_ratio=1.5, peg_ratio=1.2, ev_ebitda=8.0,
        roe=0.15, roa=0.08, profit_margin=0.10, debt_to_equity=0.3,
        current_ratio=1.2, free_cash_flow=2e10, revenue_growth=0.05,
        earnings_growth=0.08, beta=0.9, week52_high=120.0, week52_low=85.0,
        momentum_1m=2.0, momentum_3m=8.0, analyst_target=115.0,
        analyst_rating="Buy", headlines=("Dividend raised",),
    )
    defaults.update(overrides)
    return ResearchReport(**defaults)


def test_score_entry_with_research_injects_research_block(mocker):
    payload = json.dumps({"conviction": 8, "position_pct": 5.0,
                          "rationale": "Good", "entry": "buy", "risk_flags": []})
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}

    score_entry(disc, committees=["House Energy and Commerce"],
                sector="Energy", lag_days=2, estimated_cost_pct=0.05,
                research=_make_research())

    call_kwargs = mock_client.messages.create.call_args[1]
    user_content = call_kwargs["messages"][0]["content"]
    assert "INDEPENDENT RESEARCH" in user_content
    assert "Exxon Mobil" in user_content
    assert "Dividend raised" in user_content


def test_score_entry_without_research_omits_research_block(mocker):
    payload = json.dumps({"conviction": 7, "position_pct": 4.0,
                          "rationale": "Ok", "entry": "buy", "risk_flags": []})
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}

    score_entry(disc, committees=["House Energy and Commerce"],
                sector="Energy", lag_days=2, estimated_cost_pct=0.05)

    call_kwargs = mock_client.messages.create.call_args[1]
    user_content = call_kwargs["messages"][0]["content"]
    assert "INDEPENDENT RESEARCH" not in user_content


def test_review_exit_with_research_injects_research_block(mocker):
    payload = json.dumps({"action": "hold", "rationale": "Fundamentals strong"})
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    review_exit("AAPL", 150.0, 160.0, 10, research=_make_research(
        ticker="AAPL", company_name="Apple Inc.", sector="Technology",
        headlines=("Record quarter",),
    ))

    call_kwargs = mock_client.messages.create.call_args[1]
    user_content = call_kwargs["messages"][0]["content"]
    assert "INDEPENDENT RESEARCH" in user_content
    assert "Record quarter" in user_content


def test_review_exit_without_research_still_works(mocker):
    payload = json.dumps({"action": "exit", "rationale": "Stop loss"})
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=payload)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    result = review_exit("AAPL", 150.0, 125.0, 20)
    assert result.action == "exit"
