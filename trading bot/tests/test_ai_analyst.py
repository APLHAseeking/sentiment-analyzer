import json
import pytest
import openai as _openai
from unittest.mock import MagicMock
from bot.ai_analyst import (
    EntryScore, ExitDecision,
    parse_entry_response, parse_exit_response,
    score_entry, review_exit,
    _build_entry_system,
)

import dataclasses
from system.config import settings as _real_settings


@pytest.fixture(autouse=True)
def _force_anthropic_provider(mocker):
    """This file's tests assert Anthropic-specific behavior (model strings, error
    types, cache_control). Force the provider regardless of Settings' real default
    so they keep testing what they've always tested. OpenAI-path tests in this file
    explicitly re-patch to "openai" inside their own body, which overrides this."""
    mocker.patch("system.config.settings", dataclasses.replace(_real_settings, llm_provider="anthropic"))


# ---------------------------------------------------------------------------
# Anthropic mock helpers
# ---------------------------------------------------------------------------

def _make_anthropic_resp(text: str):
    """Build a mock that mimics anthropic.Message with .content[0].text."""
    resp = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp.content = [block]
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

def test_parse_entry_reads_expected_return_pct():
    raw = json.dumps({"conviction": 7, "position_pct": 4.5, "rationale": "Strong",
                      "entry": "buy", "risk_flags": [], "expected_return_pct": 4.2})
    assert parse_entry_response(raw).expected_return_pct == pytest.approx(4.2)

def test_parse_entry_defaults_expected_return_pct_when_missing():
    """Older/malformed responses without the field must not fail parsing —
    it's observability-only, not decision-critical (the model already
    applied the entry hurdle itself when setting `entry`)."""
    raw = json.dumps({"conviction": 7, "position_pct": 4.5,
                      "rationale": "Strong", "entry": "buy", "risk_flags": []})
    assert parse_entry_response(raw).expected_return_pct == 0.0

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


def test_debate_bear_call_uses_other_provider_when_cross_model_enabled(mocker):
    """enable_cross_model_debate=True routes only the bear argument to the other
    provider (Anthropic primary -> OpenAI bear); initial/bull/final stay primary."""
    cross_settings = dataclasses.replace(
        _real_settings, llm_provider="anthropic",
        sizing=dataclasses.replace(_real_settings.sizing, enable_cross_model_debate=True),
    )
    mocker.patch("system.config.settings", cross_settings)

    anthropic_client = MagicMock()
    anthropic_client.messages.create.side_effect = [
        _make_anthropic_resp(_high_conviction_payload()),   # call 1: initial score
        _make_anthropic_resp("Bull: strong fundamentals"),  # call 2: bull
        _make_anthropic_resp(_high_conviction_payload()),   # call 4: final score
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=anthropic_client)

    openai_client = MagicMock()
    openai_client.chat.completions.create.return_value = _make_openai_resp(
        "Bear: overvalued, macro headwinds"
    )
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=openai_client)

    result = score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )

    assert anthropic_client.messages.create.call_count == 3  # initial, bull, final
    assert openai_client.chat.completions.create.call_count == 1  # bear only
    assert isinstance(result, EntryScore)


def test_debate_bear_call_stays_same_provider_when_cross_model_disabled(mocker):
    """Default (enable_cross_model_debate=False): byte-identical to before this
    feature existed — all 4 calls stay on the primary provider."""
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [
        _make_anthropic_resp(_high_conviction_payload()),
        _make_anthropic_resp("Bull: strong fundamentals"),
        _make_anthropic_resp("Bear: overvalued, macro headwinds"),
        _make_anthropic_resp(_high_conviction_payload()),
    ]
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)
    openai_client = MagicMock()
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=openai_client)

    score_entry_with_debate(
        _disc(), committees=["House Energy"], sector="Technology",
        lag_days=2, estimated_cost_pct=0.05,
    )

    assert mock_client.messages.create.call_count == 4
    openai_client.chat.completions.create.assert_not_called()


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


from bot.ai_analyst import TechnicalScore, parse_technical_response


