"""Unit tests for src/manual_exclusions.py — fails-open date-keyed reader."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import src.manual_exclusions as manual_exclusions


def _reset_cache():
    manual_exclusions._exclusions = None


def _use_file(monkeypatch, tmp_path, content: str | None):
    _reset_cache()
    if content is None:
        # point at a path that doesn't exist
        fake = tmp_path / "does_not_exist.json"
    else:
        fake = tmp_path / "manual_exclusions.json"
        fake.write_text(content, encoding="utf-8")
    monkeypatch.setattr(manual_exclusions, "_EXCLUSIONS_FILE", fake)


def test_missing_file_returns_empty_set(monkeypatch, tmp_path):
    _use_file(monkeypatch, tmp_path, None)
    result = manual_exclusions.get_excluded_symbols_for_date(date(2026, 7, 29))
    assert result == set()


def test_malformed_json_returns_empty_set_no_raise(monkeypatch, tmp_path):
    _use_file(monkeypatch, tmp_path, "{not valid json")
    result = manual_exclusions.get_excluded_symbols_for_date(date(2026, 7, 29))
    assert result == set()


def test_valid_file_date_match_returns_symbols(monkeypatch, tmp_path):
    _use_file(monkeypatch, tmp_path, '{"2026-07-29": ["RELIANCE", "TATASTEEL"]}')
    result = manual_exclusions.get_excluded_symbols_for_date(date(2026, 7, 29))
    assert result == {"RELIANCE", "TATASTEEL"}


def test_valid_file_date_miss_returns_empty_set(monkeypatch, tmp_path):
    _use_file(monkeypatch, tmp_path, '{"2026-07-29": ["RELIANCE"]}')
    result = manual_exclusions.get_excluded_symbols_for_date(date(2026, 7, 30))
    assert result == set()


def test_default_date_uses_today(monkeypatch, tmp_path):
    today = date.today()
    _use_file(monkeypatch, tmp_path, f'{{"{today.isoformat()}": ["INFY"]}}')
    result = manual_exclusions.get_excluded_symbols_for_date()
    assert result == {"INFY"}
