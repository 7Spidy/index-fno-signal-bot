"""Unit tests for position_tracker.py — ladder function and SL invariants."""
import pytest

from src.position_tracker import (
    compute_ai_adjusted_target,
    compute_final_sl,
    compute_ladder_sl,
)


# ──────────────────────────────────────────────────────────────
# Ladder table — exact values from spec
# ──────────────────────────────────────────────────────────────

class TestLadderExactValues:
    """The spec defines a fixed table of (progress, sl_fraction) pairs.
    Verify each breakpoint for both CE and PE directions.
    """

    # entry=100, T=100, so sl_price = 100 + sl_fraction*100 for CE
    # progress = (current_price - entry) / T for CE

    def _ce(self, progress, prior_sl=0.0):
        """CE: entry=100, T=100, current_price = 100 + progress*100."""
        entry = 100.0
        T = 100.0
        current_price = entry + progress * T
        return compute_ladder_sl(entry, T, current_price, "CE", prior_sl)

    def _pe(self, progress, prior_sl=9999.0):
        """PE: entry=100, T=100, current_price = 100 - progress*100."""
        entry = 100.0
        T = 100.0
        current_price = entry - progress * T
        return compute_ladder_sl(entry, T, current_price, "PE", prior_sl)

    def test_progress_below_0_5_returns_prior_sl_ce(self):
        assert self._ce(0.49, prior_sl=90.0) == 90.0

    def test_progress_below_0_5_returns_prior_sl_pe(self):
        assert self._pe(0.49, prior_sl=110.0) == 110.0

    def test_progress_0_5_sl_fraction_0_25_ce(self):
        # sl_price = 100 + 0.25*100 = 125
        result = self._ce(0.5)
        assert abs(result - 125.0) < 1e-9

    def test_progress_0_5_sl_fraction_0_25_pe(self):
        # sl_price = 100 - 0.25*100 = 75
        result = self._pe(0.5)
        assert abs(result - 75.0) < 1e-9

    def test_progress_0_9_sl_fraction_0_6_ce(self):
        # sl_price = 100 + 0.6*100 = 160
        result = self._ce(0.9)
        assert abs(result - 160.0) < 1e-9

    def test_progress_0_9_sl_fraction_0_6_pe(self):
        # sl_price = 100 - 0.6*100 = 40
        result = self._pe(0.9)
        assert abs(result - 40.0) < 1e-9

    def test_progress_1_0_sl_fraction_0_9_ce(self):
        # sl_price = 100 + 0.9*100 = 190
        result = self._ce(1.0)
        assert abs(result - 190.0) < 1e-9

    def test_progress_1_0_sl_fraction_0_9_pe(self):
        # sl_price = 100 - 0.9*100 = 10
        result = self._pe(1.0)
        assert abs(result - 10.0) < 1e-9

    def test_progress_1_1_sl_fraction_1_0_ce(self):
        # n=1, sl_fraction = 0.9 + 0.1*1 = 1.0 → sl_price = 100 + 100 = 200
        result = self._ce(1.1)
        assert abs(result - 200.0) < 1e-9

    def test_progress_1_1_sl_fraction_1_0_pe(self):
        # n=1, sl_fraction=1.0 → sl_price = 100 - 100 = 0
        result = self._pe(1.1)
        assert abs(result - 0.0) < 1e-9

    def test_progress_1_2_sl_fraction_1_1_ce(self):
        # n=2, sl_fraction = 0.9 + 0.1*2 = 1.1 → sl_price = 100 + 110 = 210
        result = self._ce(1.2)
        assert abs(result - 210.0) < 1e-9

    def test_progress_1_2_sl_fraction_1_1_pe(self):
        # n=2, sl_fraction=1.1 → sl_price = 100 - 110 = -10
        result = self._pe(1.2)
        assert abs(result - (-10.0)) < 1e-9

    def test_large_progress_ce(self):
        # progress=2.3 → n = floor((2.3-1.0)/0.1) = floor(13) = 13
        # sl_fraction = 0.9 + 0.1*13 = 2.2 → sl_price = 100 + 220 = 320
        result = self._ce(2.3)
        assert abs(result - 320.0) < 1e-9


# ──────────────────────────────────────────────────────────────
# Monotonicity
# ──────────────────────────────────────────────────────────────