def _technical_payload(**overrides):
    base = {
        "conviction": 8, "entry": "buy", "setup_type": "pullback_to_support",
        "trend_alignment": "bullish", "momentum_state": "rsi_rising",
        "volume_confirmation": "confirmed", "relative_strength": "outperforming",
        "entry_trigger": "reclaim of 20-day SMA", "invalidation_price": 95.0,
        "target_price": 115.0, "reward_risk": 3.0,
        "key_levels": "support 95, resistance 115",
        "conflicts": [], "rationale": "Clean setup", "risk_flags": [],
    }
    base.update(overrides)
    return json.dumps(base)


class TestParseTechnicalResponse:
    def test_valid_buy_passes_through(self):
        # last_close=100: reward_risk = (115-100)/(100-95) = 3.0, matches reported 3.0
        score = parse_technical_response(_technical_payload(), last_close=100.0)
        assert isinstance(score, TechnicalScore)
        assert score.entry == "buy"
        assert score.reward_risk == pytest.approx(3.0)
        assert score.conviction == 8

    def test_invalidation_above_last_close_downgrades_to_skip(self):
        payload = _technical_payload(invalidation_price=105.0)  # >= last_close=100
        score = parse_technical_response(payload, last_close=100.0)
        assert score.entry == "skip"
        assert score.reward_risk == 0.0

    def test_target_below_last_close_downgrades_to_skip(self):
        payload = _technical_payload(target_price=95.0)  # <= last_close=100
        score = parse_technical_response(payload, last_close=100.0)
        assert score.entry == "skip"
        assert score.reward_risk == 0.0

    def test_reward_risk_mismatch_downgrades_to_skip(self):
        # Geometry is valid (recomputed reward_risk=3.0) but model self-reports 10.0
        payload = _technical_payload(reward_risk=10.0)
        score = parse_technical_response(payload, last_close=100.0)
        assert score.entry == "skip"
        assert score.reward_risk == 0.0

    def test_skip_entry_is_not_escalated(self):
        payload = _technical_payload(entry="skip")
        score = parse_technical_response(payload, last_close=100.0)
        assert score.entry == "skip"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            parse_technical_response("not json", last_close=100.0)


from bot.ai_analyst import score_technical
from technical.indicators import TechnicalSnapshot


def _make_snapshot(**overrides) -> TechnicalSnapshot:
    base = dict(
        ticker="AAPL", as_of="2026-06-17", last_close=100.0,
        htf_trend="up", htf_above_200d=True,
        dist_to_52w_high_pct=-5.0, dist_to_52w_low_pct=40.0,
        sma20=99.0, sma50=95.0, sma200=90.0, ma_alignment="bullish",
        sma200_slope_pct_20d=2.0, price_vs_sma20_pct=1.0, price_vs_sma50_pct=5.0,
        market_structure="HH_HL",
        ret_1m_pct=3.0, ret_3m_pct=8.0, ret_6m_pct=15.0, ret_12m_1m_pct=20.0,
        tsmom_composite=0.3,
        rsi14=60.0, rsi_regime="neutral", rsi_divergence="none",
        macd_hist=0.5, macd_state="bullish_expanding",
        atr_pct=2.0, atr_pct_percentile_1y=50.0,
        bb_percent_b=0.7, bb_bandwidth_percentile_1y=40.0,
        rel_volume_20d=1.2, obv_trend="rising", volume_confirms_move=True,
        rs_vs_spy_3m_pct=2.0, rs_vs_spy_6m_pct=5.0, rs_vs_sector_3m_pct=1.0,
        rs_line_slope="rising",
        nearest_support=95.0, nearest_resistance=110.0,
        dist_to_support_pct=5.0, dist_to_resistance_pct=10.0,
        fib_levels={"38.2": 97.0, "50.0": 95.0, "61.8": 93.0},
        anchored_vwap_from_low=96.0,
        bars_available=300, data_complete=True,
    )
    base.update(overrides)
    return TechnicalSnapshot(**base)


def test_score_technical_returns_technical_score(mocker):
    _mock_claude(mocker, _technical_payload())
    result = score_technical(_make_snapshot(), regime_label="bull", signal_type="fundamental")
    assert isinstance(result, TechnicalScore)
    assert result.entry == "buy"


