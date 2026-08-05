"""
Anthropic spend metering and the hard monthly cap.

WHY THIS EXISTS
Brent capped Stealth Finder at $250/month on 2026-08-04. Before this module the
app had no idea what it spent: every call site read `response.usage`, logged it
to `logger.info`, and threw it away. A budget expressed in SIGNALS ("score 3,000
a night") is not a budget expressed in DOLLARS — the conversion factor is the
output length, which nobody was measuring.

WHAT IT DOES
Prices every Anthropic call from its actual reported usage, accumulates the
month's spend, and refuses to spend past the cap. The cap is enforced, not
advisory: `check_budget()` returns False and the caller must not make the call.

PRICING (Anthropic first-party rates, per million tokens)
Cache reads are ~0.1x the input rate; 5-minute ephemeral cache writes are 1.25x.
Both matter enormously here — the scoring system prompt is ~7,600 tokens and
identical on every call, so the difference between a cache hit and a miss is
roughly 12x on that portion.

`usage.input_tokens` is the UNCACHED REMAINDER, not the total. Total input =
input_tokens + cache_creation_input_tokens + cache_read_input_tokens. Summing
them as though input_tokens were the whole prompt understates a cached call and
overstates an uncached one, which is exactly backwards from what a budget needs.
"""
import json
import logging
import os
import threading
from datetime import date

logger = logging.getLogger(__name__)

LEDGER_TITLE = "__bullish_cost_ledger__"

# Per MILLION tokens. Keep in sync with anthropic.com/pricing; a stale entry here
# silently mis-meters the cap rather than failing loudly.
_PRICES = {
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-haiku-4-5":  {"in": 1.00, "out": 5.00},
    "claude-opus-4-8":   {"in": 5.00, "out": 25.00},
    "claude-sonnet-5":   {"in": 3.00, "out": 15.00},
}
_CACHE_READ_MULT  = 0.10   # cache hits bill at ~10% of the input rate
_CACHE_WRITE_MULT = 1.25   # 5-minute ephemeral writes bill at 125%

_DEFAULT_MONTHLY_CAP_USD = 250.0

# Unknown models are priced at the most expensive rate we know rather than zero.
# A model we forgot to add must not become free — that is how a cap silently
# stops capping.
_FALLBACK_PRICE = {"in": 5.00, "out": 25.00}

_lock = threading.Lock()
_pending = {"usd": 0.0, "calls": 0}     # spent since the last flush
_FLUSH_EVERY = 25                        # bound how much a crash can lose


