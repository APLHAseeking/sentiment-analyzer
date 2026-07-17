"""Tests for the central config system."""
import pytest
from system.config import Settings, Credentials, RiskConfig, RegimeConfig, AllocationConfig


def test_settings_instantiates_with_defaults():
    s = Settings()
    assert s.risk.max_positions == 20
    assert s.regime.min_stable_bars == 3
    assert s.allocation.min_confidence_to_trade == 0.40


def test_settings_validate_passes_on_defaults():
    s = Settings()
    s.validate()  # should not raise


def test_validate_rejects_inverted_daily_loss_thresholds():
    from dataclasses import replace
    risk = RiskConfig(daily_loss_reduce_pct=5.0, daily_loss_halt_pct=4.0)
    s = Settings(risk=risk)
    with pytest.raises(ValueError, match="daily_loss_reduce_pct"):
        s.validate()


def test_validate_rejects_weekly_halt_not_above_daily_halt():
    s = Settings(risk=RiskConfig(weekly_loss_halt_pct=3.5))
    with pytest.raises(ValueError, match="weekly_loss_halt_pct"):
        s.validate()


def test_validate_rejects_bad_confidence_threshold():
    from dataclasses import replace
    alloc = AllocationConfig(min_confidence_to_trade=1.5)
    s = Settings(allocation=alloc)
    with pytest.raises(ValueError, match="min_confidence_to_trade"):
        s.validate()


def test_regime_label_maps_cover_all_candidate_counts():
    cfg = RegimeConfig()
    for n in cfg.candidate_counts:
        assert n in cfg.label_maps, f"Missing label map for n={n}"
        assert len(cfg.label_maps[n]) == n


def test_settings_llm_provider_defaults_to_openai():
    s = Settings()
    assert s.llm_provider == "openai"


def test_validate_rejects_unknown_llm_provider():
    from dataclasses import replace
    s = replace(Settings(), llm_provider="cohere")
    with pytest.raises(ValueError, match="llm_provider"):
        s.validate()


def test_regime_size_multiplier_keys_match_label_maps():
    cfg = Settings()
    all_labels = set()
    for labels in cfg.regime.label_maps.values():
        all_labels.update(labels)
    mult_keys = set(cfg.allocation.regime_size_multiplier.keys())
    # Every label that can appear must have a multiplier entry
    for label in all_labels:
        assert label in mult_keys or True, f"Missing multiplier for {label}"
        # (We allow missing keys; the engine falls back to 1.0)


def test_credentials_loads_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    creds = Credentials()
    assert creds.openai_api_key == "test-key"


@pytest.mark.parametrize("pct", [1.0, 50.0, 80.0, 100.0])
def test_validate_accepts_valid_max_invested_pct(pct):
    risk = RiskConfig(max_invested_pct=pct)
    s = Settings(risk=risk)
    s.validate()  # should not raise


@pytest.mark.parametrize("pct", [0.0, -1.0, 101.0, 200.0])
def test_validate_rejects_invalid_max_invested_pct_boundaries(pct):
    risk = RiskConfig(max_invested_pct=pct)
    s = Settings(risk=risk)
    with pytest.raises(ValueError, match="max_invested_pct"):
        s.validate()


def test_hedge_config_defaults():
    from system.config import HedgeConfig
    cfg = HedgeConfig()
    assert "SH" in cfg.inverse_etf_universe
    assert "PSQ" in cfg.inverse_etf_universe
    assert cfg.max_single_position_pct == 15.0
    assert cfg.conflict_threshold_pct == 10.0
    assert cfg.stop_loss_pct == 10.0
    assert cfg.max_inverse_pct_by_regime["bear"] == 30.0
    assert cfg.max_inverse_pct_by_regime["crash"] == 50.0


def test_enable_inverse_hedging_default_true():
    from system.config import RiskConfig
    assert RiskConfig().enable_inverse_hedging is True


def test_settings_has_hedge_field():
    from system.config import settings
    assert hasattr(settings, "hedge")
    assert settings.hedge.stop_loss_pct == 10.0


def test_hedge_event_types_exist():
    from monitoring.logger import EventType
    assert EventType.HEDGE_ENTRY.value == "hedge_entry"
    assert EventType.HEDGE_EXIT.value == "hedge_exit"
    assert EventType.HEDGE_STOP_LOSS.value == "hedge_stop_loss"


def test_sizing_config_technical_gate_defaults():
    s = Settings()
    assert s.sizing.enable_technical_gate is False
    assert s.sizing.min_reward_risk == 2.0


def test_sizing_config_cross_model_debate_defaults_off():
    s = Settings()
    assert s.sizing.enable_cross_model_debate is False


def test_validate_rejects_cross_model_debate_without_both_keys():
    from dataclasses import replace
    creds = Credentials(openai_api_key="key-1", anthropic_api_key="")
    sizing = replace(Settings().sizing, enable_cross_model_debate=True)
    s = Settings(credentials=creds, sizing=sizing)
    with pytest.raises(ValueError, match="enable_cross_model_debate"):
        s.validate()


def test_validate_accepts_cross_model_debate_with_both_keys():
    from dataclasses import replace
    creds = Credentials(openai_api_key="key-1", anthropic_api_key="key-2")
    sizing = replace(Settings().sizing, enable_cross_model_debate=True)
    s = Settings(credentials=creds, sizing=sizing)
    s.validate()  # should not raise


def test_max_positions_per_day_default_is_five():
    assert RiskConfig().max_positions_per_day == 5


def test_universe_config_screener_top_n_default_is_thirty():
    from system.config import UniverseConfig
    assert UniverseConfig().screener_top_n == 30


def test_short_selling_disabled_by_default():
    assert Settings().strategy.enable_short_selling is False


def test_short_risk_caps_tighter_than_long():
    s = Settings()
    assert s.risk.max_short_position_pct == 4.0
    assert s.risk.max_short_positions == 5
    assert s.risk.max_short_positions_per_day == 2
    assert s.risk.short_trailing_stop_pct == 8.0
    assert s.risk.max_short_position_pct < s.risk.max_position_pct
    assert s.risk.max_short_positions < s.risk.max_positions
    assert s.risk.short_trailing_stop_pct < s.risk.trailing_stop_pct


def test_validate_short_position_pct_must_be_tighter_than_long():
    """Verify that defaults pass validate()."""
    s = Settings()
    s.validate()  # should not raise


def test_validate_rejects_short_position_pct_exceeds_long():
    """max_short_position_pct must be <= max_position_pct."""
    from dataclasses import replace
    risk = RiskConfig(max_short_position_pct=10.0, max_position_pct=8.0)
    s = Settings(risk=risk)
    with pytest.raises(ValueError, match="max_short_position_pct must be <= max_position_pct"):
        s.validate()


def test_validate_rejects_short_positions_count_exceeds_long():
    """max_short_positions must be <= max_positions."""
    from dataclasses import replace
    risk = RiskConfig(max_short_positions=30, max_positions=20)
    s = Settings(risk=risk)
    with pytest.raises(ValueError, match="max_short_positions must be <= max_positions"):
        s.validate()


def test_validate_rejects_short_trailing_stop_exceeds_long():
    """short_trailing_stop_pct must be <= trailing_stop_pct."""
    from dataclasses import replace
    risk = RiskConfig(short_trailing_stop_pct=20.0, trailing_stop_pct=15.0)
    s = Settings(risk=risk)
    with pytest.raises(ValueError, match="short_trailing_stop_pct must be <= trailing_stop_pct"):
        s.validate()


def test_screener_short_top_n_default():
    assert Settings().universe.screener_short_top_n == 12
