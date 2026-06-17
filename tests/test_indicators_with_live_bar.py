"""Tests for indicators.with_live_bar() — confirms iloc[-1] is dropped, not kept."""
import numpy as np
import pandas as pd

from src import indicators

N = 30


def _make_df(n=N, close_val=23000.0):
    return pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01 09:15", periods=n, freq="5min"),
        "open":  np.full(n, close_val - 5),
        "high":  np.full(n, close_val + 10),
        "low":   np.full(n, close_val - 10),
        "close": np.full(n, close_val),
        "volume": np.ones(n) * 1000,
    })


def test_with_live_bar_result_length_equals_original():
    """Result has same row count as input — one row replaced, not added."""
    df = _make_df()
    result = indicators.with_live_bar(df, 23010.0)
    assert len(result) == len(df)


def test_with_live_bar_synthetic_bar_close_is_live_ltp():
    """Last row's close is the live LTP."""
    df = _make_df(close_val=23000.0)
    result = indicators.with_live_bar(df, 23050.0)
    assert float(result.iloc[-1]["close"]) == 23050.0


def test_with_live_bar_synthetic_bar_open_is_p0_close():
    """Synthetic bar opens at P0's close."""
    df = _make_df(close_val=23000.0)
    result = indicators.with_live_bar(df, 23050.0)
    assert float(result.iloc[-1]["open"]) == 23000.0


def test_with_live_bar_high_low_live_above_p0():
    """When live > P0 close: high=live, low=P0 close."""
    df = _make_df(close_val=23000.0)
    result = indicators.with_live_bar(df, 23100.0)
    assert float(result.iloc[-1]["high"]) == 23100.0
    assert float(result.iloc[-1]["low"])  == 23000.0


def test_with_live_bar_high_low_live_below_p0():
    """When live < P0 close: high=P0 close, low=live."""
    df = _make_df(close_val=23000.0)
    result = indicators.with_live_bar(df, 22900.0)
    assert float(result.iloc[-1]["high"]) == 23000.0
    assert float(result.iloc[-1]["low"])  == 22900.0


def test_with_live_bar_drops_corrupt_last_row_not_appends_after():
    """Extreme values in df.iloc[-1] must NOT appear in the DMI result.

    If with_live_bar() appended after iloc[-1] instead of replacing it,
    the corrupt extreme high/low would produce a massive true-range spike
    that collapses DI values to near-zero — detectable by comparing against
    a clean df run through the same function.
    """
    df_corrupt = _make_df()
    df_corrupt.at[N - 1, "high"] = 99999.0   # extreme — would destroy DMI if kept
    df_corrupt.at[N - 1, "low"]  = 0.001

    live_ltp = 23005.0
    result_corrupt = indicators.with_live_bar(df_corrupt, live_ltp)
    result_clean   = indicators.with_live_bar(_make_df(), live_ltp)

    # Length check: replacement, not append
    assert len(result_corrupt) == N
    assert len(result_clean)   == N

    # DI values must match — corrupt row was dropped before the synthetic bar
    pdi_c, ndi_c, _ = indicators.dmi_wilder(result_corrupt)
    pdi_k, ndi_k, _ = indicators.dmi_wilder(result_clean)

    assert abs(float(pdi_c.iloc[-1]) - float(pdi_k.iloc[-1])) < 0.5, (
        f"PDI mismatch ({pdi_c.iloc[-1]:.2f} vs {pdi_k.iloc[-1]:.2f}): "
        "corrupt last row was NOT dropped — with_live_bar is appending, not replacing"
    )
    assert abs(float(ndi_c.iloc[-1]) - float(ndi_k.iloc[-1])) < 0.5, (
        f"NDI mismatch ({ndi_c.iloc[-1]:.2f} vs {ndi_k.iloc[-1]:.2f}): "
        "corrupt last row was NOT dropped"
    )
