"""Unit tests for position_tracker.py — ladder function and SL invariants."""
import pytest

from src.position_tracker import (
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
# SL can legitimately exceed the original T value
# ──────────────────────────────────────────────────────────────

class TestSlExceedsOriginalT:
    """Confirm that SL can exceed the original T value when price runs far
    past 1.0T — this is intended behaviour with T fixed as the denominator.
    No special-casing needed: it falls out naturally from the +0.1 per +0.1T
    ladder step beyond 1.0T.
    """

    def test_sl_exceeds_original_t_ce(self):
        # entry=100, T=100 (original, fixed), progress=1.8 (price=280)
        # n = floor((1.8 - 1.0) / 0.1) = 8 → sl_fraction = 0.9 + 0.8 = 1.7
        # sl_price = 100 + 1.7 * 100 = 270 > entry+T = 200
        entry = 100.0
        T = 100.0
        price_at_1_8T = entry + 1.8 * T  # 280
        sl = compute_ladder_sl(entry, T, price_at_1_8T, "CE", 0.0)
        assert sl > entry + T, f"Expected SL {sl} > entry+T {entry + T}"
        assert abs(sl - 270.0) < 1e-9

    def test_sl_exceeds_original_t_pe(self):
        # entry=100, T=100, progress=1.8 (price=−80)
        # sl_fraction = 1.7 → sl_price = 100 - 1.7*100 = −70 < entry−T = 0
        entry = 100.0
        T = 100.0
        price_at_1_8T = entry - 1.8 * T  # −80
        sl = compute_ladder_sl(entry, T, price_at_1_8T, "PE", 9999.0)
        assert sl < entry - T, f"Expected SL {sl} < entry-T {entry - T}"
        assert abs(sl - (-70.0)) < 1e-9
