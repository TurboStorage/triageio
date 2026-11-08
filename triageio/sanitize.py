"""Neutralize spreadsheet formula injection in exported text cells."""
from __future__ import annotations

import pandas as pd

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value):
    """Prefix text starting with = + - @ tab or CR with a single quote.

    Non-string values (numbers, NaN, None) are returned unchanged.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def sanitize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of df with text cells made safe; numeric columns untouched."""
    out = df.copy()
    for col in out.columns:
        series = out[col]
        if series.dtype == object or pd.api.types.is_string_dtype(series):
            out[col] = series.map(safe_cell)
    out.columns = [safe_cell(c) for c in out.columns]
    return out