class TestMonotonicity:
    """Calling compute_ladder_sl with a lower current_price (CE) after a higher
    one must never decrease the returned SL (monotonic non-decreasing for CE,
    non-increasing for PE).
    """

    def test_ce_monotonic_ascending(self):
        entry, T = 100.0, 100.0
        prior_sl = 0.0
        prices = [145, 155, 165, 140, 150]   # price dips then rises
        sls = []
        for p in prices:
            sl = compute_ladder_sl(entry, T, p, "CE", prior_sl)
            sls.append(sl)
            prior_sl = sl
        # Every SL must be >= the previous one
        for i in range(1, len(sls)):
            assert sls[i] >= sls[i - 1], f"SL not monotonic at step {i}: {sls}"

    def test_pe_monotonic_descending(self):
        entry, T = 100.0, 100.0
        prior_sl = 9999.0
        prices = [55, 45, 35, 50, 40]   # price bounces but trends down
        sls = []
        for p in prices:
            sl = compute_ladder_sl(entry, T, p, "PE", prior_sl)
            sls.append(sl)
            prior_sl = sl
        for i in range(1, len(sls)):
            assert sls[i] <= sls[i - 1], f"SL not monotonic at step {i}: {sls}"

    def test_ce_never_decreases_when_price_drops_below_entry(self):
        # Price fell back below entry — ladder should not be reset
        entry, T = 100.0, 100.0
        # First call at progress 0.6 → sl set to 125
        sl1 = compute_ladder_sl(entry, T, 160.0, "CE", 0.0)
        # Second call: price drops to 80 (below entry)
        sl2 = compute_ladder_sl(entry, T, 80.0, "CE", sl1)
        assert sl2 >= sl1


# ──────────────────────────────────────────────────────────────
# compute_final_sl
# ──────────────────────────────────────────────────────────────

class TestComputeFinalSl:
    def test_ce_takes_max(self):
        assert compute_final_sl(150.0, 160.0, "CE") == 160.0
        assert compute_final_sl(160.0, 150.0, "CE") == 160.0

    def test_pe_takes_min(self):
        assert compute_final_sl(50.0, 40.0, "PE") == 40.0
        assert compute_final_sl(40.0, 50.0, "PE") == 40.0

    def test_ce_final_never_worse_than_ladder(self):
        ladder = 150.0
        ai     = 145.0   # AI mistakenly looser — but compute_final_sl should correct
        result = compute_final_sl(ladder, ai, "CE")
        assert result >= ladder

    def test_pe_final_never_worse_than_ladder(self):
        ladder = 50.0
        ai     = 55.0   # AI mistakenly looser
        result = compute_final_sl(ladder, ai, "PE")
        assert result <= ladder


# ──────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            compute_ladder_sl(100.0, 100.0, 150.0, "LONG", 90.0)

    def test_t_zero_returns_prior_sl(self):
        result = compute_ladder_sl(100.0, 0.0, 150.0, "CE", 90.0)
        assert result == 90.0

    def test_t_negative_returns_prior_sl(self):
        result = compute_ladder_sl(100.0, -10.0, 150.0, "CE", 90.0)
        assert result == 90.0

    def test_t_none_returns_prior_sl(self):
        result = compute_ladder_sl(100.0, None, 150.0, "CE", 90.0)
        assert result == 90.0

    def test_current_price_none_returns_prior_sl(self):
        result = compute_ladder_sl(100.0, 100.0, None, "CE", 90.0)
        assert result == 90.0

    def test_direction_case_insensitive(self):
        r1 = compute_ladder_sl(100.0, 100.0, 160.0, "ce", 0.0)
        r2 = compute_ladder_sl(100.0, 100.0, 160.0, "CE", 0.0)
        assert r1 == r2


# ──────────────────────────────────────────────────────────────
# compute_final_sl — backward compatibility and sl_history
# ──────────────────────────────────────────────────────────────