def test_score_technical_both_includes_bonus_text_in_system_prompt(mocker):
    _mock_claude(mocker, _technical_payload())
    score_technical(_make_snapshot(), regime_label="bull", signal_type="both")
    import bot.ai_analyst as m
    system_text = m._get_client().messages.create.call_args[1]["system"][0]["text"]
    assert "Combined Signal Note" in system_text


def test_score_technical_congressional_omits_bonus_text(mocker):
    _mock_claude(mocker, _technical_payload())
    score_technical(_make_snapshot(), regime_label="bull", signal_type="congressional")
    import bot.ai_analyst as m
    system_text = m._get_client().messages.create.call_args[1]["system"][0]["text"]
    assert "Combined Signal Note" not in system_text


def test_get_openai_client_raises_without_key(mocker):
    import dataclasses
    from system.config import settings as real_settings
    bad_settings = dataclasses.replace(
        real_settings,
        credentials=dataclasses.replace(real_settings.credentials, openai_api_key=""),
    )
    mocker.patch("system.config.settings", bad_settings)
    mocker.patch("bot.ai_analyst._openai_client", None)

    from bot.ai_analyst import _get_openai_client
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _get_openai_client()


# ---------------------------------------------------------------------------
# _llm_call provider branch (Task 3 of the openai-primary-provider plan)
# ---------------------------------------------------------------------------

def _make_openai_resp(text: str):
    """Build a mock that mimics an OpenAI ChatCompletion with .choices[0].message.content."""
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    return resp


def test_llm_call_uses_openai_when_provider_is_openai(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="openai"))

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_openai_resp("openai response text")
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    result = _llm_call("system prompt", "user prompt", max_tokens=256)

    assert result == "openai response text"
    call_kwargs = mock_client.chat.completions.create.call_args[1]
    assert call_kwargs["model"] == "gpt-5.4"
    assert call_kwargs["temperature"] == 0
    assert call_kwargs["seed"] == 0
    assert call_kwargs["max_tokens"] == 256
    assert call_kwargs["messages"] == [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "user prompt"},
    ]


def test_llm_call_uses_anthropic_when_provider_is_anthropic(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="anthropic"))
    _mock_claude(mocker, "anthropic response text")

    from bot.ai_analyst import _llm_call
    result = _llm_call("system prompt", "user prompt", max_tokens=256)

    assert result == "anthropic response text"


def test_llm_call_routes_to_openai_by_real_default(mocker):
    """Regression guard: a fresh, unmodified Settings() (no provider override at
    all) must route through the OpenAI branch — this is the actual behavior
    change Task 7 made. Every other _llm_call test in this file pins the
    provider explicitly, so none of them would catch a regression in the
    default itself (e.g. _llm_call checking the wrong attribute)."""
    from system.config import Settings
    mocker.patch("system.config.settings", Settings())

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_openai_resp("default routed to openai")
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    result = _llm_call("system prompt", "user prompt")

    assert result == "default routed to openai"
    mock_client.chat.completions.create.assert_called_once()


def test_call_with_retry_retries_on_openai_rate_limit(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="openai"))
    mocker.patch("bot.ai_analyst.time.sleep")

    rate_err = _openai.RateLimitError(
        "rate limited",
        response=MagicMock(status_code=429, headers={}),
        body={},
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        rate_err, rate_err, _make_openai_resp("worked on third try"),
    ]
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call, _call_with_retry
    result = _call_with_retry(lambda: _llm_call("sys", "user"))

    assert result == "worked on third try"
    assert mock_client.chat.completions.create.call_count == 3


# ---------------------------------------------------------------------------
# _llm_call missing/empty content handling (refusal / pure tool-call / empty
# content block) — must raise ValueError so _call_with_retry can retry it,
# instead of letting a raw TypeError/IndexError propagate and kill the run.
# ---------------------------------------------------------------------------

def test_llm_call_openai_none_content_raises_value_error(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="openai"))

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _make_openai_resp(None)
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    with pytest.raises(ValueError):
        _llm_call("system prompt", "user prompt")


def test_llm_call_anthropic_empty_content_raises_value_error(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="anthropic"))

    mock_client = MagicMock()
    empty_resp = MagicMock()
    empty_resp.content = []
    mock_client.messages.create.return_value = empty_resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    with pytest.raises(ValueError):
        _llm_call("system prompt", "user prompt")


