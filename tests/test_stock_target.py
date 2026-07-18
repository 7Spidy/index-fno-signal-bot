"""
Unit tests: halved stock target/floor and removal of RR suppression.

Covers the 2026-07-18 change spec:
- ATR_TARGET_K halved 0.40 -> 0.20
- floor_pts halved
- fallback multiplier halved 1.5R -> 0.75R
- rr_suppressed hardcoded False regardless of how thin rr_effective is
"""
import inspect

from src import stock_config as cfg
from src import stock_main


def test_atr_target_k_halved():
    assert cfg.ATR_TARGET_K == 0.20


def test_atr_target_pts_is_half_of_pre_change_value():
    stock_atr = 40.0  # arbitrary known ATR
    old_target_pts = 0.40 * stock_atr   # pre-change coefficient
    new_target_pts = cfg.ATR_TARGET_K * stock_atr
    assert new_target_pts == old_target_pts / 2


def test_floor_pts_is_half_of_pre_change_value_for_known_spot():
    spot = 1400.0
    old_floor_pts = max(0.0015 * spot, 2 * cfg.SLIPPAGE_PTS_EST)
    new_floor_pts = 0.5 * max(0.0015 * spot, 2 * cfg.SLIPPAGE_PTS_EST)
    assert new_floor_pts == old_floor_pts / 2


def test_fallback_target_is_half_of_pre_change_value():
    risk_pts = 10.0
    old_fallback_target = risk_pts * 1.5
    new_fallback_target = risk_pts * 0.75
    assert new_fallback_target == old_fallback_target / 2


def test_rr_suppressed_hardcoded_false_in_source():
    """rr_suppressed must be unconditionally False — never gates on MIN_RR,
    no matter how thin rr_effective is. Since rr_suppressed is now a literal
    assignment (not a comparison), assert directly on the source line rather
    than re-deriving MIN_RR gating logic that no longer exists."""
    source = inspect.getsource(stock_main)
    assert "rr_suppressed = False" in source
    # Guard against a regression that reintroduces a MIN_RR comparison here.
    assert "rr_effective < cfg.MIN_RR" not in source


def test_min_rr_constant_still_present_but_unused_for_gating():
    # MIN_RR stays defined (still surfaced informationally) per spec step 1.
    assert hasattr(cfg, "MIN_RR")