class TestComputeFinalSlHistory:
    def test_backward_compat_no_sl_history_ce(self):
        # 3-arg call (old signature) must still work unchanged
        result = compute_final_sl(150.0, 160.0, "CE")
        assert result == 160.0

    def test_backward_compat_no_sl_history_pe(self):
        result = compute_final_sl(50.0, 40.0, "PE")
        assert result == 40.0

    def test_sl_history_prevents_regression_ce(self):
        # Historical best (130.0) exceeds both fresh ladder and AI values
        result = compute_final_sl(125.0, 126.0, "CE", [100.0, 130.0])
        assert result == 130.0

    def test_sl_history_prevents_regression_pe(self):
        # Historical tightest (70.0) is below both fresh values
        result = compute_final_sl(75.0, 74.0, "PE", [90.0, 70.0])
        assert result == 70.0

    def test_empty_sl_history_behaves_like_no_history(self):
        r1 = compute_final_sl(150.0, 160.0, "CE", [])
        r2 = compute_final_sl(150.0, 160.0, "CE")
        assert r1 == r2

    def test_none_sl_history_behaves_like_no_history(self):
        r1 = compute_final_sl(150.0, 160.0, "CE", None)
        r2 = compute_final_sl(150.0, 160.0, "CE")
        assert r1 == r2


# ──────────────────────────────────────────────────────────────
# compute_ai_adjusted_target
# ──────────────────────────────────────────────────────────────

_SNAPSHOT_UP = {
    "rsi_last3":  [40.0, 45.0, 50.0],   # RSI rising  → favoring CE
    "dmi_last": {
        "pdi": [20.0, 26.0, 28.0],       # +DI rising and > 25
        "ndi": [28.0, 22.0, 18.0],
        "adx": 26.0,
    },
    "progress":      0.95,
    "current_price": 195.0,
    "T":             100.0,
    "instrument":    "NIFTY",
}

_SNAPSHOT_DOWN = {
    "rsi_last3":  [50.0, 45.0, 40.0],   # RSI falling → reversing against CE
    "dmi_last": {
        "pdi": [28.0, 22.0, 18.0],       # +DI falling
        "ndi": [18.0, 22.0, 30.0],       # -DI rising and flipped above +DI
        "adx": 22.0,
    },
    "progress":      0.95,
    "current_price": 195.0,
    "T":             115.0,
    "instrument":    "NIFTY",
}


class TestComputeAiAdjustedTarget:
    def test_upward_revision_when_momentum_confirmed(self):
        result = compute_ai_adjusted_target("CE", _SNAPSHOT_UP, 100.0, 100.0, 0.95)
        assert abs(result - 115.0) < 1e-9   # 100 * 1.15

    def test_upward_revision_pe(self):
        snap = dict(_SNAPSHOT_UP)
        snap["rsi_last3"] = [50.0, 45.0, 40.0]  # RSI falling → favoring PE
        snap["dmi_last"] = {
            "pdi": [28.0, 22.0, 18.0],
            "ndi": [20.0, 26.0, 29.0],           # -DI rising and > 25
            "adx": 26.0,
        }
        result = compute_ai_adjusted_target("PE", snap, 100.0, 100.0, 0.95)
        assert abs(result - 115.0) < 1e-9

    def test_downward_revision_when_momentum_reversed(self):
        result = compute_ai_adjusted_target("CE", _SNAPSHOT_DOWN, 115.0, 100.0, 0.95)
        expected = max(115.0 * 0.9, 100.0)  # 103.5
        assert abs(result - expected) < 1e-9

    def test_never_below_original_t_on_repeated_reversal(self):
        T = 100.0
        original_T = 100.0
        snap = dict(_SNAPSHOT_DOWN)
        snap["T"] = T
        for _ in range(10):
            snap["T"] = T
            T = compute_ai_adjusted_target("CE", snap, T, original_T, 0.95)
            assert T >= original_T, f"T={T} fell below original_T={original_T}"

    def test_unchanged_when_progress_below_0_9(self):
        snap = dict(_SNAPSHOT_UP)
        snap["progress"] = 0.89
        result = compute_ai_adjusted_target("CE", snap, 100.0, 100.0, 0.89)
        assert result == 100.0

    def test_unchanged_when_neither_condition_met(self):
        # RSI flat (no staircase), DI below threshold
        snap = {
            "rsi_last3":  [45.0, 45.0, 45.0],
            "dmi_last": {
                "pdi": [20.0, 20.0, 20.0],   # not rising, below threshold
                "ndi": [20.0, 20.0, 20.0],
                "adx": 18.0,
            },
            "progress":      0.95,
            "current_price": 195.0,
            "T":             100.0,
            "instrument":    "NIFTY",
        }
        result = compute_ai_adjusted_target("CE", snap, 100.0, 100.0, 0.95)
        assert result == 100.0

    def test_missing_dmi_returns_unchanged(self):
        snap = dict(_SNAPSHOT_UP)
        snap = {k: v for k, v in snap.items() if k != "dmi_last"}
        result = compute_ai_adjusted_target("CE", snap, 100.0, 100.0, 0.95)
        assert result == 100.0

    def test_missing_rsi_returns_unchanged(self):
        snap = {k: v for k, v in _SNAPSHOT_UP.items() if k != "rsi_last3"}
        result = compute_ai_adjusted_target("CE", snap, 100.0, 100.0, 0.95)
        assert result == 100.0