def monthly_cap_usd() -> float:
    """The hard ceiling, overridable with ANTHROPIC_MONTHLY_CAP_USD."""
    raw = os.environ.get("ANTHROPIC_MONTHLY_CAP_USD", "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning("ANTHROPIC_MONTHLY_CAP_USD=%r is not a number — using default", raw)
    return _DEFAULT_MONTHLY_CAP_USD


def price_call(model: str, usage) -> float:
    """
    Dollars for one call, from the usage the API actually reported.

    Takes the response's usage object (or any object/dict with the same field
    names). Missing fields count as zero — a provider that stops reporting cache
    fields should under-report, not crash the scan.
    """
    if usage is None:
        return 0.0

    def _f(name):
        v = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    price = _PRICES.get(model)
    if price is None:
        price = _FALLBACK_PRICE
        logger.warning("No price for model %r — metering at Opus rates so the cap still binds", model)

    per_in, per_out = price["in"] / 1_000_000.0, price["out"] / 1_000_000.0
    return (
        _f("input_tokens") * per_in                              # uncached remainder
        + _f("cache_read_input_tokens") * per_in * _CACHE_READ_MULT
        + _f("cache_creation_input_tokens") * per_in * _CACHE_WRITE_MULT
        + _f("output_tokens") * per_out
    )


def _month_key(today=None) -> str:
    return (today or date.today()).strftime("%Y-%m")


def _read_ledger_raw(conn):
    """Read the ledger dict on a caller-supplied connection."""
    from sqlalchemy import text
    row = conn.execute(text("SELECT description FROM items WHERE title = :t"),
                       {"t": LEDGER_TITLE}).fetchone()
    if row is None or not row[0]:
        return {}
    try:
        return json.loads(row[0]) or {}
    except (ValueError, TypeError):
        return {}


def month_to_date_usd() -> float:
    """
    Persisted spend for the current month, plus anything not yet flushed.

    Runs on its OWN connection and deliberately does not touch db.session. This
    is checked before every scored signal, including from inside enrich_signal,
    and an ORM read there leaves the caller's session idle-in-transaction — which
    deadlocked the test suite against DROP TABLE and would block migrations in
    production the same way. `_scan_progress` learned this lesson first; the
    ledger gets the same treatment.
    """
    from ..extensions import db
    with db.engine.begin() as conn:
        data = _read_ledger_raw(conn)
    stored = float((data.get("months") or {}).get(_month_key(), 0.0))
    with _lock:
        return stored + _pending["usd"]


def record(model: str, usage, *, flush: bool = False) -> float:
    """
    Meter one call. Returns its cost.

    Accumulates in process and flushes periodically rather than writing on every
    call: four scoring workers each hitting the same row per signal would
    serialise the whole scan for no benefit, since the cap is checked before
    each call anyway.
    """
    usd = price_call(model, usage)
    with _lock:
        _pending["usd"] += usd
        _pending["calls"] += 1
        due = flush or _pending["calls"] >= _FLUSH_EVERY
    if due:
        flush_pending()
    return usd


def flush_pending() -> float:
    """
    Persist accumulated spend, on its own connection.

    Plain SQL rather than the ORM for the same reason as the read: this runs
    mid-scan from worker threads and must not touch, autoflush, or roll back the
    caller's session.
    """
    from sqlalchemy import text
    from ..extensions import db

    with _lock:
        usd, calls = _pending["usd"], _pending["calls"]
        if usd <= 0 and calls == 0:
            return 0.0
        _pending["usd"], _pending["calls"] = 0.0, 0

    try:
        with db.engine.begin() as conn:
            # Serialise the read-modify-write across workers and processes; a
            # lost update here is money the cap never sees.
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                         {"k": LEDGER_TITLE})
            data = _read_ledger_raw(conn)
            months = data.setdefault("months", {})
            key = _month_key()
            months[key] = round(float(months.get(key, 0.0)) + usd, 6)
            data["calls"] = int(data.get("calls", 0)) + calls
            data["updated_at"] = date.today().isoformat()
            payload = json.dumps(data, separators=(",", ":"))

            updated = conn.execute(
                text("UPDATE items SET description = :d, updated_at = now() "
                     "WHERE title = :t"),
                {"d": payload, "t": LEDGER_TITLE}).rowcount
            if not updated:
                # items.owner_id is a NOT NULL FK to users.id. Scheduler
                # bookkeeping rows once hardcoded owner_id=1 against a database
                # with no user 1; the IntegrityError was swallowed and the row
                # never persisted. Here that would mean the ledger resets on
                # every deploy and the cap silently stops capping — so resolve a
                # real owner and fail loudly if there is none.
                owner = conn.execute(text(
                    "SELECT id FROM users ORDER BY (role = 'admin') DESC, id LIMIT 1"
                )).fetchone()
                if owner is None:
                    raise RuntimeError("no users.id available to own the cost ledger")
                conn.execute(text(
                    "INSERT INTO items (title, item_type, owner_id, description, "
                    "created_at, updated_at) VALUES (:t, 'system', :o, :d, now(), now())"),
                    {"t": LEDGER_TITLE, "o": owner[0], "d": payload})
        return usd
    except Exception as exc:
        logger.error("Cost ledger flush failed (%s) — retaining %.4f as pending", exc, usd)
        with _lock:      # never drop spend; under-reporting lets the cap be exceeded
            _pending["usd"] += usd
            _pending["calls"] += calls
        return 0.0


def check_budget(estimated_usd: float = 0.0) -> bool:
    """
    True if a call costing roughly `estimated_usd` may proceed.

    FAILS CLOSED. If the ledger cannot be read we do not know what has been
    spent, and the entire point of a hard cap is that an unknown is treated as
    exhausted. A night of unscored signals is recoverable on the next run; an
    unbounded bill is not. Collection and triage-free work continue either way —
    only the paid step stops.
    """
    cap = monthly_cap_usd()
    if cap <= 0:
        return False
    try:
        spent = month_to_date_usd()
    except Exception:
        logger.error("Cost ledger unreadable — refusing to spend (failing closed)")
        return False
    return (spent + max(0.0, estimated_usd)) <= cap


def summary() -> dict:
    """Spend snapshot for the digest and the admin API."""
    cap = monthly_cap_usd()
    try:
        spent = month_to_date_usd()
    except Exception:
        return {"month": _month_key(), "cap_usd": cap, "error": "ledger_unreadable"}
    return {
        "month": _month_key(),
        "cap_usd": round(cap, 2),
        "spent_usd": round(spent, 2),
        "remaining_usd": round(max(0.0, cap - spent), 2),
        "pct_used": round(100.0 * spent / cap, 1) if cap else 0.0,
        "exhausted": spent >= cap,
    }
