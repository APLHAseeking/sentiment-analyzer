import json
import pytest
from unittest.mock import MagicMock
from bot.ai_analyst import (
    EntryScore, ExitDecision,
    parse_entry_response, parse_exit_response,
    score_entry, review_exit,
    _build_entry_system,
)

# ---------------------------------------------------------------------------
# Anthropic mock helpers
# ---------------------------------------------------------------------------

def _make_anthropic_resp(text: str):
    """Build a mock that mimics anthropic.Message with .content[0].text."""
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    return resp


def _mock_claude(mocker, text: str):
    """Patch _get_client() so messages.create() returns a mock Anthropic response."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_anthropic_resp(text)
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)


def _get_prompt(mocker) -> str:
    """Retrieve the user message content from the last messages.create call."""
    import bot.ai_analyst as m
    call_kwargs = m._get_client().messages.create.call_args[1]
    return call_kwargs["messages"][0]["content"]


# ---------------------------------------------------------------------------
# parse_ unit tests (no mocking needed)
# ---------------------------------------------------------------------------

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
        momentum_1m=2.0, momentum_3m=8.0,
        short_interest_pct=1.5, avg_daily_volume_usd=500_000_000,
        analyst_target=115.0, analyst_rating="Buy", num_analysts=20,
        headlines=("Dividend raised",),
    )
    defaults.update(overrides)
    return ResearchReport(**defaults)


def test_score_entry_with_research_injects_research_block(mocker):
    payload = json.dumps({"conviction": 8, "position_pct": 5.0,
                          "rationale": "Good", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)

    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}

    score_entry(disc, committees=["House Energy and Commerce"],
                sector="Energy", lag_days=2, estimated_cost_pct=0.05,
                research=_make_research())

    user_content = _get_prompt(mocker)
    assert "INDEPENDENT RESEARCH" in user_content
    assert "Exxon Mobil" in user_content
    assert "Dividend raised" in user_content


def test_score_entry_without_research_omits_research_block(mocker):
    payload = json.dumps({"conviction": 7, "position_pct": 4.0,
                          "rationale": "Ok", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)

    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}

    score_entry(disc, committees=["House Energy and Commerce"],
                sector="Energy", lag_days=2, estimated_cost_pct=0.05)

    user_content = _get_prompt(mocker)
    assert "INDEPENDENT RESEARCH" not in user_content


def test_review_exit_with_research_injects_research_block(mocker):
    payload = json.dumps({"action": "hold", "rationale": "Fundamentals strong"})
    _mock_claude(mocker, payload)

    review_exit("AAPL", 150.0, 160.0, 10, research=_make_research(
        ticker="AAPL", company_name="Apple Inc.", sector="Technology",
        headlines=("Record quarter",),
    ))

    user_content = _get_prompt(mocker)
    assert "INDEPENDENT RESEARCH" in user_content
    assert "Record quarter" in user_content


def test_review_exit_without_research_still_works(mocker):
    payload = json.dumps({"action": "exit", "rationale": "Stop loss"})
    _mock_claude(mocker, payload)

    result = review_exit("AAPL", 150.0, 125.0, 20)
    assert result.action == "exit"


def test_score_entry_includes_cluster_count_in_prompt(mocker):
    payload = json.dumps({"conviction": 9, "position_pct": 6.0,
                          "rationale": "Strong cluster", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    disc = {"id": "x1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    score_entry(disc, committees=["House Energy and Commerce"],
                sector="Energy", lag_days=2, estimated_cost_pct=0.05,
                cluster_count=4)
    prompt_text = _get_prompt(mocker)
    assert "Cluster count" in prompt_text
    assert "4" in prompt_text


def test_score_entry_cluster_count_defaults_to_1(mocker):
    payload = json.dumps({"conviction": 7, "position_pct": 4.0,
                          "rationale": "Ok", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    disc = {"id": "x2", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(disc, committees=["House Energy and Commerce"],
                         sector="Energy", lag_days=2, estimated_cost_pct=0.05)
    assert isinstance(result, EntryScore)
    prompt_text = _get_prompt(mocker)
    assert "Cluster count (other members buying same stock last 30d): 1" in prompt_text


def test_parse_entry_invalid_conviction_raises():
    raw = json.dumps({"conviction": 11, "position_pct": 5.0,
                      "rationale": "Bad", "entry": "buy", "risk_flags": []})
    with pytest.raises(ValueError, match="conviction"):
        parse_entry_response(raw)


def test_review_exit_has_no_headlines_param():
    import inspect
    sig = inspect.signature(review_exit)
    assert "headlines" not in sig.parameters
    assert "research" in sig.parameters


def test_parse_exit_invalid_action_raises():
    raw = json.dumps({"action": "yolo", "rationale": "Bad"})
    with pytest.raises(ValueError, match="action"):
        parse_exit_response(raw)


def test_parse_exit_malformed_json_raises():
    with pytest.raises(ValueError, match="invalid JSON"):
        parse_exit_response("not json")


def test_score_entry_fundamental_omits_congressional_fields(mocker):
    payload = json.dumps({"conviction": 7, "position_pct": 4.0,
                          "rationale": "Good fundamentals", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    result = score_entry(
        disclosure=None, committees=[], sector="Technology",
        lag_days=0, estimated_cost_pct=0.05,
        signal_type="fundamental", factor_score=82, ticker="MSFT",
    )
    prompt = _get_prompt(mocker)
    assert "Politician" not in prompt
    assert "Committees" not in prompt
    assert "factor score" in prompt.lower()
    assert "82" in prompt
    assert isinstance(result, EntryScore)


def test_score_entry_both_includes_fundamental_and_congressional_fields(mocker):
    payload = json.dumps({"conviction": 9, "position_pct": 6.0,
                          "rationale": "Strong both", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    disc = {"id": "b1", "politician": "Jane Doe", "ticker": "AAPL",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(
        disclosure=disc, committees=["House Energy"],
        sector="Technology", lag_days=2, estimated_cost_pct=0.05,
        signal_type="both", factor_score=78, cluster_count=2,
    )
    prompt = _get_prompt(mocker)
    assert "Politician" in prompt
    assert "factor score" in prompt.lower()
    assert "78" in prompt
    assert isinstance(result, EntryScore)


def test_score_entry_congressional_default_unchanged(mocker):
    payload = json.dumps({"conviction": 8, "position_pct": 5.0,
                          "rationale": "Good", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    disc = {"id": "c1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(disc, committees=["House Energy"],
                         sector="Energy", lag_days=2, estimated_cost_pct=0.05)
    prompt = _get_prompt(mocker)
    assert "Politician" in prompt
    assert "factor score" not in prompt.lower()
    assert isinstance(result, EntryScore)


def test_score_entry_both_no_disclosure_omits_congressional_fields(mocker):
    payload = json.dumps({"conviction": 8, "position_pct": 5.0,
                          "rationale": "Both signal", "entry": "buy", "risk_flags": []})
    _mock_claude(mocker, payload)
    result = score_entry(
        disclosure=None, committees=[], sector="Technology",
        lag_days=0, estimated_cost_pct=0.05,
        signal_type="both", factor_score=75, ticker="AAPL",
    )
    prompt = _get_prompt(mocker)
    assert "Politician" not in prompt
    assert "factor score" in prompt.lower()
    assert isinstance(result, EntryScore)


def test_build_entry_system_congressional_includes_lag_rules():
    system = _build_entry_system("congressional", has_disclosure=True)
    assert "Lag Decay" in system
    assert "Fundamental Factor Score Rules" not in system


def test_build_entry_system_fundamental_excludes_congressional_rules():
    system = _build_entry_system("fundamental", has_disclosure=False)
    assert "Lag Decay" not in system
    assert "Fundamental Factor Score Rules" in system


def test_build_entry_system_both_no_disclosure_excludes_lag_rules():
    system = _build_entry_system("both", has_disclosure=False)
    assert "Lag Decay" not in system
    assert "Fundamental Factor Score Rules" in system
    assert "conviction bonus" in system.lower()


def test_build_entry_system_invalid_type_raises():
    with pytest.raises(ValueError, match="signal_type"):
        _build_entry_system("unknown")


from bot.ai_analyst import score_entry_with_debate, _bull_argument, _bear_argument, _call_with_retry


def _low_conviction_payload():
    return json.dumps({
        "conviction": 5, "position_pct": 1.5,
        "rationale": "Weak signal", "entry": "skip", "risk_flags": [],
    })


def _high_conviction_payload():
    return json.dumps({
        "conviction": 8, "position_pct": 5.0,
        "rationale": "Strong signal", "entry": "buy", "risk_flags": [],
    })


def _disc():
    return {
        "id": "d1", "politician": "Jane Doe", "ticker": "MSFT",
        "transaction_date": "2026-04-20", "disclosure_date": "2026-04-22",
        "amount_range": "$50,001 - $100,000",
    }


def test_debate_makes_one_call_when_conviction_below_7(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_anthropic_resp(_low_conviction_payload())
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    result = score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )

    assert mock_client.messages.create.call_count == 1
    assert isinstance(result, EntryScore)
    assert result.conviction == 5


def test_debate_makes_four_calls_when_conviction_gte_7(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _make_anthropic_resp(_high_conviction_payload()),   # call 1: initial score
        _make_anthropic_resp("Strong revenue growth, market leadership..."),  # call 2: bull
        _make_anthropic_resp("High valuation, margin pressure risk..."),      # call 3: bear
        _make_anthropic_resp(_high_conviction_payload()),   # call 4: final score
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    result = score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )

    assert mock_client.messages.create.call_count == 4
    assert isinstance(result, EntryScore)


def test_debate_call4_prompt_includes_debate_block(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _make_anthropic_resp(_high_conviction_payload()),
        _make_anthropic_resp("Bull: Strong fundamentals"),
        _make_anthropic_resp("Bear: Elevated risk"),
        _make_anthropic_resp(_high_conviction_payload()),
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )

    call4_content = mock_client.messages.create.call_args_list[3][1]["messages"][0]["content"]
    assert "DEBATE" in call4_content
    assert "Bull case" in call4_content
    assert "Bear case" in call4_content


def test_debate_returns_entry_score_type(mocker):
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _make_anthropic_resp(_high_conviction_payload()),
        _make_anthropic_resp("Bull: margins expanding, market share gains"),
        _make_anthropic_resp("Bear: valuation stretched, macro headwinds"),
        _make_anthropic_resp(_high_conviction_payload()),
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)
    result = score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )
    assert isinstance(result, EntryScore)
    assert result.entry == "buy"


def test_score_entry_retries_on_rate_limit(mocker):
    """RateLimitError on first two calls; third call succeeds."""
    import anthropic as _anthropic

    good_payload = json.dumps({
        "conviction": 7, "position_pct": 4.0,
        "rationale": "Retry worked", "entry": "buy", "risk_flags": [],
    })

    rate_err = _anthropic.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={}),
        body={},
    )

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        rate_err, rate_err, _make_anthropic_resp(good_payload)
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)
    mocker.patch("bot.ai_analyst.time.sleep")

    disc = {"id": "r1", "politician": "Jane Doe", "ticker": "XOM",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(disc, committees=["House Energy"],
                         sector="Energy", lag_days=2, estimated_cost_pct=0.05)

    assert isinstance(result, EntryScore)
    assert result.conviction == 7
    assert mock_client.messages.create.call_count == 3


def test_score_entry_retries_on_parse_error(mocker):
    """Bad JSON on first two calls; valid JSON on third call."""
    good_payload = json.dumps({
        "conviction": 6, "position_pct": 1.5,
        "rationale": "Parse retry worked", "entry": "skip", "risk_flags": [],
    })

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _make_anthropic_resp("```json\nnot valid json\n```"),
        _make_anthropic_resp("```json\nnot valid json\n```"),
        _make_anthropic_resp(good_payload),
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    disc = {"id": "r2", "politician": "Jane Doe", "ticker": "AAPL",
            "transaction_date": "2026-04-10", "disclosure_date": "2026-04-12",
            "amount_range": "$50,001 - $100,000"}
    result = score_entry(disc, committees=["House Energy"],
                         sector="Technology", lag_days=2, estimated_cost_pct=0.05)

    assert isinstance(result, EntryScore)
    assert result.conviction == 6
    assert mock_client.messages.create.call_count == 3


def test_bull_bear_use_claude_sonnet(mocker):
    """_bull_argument and _bear_argument must use claude-sonnet-4-6."""
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _make_anthropic_resp("Some argument text")
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    _bull_argument("test prompt")
    bull_model = mock_client.messages.create.call_args[1]["model"]
    assert bull_model == "claude-sonnet-4-6", f"Bull used {bull_model!r}"

    mock_client.messages.create.reset_mock()
    mock_client.messages.create.return_value = _make_anthropic_resp("Bear argument text")

    _bear_argument("test prompt", "bull text")
    bear_model = mock_client.messages.create.call_args[1]["model"]
    assert bear_model == "claude-sonnet-4-6", f"Bear used {bear_model!r}"