# ──────────────────────────────────────────────────────────────
# Critical invariant: SL never regresses after T cycle (up → down)
# ──────────────────────────────────────────────────────────────

class TestSlInvariantAfterTRevision:
    """Show that compute_final_sl with sl_history protects against regression
    even when T falls back and ladder_sl computes a weaker value."""

    def test_sl_invariant_ce(self):
        entry = 100.0
        original_T = 100.0
        ltp = 185.0

        # Step 1: T raised
        T_raised = compute_ai_adjusted_target("CE", _SNAPSHOT_UP, original_T, original_T, 0.95)
        assert T_raised > original_T

        # Step 2: compute sl with raised T (progress = 85/115 ≈ 0.739 → fraction 0.25)
        ladder_1 = compute_ladder_sl(entry, T_raised, ltp, "CE", 0.0)
        final_1  = compute_final_sl(ladder_1, ladder_1, "CE", [0.0])
        sl_history = [0.0, final_1]

        # Step 3: T trimmed back
        T_trimmed = compute_ai_adjusted_target("CE", _SNAPSHOT_DOWN, T_raised, original_T, 0.95)
        assert T_trimmed < T_raised
        assert T_trimmed >= original_T   # original_T floor

        # Step 4: ladder with lower T and a stale/reset prior_sl (demonstrating regression risk)
        ladder_regressed = compute_ladder_sl(entry, T_trimmed, ltp, "CE", 0.0)
        # This may be weaker than final_1 because T is smaller → sl_price = entry + 0.25*T_trimmed
        # is less than entry + 0.25*T_raised

        # Step 5: compute_final_sl with sl_history must return at least final_1
        final_2 = compute_final_sl(ladder_regressed, ladder_regressed, "CE", sl_history)
        assert final_2 >= final_1, (
            f"SL regressed: final_2={final_2} < final_1={final_1}. "
            "sl_history invariant failed."
        )

    def test_sl_invariant_pe(self):
        entry = 100.0
        original_T = 100.0
        ltp = 15.0   # deep in-the-money for PE

        snap_up_pe = {
            "rsi_last3":  [50.0, 45.0, 40.0],
            "dmi_last": {
                "pdi": [28.0, 22.0, 18.0],
                "ndi": [20.0, 26.0, 29.0],
                "adx": 26.0,
            },
            "progress": 0.95, "current_price": ltp, "T": original_T, "instrument": "NIFTY",
        }
        snap_down_pe = {
            "rsi_last3":  [40.0, 45.0, 50.0],   # RSI rising → reversing against PE
            "dmi_last": {
                "pdi": [18.0, 22.0, 30.0],       # +DI rising and flipped above -DI
                "ndi": [28.0, 22.0, 18.0],
                "adx": 22.0,
            },
            "progress": 0.95, "current_price": ltp, "T": original_T, "instrument": "NIFTY",
        }

        T_raised  = compute_ai_adjusted_target("PE", snap_up_pe, original_T, original_T, 0.95)
        assert T_raised > original_T

        ladder_1  = compute_ladder_sl(entry, T_raised, ltp, "PE", 9999.0)
        final_1   = compute_final_sl(ladder_1, ladder_1, "PE", [9999.0])
        sl_history = [9999.0, final_1]

        T_trimmed = compute_ai_adjusted_target("PE", snap_down_pe, T_raised, original_T, 0.95)
        assert T_trimmed < T_raised
        assert T_trimmed >= original_T

        ladder_regressed = compute_ladder_sl(entry, T_trimmed, ltp, "PE", 9999.0)
        final_2 = compute_final_sl(ladder_regressed, ladder_regressed, "PE", sl_history)
        assert final_2 <= final_1, (
            f"SL regressed (PE): final_2={final_2} > final_1={final_1}. "
            "sl_history invariant failed."
        )