# ---------------------------------------------------------------------------
# _llm_call retry-without-temperature/seed on reasoning-tier OpenAI models
# that reject those sampling params with a 400.
# ---------------------------------------------------------------------------

def test_llm_call_openai_retries_without_temperature_seed_on_bad_request(mocker):
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="openai"))

    bad_request_err = _openai.BadRequestError(
        "Unsupported parameter: 'temperature' is not supported with this model.",
        response=MagicMock(status_code=400, headers={}),
        body={},
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        bad_request_err, _make_openai_resp("worked without temperature/seed"),
    ]
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    result = _llm_call("system prompt", "user prompt")

    assert result == "worked without temperature/seed"
    assert mock_client.chat.completions.create.call_count == 2
    first_kwargs = mock_client.chat.completions.create.call_args_list[0][1]
    second_kwargs = mock_client.chat.completions.create.call_args_list[1][1]
    assert "temperature" in first_kwargs
    assert "seed" in first_kwargs
    assert "temperature" not in second_kwargs
    assert "seed" not in second_kwargs


def test_llm_call_openai_retries_with_max_completion_tokens_on_bad_request(mocker):
    """gpt-5.4 rejects 'max_tokens' (not temperature/seed) with a 400 asking for
    'max_completion_tokens' instead — must swap the token param, not drop
    temperature/seed (that was the original bug: the retry re-sent max_tokens
    and failed identically)."""
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings", dataclasses.replace(real_settings, llm_provider="openai"))

    bad_request_err = _openai.BadRequestError(
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead.",
        response=MagicMock(status_code=400, headers={}),
        body={},
    )
    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = [
        bad_request_err, _make_openai_resp("worked with max_completion_tokens"),
    ]
    mocker.patch("bot.ai_analyst._get_openai_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    result = _llm_call("system prompt", "user prompt", max_tokens=256)

    assert result == "worked with max_completion_tokens"
    assert mock_client.chat.completions.create.call_count == 2
    first_kwargs = mock_client.chat.completions.create.call_args_list[0][1]
    second_kwargs = mock_client.chat.completions.create.call_args_list[1][1]
    assert first_kwargs["max_tokens"] == 256
    assert "max_completion_tokens" not in first_kwargs
    assert second_kwargs["max_completion_tokens"] == 256
    assert "max_tokens" not in second_kwargs
    assert second_kwargs["temperature"] == 0
    assert second_kwargs["seed"] == 0


# ============================================================
# HIGH #4: parse_*_response KeyError/TypeError → ValueError
# ============================================================

def test_parse_entry_missing_conviction_raises_value_error():
    """KeyError on missing 'conviction' must become ValueError so
    _call_with_retry can retry rather than propagate an unexpected exception."""
    raw = json.dumps({"entry": "buy", "position_pct": 4.5,
                      "rationale": "Ok", "risk_flags": []})
    with pytest.raises(ValueError, match="conviction"):
        parse_entry_response(raw)


def test_parse_entry_null_conviction_raises_value_error():
    """TypeError when float(None) must become ValueError (not kill the pipeline)."""
    raw = json.dumps({"conviction": None, "entry": "buy", "position_pct": 4.5,
                      "rationale": "Ok", "risk_flags": []})
    with pytest.raises(ValueError, match="conviction"):
        parse_entry_response(raw)


def test_parse_exit_missing_action_raises_value_error():
    """Missing 'action' must raise ValueError, not KeyError."""
    raw = json.dumps({"rationale": "Hold the position"})
    with pytest.raises(ValueError):
        parse_exit_response(raw)


def test_parse_technical_missing_conviction_raises_value_error():
    """Missing 'conviction' in technical response must raise ValueError."""
    raw = json.dumps({
        "entry": "buy", "setup_type": "breakout",
        "trend_alignment": "bullish", "momentum_state": "rising",
        "volume_confirmation": "confirmed", "relative_strength": "outperforming",
        "entry_trigger": "breakout", "invalidation_price": 95.0,
        "target_price": 115.0, "reward_risk": 3.0, "key_levels": "95, 115",
        "conflicts": [], "rationale": "Clean setup", "risk_flags": [],
    })
    with pytest.raises(ValueError, match="conviction"):
        parse_technical_response(raw, last_close=100.0)


# ============================================================
# HIGH #5: Prompt injection — external_data XML tags
# ============================================================

def test_entry_system_prompt_contains_external_data_warning():
    """Relevant system prompts must include the untrusted data instruction."""
    system = _build_entry_system("congressional", has_disclosure=True)
    assert "untrusted" in system.lower()


def test_exit_system_prompt_contains_external_data_warning():
    """_EXIT_SYSTEM must include the untrusted data instruction."""
    from bot.ai_analyst import _EXIT_SYSTEM
    assert "untrusted" in _EXIT_SYSTEM.lower()


# ============================================================
# MEDIUM #5: Anthropic content[0] — find first text block
# ============================================================

def test_llm_call_anthropic_returns_text_when_thinking_block_is_first(mocker):
    """When content[0] is a thinking block (type != 'text'), skip it and return
    the first text block."""
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings",
                 dataclasses.replace(real_settings, llm_provider="anthropic"))

    thinking_block = MagicMock()
    thinking_block.type = "thinking"

    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "actual response"

    resp = MagicMock()
    resp.content = [thinking_block, text_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    result = _llm_call("system prompt", "user prompt")
    assert result == "actual response"


def test_llm_call_anthropic_raises_value_error_when_no_text_block(mocker):
    """If Anthropic returns only non-text blocks, raise ValueError so
    _call_with_retry can retry."""
    import dataclasses
    from system.config import settings as real_settings
    mocker.patch("system.config.settings",
                 dataclasses.replace(real_settings, llm_provider="anthropic"))

    thinking_block = MagicMock()
    thinking_block.type = "thinking"

    resp = MagicMock()
    resp.content = [thinking_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = resp
    mocker.patch("bot.ai_analyst._get_client", return_value=mock_client)

    from bot.ai_analyst import _llm_call
    with pytest.raises(ValueError, match="no text block"):
        _llm_call("system prompt", "user prompt")


# ============================================================
# MEDIUM #7: conviction round(float()) not int()
# ============================================================

def test_parse_entry_conviction_rounds_float_up():
    """round(float(7.9)) should give conviction=8, not int(7.9)=7."""
    raw = json.dumps({"conviction": 7.9, "position_pct": 4.5,
                      "rationale": "Good", "entry": "buy", "risk_flags": []})
    score = parse_entry_response(raw)
    assert score.conviction == 8


def test_parse_technical_conviction_rounds_float_up():
    """round(float(7.9)) in parse_technical_response gives conviction=8."""
    payload = _technical_payload(conviction=7.9)
    score = parse_technical_response(payload, last_close=100.0)
    assert score.conviction == 8


# ============================================================
# TASK 9: short-side entry/exit scoring (bearish mirror)
# ============================================================

from bot.ai_analyst import score_entry_short, review_short_exit, EntryScore, ExitDecision


def test_score_entry_short_parses_response(monkeypatch):
    monkeypatch.setattr(
        "bot.ai_analyst._llm_call",
        lambda *a, **k: '{"conviction": 8, "position_pct": 4.0, "rationale": "overvalued", '
                        '"entry": "sell", "risk_flags": [], "expected_return_pct": -8.0}',
    )
    score = score_entry_short(sector="Technology", estimated_cost_pct=0.4,
                              factor_score=15, ticker="XYZ")
    assert isinstance(score, EntryScore)
    assert score.entry == "sell"
    assert score.expected_return_pct == -8.0


def test_review_short_exit_pnl_is_inverted(monkeypatch):
    captured = {}

    def fake_llm_call(system_text, prompt, max_tokens=256, **kwargs):
        captured["prompt"] = prompt
        return '{"action": "hold", "rationale": "still bearish"}'

    monkeypatch.setattr("bot.ai_analyst._llm_call", fake_llm_call)
    decision = review_short_exit(ticker="XYZ", entry_price=100.0, current_price=90.0, days_held=10)
    assert isinstance(decision, ExitDecision)
    # Short profits when price FALLS — entry 100 -> current 90 must show as +10%, not -10%.
    assert "+10.0%" in captured["prompt"]
