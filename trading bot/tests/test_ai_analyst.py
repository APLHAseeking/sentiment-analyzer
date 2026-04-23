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
    result = review_exit("AAPL", 150.0, 125.0, 20, ["Bad news"])
    assert isinstance(result, ExitDecision)
    assert result.action == "exit"
