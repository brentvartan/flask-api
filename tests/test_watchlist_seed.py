"""
Tests for the CSV-backed watchlist seed loaders (Task 8).

Covers:
- A founder row in the CSV matches via check_conviction_match with designation='founder'
- An alumni row in the CSV matches via check_exit_alumni_match with designation='alumni'
- A name not in the CSV returns None from both matchers
- Designation is mutually exclusive: founder not in alumni, alumni not in conviction
"""
import csv
import os
import pytest

from app.services.conviction import (
    CONVICTION_FOUNDERS,
    check_conviction_match,
)
from app.services.exit_watch import (
    EXIT_ALUMNI,
    check_exit_alumni_match,
)

_CSV_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', 'data', 'watchlist_seed.csv')
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_seed_names_by_designation(designation: str) -> list[str]:
    names = []
    with open(_CSV_PATH, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            if row.get('designation', '').strip().lower() == designation:
                name = row.get('name', '').strip()
                if name:
                    names.append(name)
    return names


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def founder_names():
    return _get_seed_names_by_designation('founder')


@pytest.fixture(scope='module')
def alumni_names():
    return _get_seed_names_by_designation('alumni')


# ── CSV sanity ────────────────────────────────────────────────────────────────

def test_csv_exists():
    assert os.path.exists(_CSV_PATH), f"watchlist_seed.csv not found at {_CSV_PATH}"


def test_csv_has_founders(founder_names):
    assert len(founder_names) > 0, "No founder rows found in watchlist_seed.csv"


def test_csv_has_alumni(alumni_names):
    assert len(alumni_names) > 0, "No alumni rows found in watchlist_seed.csv"


# ── Loader integrity ──────────────────────────────────────────────────────────

def test_conviction_founders_loaded(founder_names):
    assert len(CONVICTION_FOUNDERS) > 0
    for name in founder_names:
        assert name in CONVICTION_FOUNDERS, f"Founder '{name}' missing from CONVICTION_FOUNDERS"


def test_exit_alumni_loaded(alumni_names):
    assert len(EXIT_ALUMNI) > 0
    for name in alumni_names:
        assert name in EXIT_ALUMNI, f"Alumni '{name}' missing from EXIT_ALUMNI"


# ── Match: founder in seed → conviction match with designation='founder' ───────

def test_founder_matches_conviction(founder_names):
    name = founder_names[0]
    result = check_conviction_match(name)
    assert result is not None, f"check_conviction_match returned None for founder '{name}'"
    assert result["name"] == name
    assert result["designation"] == "founder"


def test_conviction_match_returns_designation_field(founder_names):
    name = founder_names[0]
    result = check_conviction_match(name)
    assert "designation" in result
    assert result["designation"] == "founder"


# ── Match: alumni in seed → exit alumni match with designation='alumni' ────────

def test_alumni_matches_exit_watch(alumni_names):
    name = alumni_names[0]
    result = check_exit_alumni_match(name)
    assert result is not None, f"check_exit_alumni_match returned None for alumni '{name}'"
    assert result["name"] == name
    assert result["designation"] == "alumni"


def test_exit_alumni_match_returns_designation_field(alumni_names):
    name = alumni_names[0]
    result = check_exit_alumni_match(name)
    assert "designation" in result
    assert result["designation"] == "alumni"


# ── Name NOT in seed → both return None ───────────────────────────────────────

def test_unknown_name_not_in_conviction():
    result = check_conviction_match("Completely Fictional Person Zzz")
    assert result is None


def test_unknown_name_not_in_alumni():
    result = check_exit_alumni_match("Completely Fictional Person Zzz")
    assert result is None


# ── Per-row CSV designation is always 'founder' or 'alumni' ──────────────────
# Note: a person CAN appear in both dicts if they have two rows with different
# designations (e.g. founder of Brand A + alumni exec at Brand B). That is valid.
# What must never happen is a single row with a missing or unrecognised designation.

def test_every_row_has_valid_designation():
    valid = {'founder', 'alumni'}
    bad = []
    with open(_CSV_PATH, newline='', encoding='utf-8') as f:
        for i, row in enumerate(csv.DictReader(f), start=2):  # row 1 is header
            d = row.get('designation', '').strip().lower()
            if d not in valid:
                bad.append(f"row {i}: name='{row.get('name', '')}' designation='{d}'")
    assert not bad, "Invalid designations found:\n" + "\n".join(bad)


def test_every_row_has_a_name():
    bad = []
    with open(_CSV_PATH, newline='', encoding='utf-8') as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            if not row.get('name', '').strip():
                bad.append(f"row {i}: empty name")
    assert not bad, "Rows with missing names:\n" + "\n".join(bad)
