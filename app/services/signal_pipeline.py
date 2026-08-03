"""
Bullish Stealth Finder — Shared Post-Save Signal Pipeline.

ONE implementation of everything that must happen to a signal AFTER it has been
persisted. Both scan paths call it:

  - manual scans    → app/api/scans/routes.py::_enrich_items_in_background
  - scheduled scans → app/services/scheduler.py::run_scan_now

Why this file exists (P1): the two paths independently implemented the same
concepts and silently diverged three times — independent json.dumps whitelists,
the confluence score gate, and conviction/alumni matching. The result was that
the NIGHTLY scan, the one that is supposed to be airtight, did strictly LESS work
than a human clicking "Run Scan": no trademark-assignment resolution, no
conviction or alumni text matching, no Form D related-persons extraction, no
acquisition detection, and no 'owner' in the enrichment prompt. A conviction
founder named in a USPTO trademark owner field was caught manually and missed
nightly.

Stage order is load-bearing:
  (a) parse meta
  (b) owner extraction        — explicit meta['owner'], else the "Owner: NAME." notes prefix
  (c) trademark assignment    — Lightyear → Filament stealth-entity resolution
  (d) conviction, then alumni — alumni only when there is no conviction match
  (e) Form D related persons  — promotes a named officer's match to the top level
  (f) acquisition detection   — grows the exit-watch brand list
  (g) CONFLUENCE, then TRIAGE, then ENRICH
                              — enrichment.py's tier floors read signal["signal_types"]
                                and its confluence prompt boost reads signal_count >= 2.
                                Neither scan path passed them before, so the
                                "never bury a trademark or Form D as cold" floor and
                                the multi-signal boost were dead code at scan time.
                                TRIAGE (P2) sits between them: a cheap Haiku gate that
                                decides whether the expensive Sonnet scoring pass is
                                worth making. It runs AFTER confluence and AFTER people
                                matching precisely so it can be BYPASSED for the signals
                                that must never be gated by a cheap model — a conviction
                                founder, an exit alumnus, or a brand confluence has
                                already seen in 2+ distinct signal types.
  (h) persist enrichment
  (i) confluence alert        — AFTER enrichment, gated by should_send_confluence_alert()
  (j) watchlist-match alert    — conviction OR alumni named on this signal
  (k) HOT handling            — auto-watchlist + founder enrichment

Every stage is individually try/except'd. A failure in one stage is logged, is
recorded in the returned "errors" list, and does NOT abort the remaining stages —
a signal must never be lost because one sub-step raised.

Nothing here gates VISIBILITY. People matching, consumer codes and raise size
change rank and badges only; the signal row is already saved before this runs.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone

from flask import current_app

logger = logging.getLogger(__name__)

# Founder enrichment triggers. A newswire brand has just broken cover, so it is
# the highest-urgency moment to identify the founder — hence the lower floor.
FOUNDER_ENRICHMENT_MIN_SCORE = 70
FOUNDER_ENRICHMENT_MIN_SCORE_NEWSWIRE = 50

# Kill switch for Stage 1. Default ON; setting ENABLE_TRIAGE to one of these
# values restores exactly the pre-P2 behaviour — every saved signal goes straight
# to the full Sonnet scoring pass.
_TRIAGE_OFF_VALUES = frozenset({"0", "false", "no", "off"})


# Item.title is String(255). A trademark wordmark can be far longer — USPTO
# sometimes puts the DESIGN DESCRIPTION in that field ("THE MARK CONSISTS OF A
# STYLIZED CIRCULAR DESIGN...", 572 chars observed) — and a single over-long
# value raises DataError, which fails the entire batch insert and ends the run.
# Invisible at the ~200 signals a run this path once handled; certain at ~8,500.
# The untruncated name is still kept in the JSON description, which is Text.
TITLE_MAX = 255


def _safe_title(name: str) -> str:
    """Fit a brand name to the title column without ever failing an insert."""
    return (name or "")[:TITLE_MAX]


class _DeferAlert(Exception):
    """Control-flow sentinel: alert intentionally deferred to the weekly digest."""


# ─── Alert delivery mode ──────────────────────────────────────────────────────

# Brent, 2026-07-30: "I would rather get one big one on Monday and work from
# there — especially if it makes it easier and more stable." So WEEKLY is the
# default: per-event confluence and watchlist emails are deferred into the
# Monday digest instead of being sent the moment they fire. This is also the
# more stable shape — no Resend calls inside scan runs, fewer failure modes.
#
# "realtime" restores the old per-event behaviour (set alert_delivery in the
# __bullish_settings__ row). A ConfluenceHit deferred this way simply keeps
# alert_sent=False, which the digest uses as its queue.
_REALTIME_VALUES = frozenset({"realtime", "instant", "immediate", "per-event"})


def alert_delivery_mode() -> str:
    """'weekly' (default) or 'realtime', from __bullish_settings__."""
    from ..models.item import Item

    try:
        row = Item.query.filter_by(title="__bullish_settings__").first()
        if row:
            raw = (json.loads(row.description or "{}").get("alert_delivery") or "").strip().lower()
            if raw in _REALTIME_VALUES:
                return "realtime"
    except Exception as exc:
        logger.warning("alert_delivery lookup failed (%s) — defaulting to weekly", exc)
    return "weekly"


# ─── Alert recipients ─────────────────────────────────────────────────────────

def resolve_alert_emails() -> list:
    """
    Return the alert email list from __bullish_settings__, falling back to the
    ALERT_EMAILS env var.

    Callers should resolve this ONCE per scan batch and pass it to
    process_saved_signal — otherwise every signal costs an extra settings query.
    """
    from ..models.item import Item

    try:
        settings_item = Item.query.filter_by(title="__bullish_settings__").first()
        if settings_item:
            settings = json.loads(settings_item.description or "{}")
            emails = settings.get("alert_emails", [])
            if emails:
                return [e.strip() for e in emails if e and e.strip()]
    except Exception as exc:
        logger.warning("Alert email settings lookup failed: %s", exc)
    env_value = os.environ.get("ALERT_EMAILS", "").strip()
    return [e.strip() for e in env_value.split(",") if e.strip()]


# ─── Stage helpers (individually testable) ────────────────────────────────────

def extract_owner(meta: dict) -> str:
    """
    (b) Resolve the filer/owner name for a signal.

    Prefers an explicit meta['owner'] (collectors that know the owner set it
    directly). Falls back to parsing the "Owner: NAME. <goods and services>"
    prefix that USPTO trademark notes carry — the same parse the manual scan
    path has always done. Returns "" when no owner can be determined.
    """
    explicit = (meta.get("owner") or "").strip()
    if explicit:
        return explicit

    notes = meta.get("notes") or ""
    if notes.lower().startswith("owner:"):
        part = notes[6:].strip()
        dot = part.find(". ")
        return part[:dot].strip() if dot > 0 else part.strip()
    return ""


def resolve_trademark_assignment(meta: dict, owner: str) -> bool:
    """
    (c) Resolve a trademark filed under a stealth entity to its operating entity.

    "Lightyear Inc" files the mark, later assigns it to "Filament Sciences" —
    the assignee is the real brand. Mutates meta with stealth_entity /
    resolved_owner / serial_number. Returns True when meta changed.
    """
    from .trademark_assignments import resolve_brand_name

    assignment = resolve_brand_name(
        original_owner=owner,
        notes=meta.get("notes", ""),
        description=meta.get("description", ""),
    )
    if not (assignment.get("has_assignment") and assignment.get("resolved_name")):
        return False

    meta["stealth_entity"] = assignment["stealth_entity"]
    meta["resolved_owner"] = assignment["resolved_name"]
    meta["serial_number"]  = assignment["serial_number"]
    return True


def match_people(meta: dict, owner: str, fallback_name: str) -> tuple:
    """
    (d) Run conviction matching, then — only when there is no conviction match —
    exit-alumni matching, over [owner, notes, description, company_name].

    The two designations stay separate on purpose: a founder is not an alumnus.
    Returns (conviction_match | None, exit_alumni_match | None).
    """
    from .conviction import check_conviction_match_multi
    from .exit_watch import check_exit_alumni_match_multi

    fields = [
        owner,
        meta.get("notes", ""),
        meta.get("description", ""),
        meta.get("company_name", fallback_name),
    ]
    conviction = check_conviction_match_multi(fields)
    if conviction:
        return conviction, None
    return None, check_exit_alumni_match_multi(fields)


def extract_form_d_related_persons(meta: dict) -> list:
    """
    (e) Fetch the EDGAR Form D XML and return every named Director/Officer with
    their own conviction / alumni match attached.

    Requires meta['_adsh'] and meta['_cik'] — which the scheduled path only began
    persisting in P1. Returns [] when the filing IDs are absent or EDGAR fails.

    Skipped entirely when the collector already attached officers. Since P2,
    delaware.py extracts them for EVERY Form D signal during collection rather
    than the first 50, so re-fetching here would ask SEC for the exact same
    primary_doc.xml a second time and overwrite the result with identical data —
    doubling our request volume against a free public endpoint for nothing. The
    fetch stays as a fallback for signals collected before that change, and for
    the manual per-signal paths.
    """
    from .conviction import check_conviction_match_multi
    from .delaware import fetch_form_d_related_persons as _fetch
    from .exit_watch import check_exit_alumni_match_multi

    already = meta.get("related_persons")
    if already:
        return already

    adsh = meta.get("_adsh", "")
    cik  = meta.get("_cik", "")
    if not adsh or not cik:
        return []

    enriched = []
    for person in _fetch(adsh, cik) or []:
        pname    = person.get("name", "")
        p_conv   = check_conviction_match_multi([pname])
        p_alumni = check_exit_alumni_match_multi([pname]) if not p_conv else None
        enriched.append({
            **person,
            "conviction_match":  p_conv,
            "exit_alumni_match": p_alumni,
        })
    return enriched


def detect_acquisition(meta: dict) -> str | None:
    """
    (f) Look for acquisition language in a press/newswire signal and grow the
    exit-watch brand list, so future signals from that brand's alumni can be
    cross-referenced. Returns the brand name added, or None.
    """
    from .exit_watch import add_exit_brand, detect_acquisition_in_text

    text = (meta.get("description", "") or "") + " " + (meta.get("notes", "") or "")
    acq_brand = detect_acquisition_in_text(text)
    if not acq_brand:
        return None
    add_exit_brand(acq_brand, notes=f"Auto-detected from {meta.get('signal_type', 'press')} signal")
    return acq_brand


def build_person_names(meta: dict) -> list:
    """
    Names offered to confluence for person-key clustering: every Form D related
    person, the filer, and the owner ONLY when it is a natural person.

    The owner needs that gate. confluence.normalize_person rejects a name
    carrying a legal suffix ("Acme Labs LLC") but happily turns a plain company
    name ("Sunset Beverage") into the person key 'sunset beverage' — so feeding
    every trademark owner in would cluster unrelated brands filed by
    similarly-named entities and fire a false multi-signal alert. A wrong merge
    is worse than a missed link, so we trust USPTO's own INDIVIDUAL annotation
    (captured as owner_is_person in trademarks.py) rather than guessing from
    the string. Form D related persons and filer_name are already people by
    construction and need no gate.
    """
    names = [rp["name"] for rp in (meta.get("related_persons") or []) if rp.get("name")]
    if meta.get("filer_name"):
        names.append(meta["filer_name"])
    if meta.get("owner") and meta.get("owner_is_person"):
        names.append(meta["owner"])
    return names


def triage_enabled() -> bool:
    """
    (g) Is the cheap Stage-1 gate switched on?

    Default ON. ENABLE_TRIAGE=0|false|no|off disables Stage 1 entirely and
    restores pre-P2 behaviour: every saved signal goes straight to the full
    Sonnet scoring pass. One env var, no redeploy of logic — because the failure
    mode of a bad gate is silently invisible brands, and that has to be
    revertible in seconds.
    """
    raw = os.environ.get("ENABLE_TRIAGE")
    if raw is None:
        return True
    return raw.strip().lower() not in _TRIAGE_OFF_VALUES


# Triage exists to make ~1,000 trademark filings a day affordable to score. It
# applies to TRADEMARK SIGNALS ONLY, and deliberately so.
#
# Every other collector is low-volume — Form D runs ~110 candidates a WEEK,
# newswire and press tens per week — so gating them saves almost nothing while
# risking the thing the product is for. Form D is Job 1, and a Form D record is
# inherently sparse: the gate would see little more than "NORTHSTAR LABS INC /
# Category: Unknown" and could reasonably reject a genuine first-capital-raise
# filing by a real consumer brand. That is the most expensive possible mistake
# here, and no amount of prompt tuning makes a cheap model reliable on a record
# that carries almost no text.
_TRIAGEABLE_SIGNAL_TYPES = frozenset({"trademark"})


def triage_bypass_reason(conviction, alumni, conf_result: dict,
                         signal_type: str = "trademark") -> str | None:
    """
    (g) Should this signal skip the gate and go straight to full scoring?

    Bypassed when:
      • not_triageable    — anything other than a trademark. See the note above:
                            triage buys volume relief on trademarks and nothing
                            else, and Form D (Job 1) must never be gated.
      • conviction_match  — a Bullish conviction-list founder is named. Job 2.
      • exit_alumni_match — a tracked exit operator is named. Job 2.
      • multi_signal      — confluence has seen this brand in 2+ DISTINCT signal
                            types (confluence's signal_count is len(distinct
                            types), not an event count). Triangulation is the
                            core edge; a triangulated brand is real by
                            definition and gets scored regardless.

    Returns the reason string, or None when the signal should be triaged.
    """
    if (signal_type or "") not in _TRIAGEABLE_SIGNAL_TYPES:
        return "not_triageable"
    if conviction:
        return "conviction_match"
    if alumni:
        return "exit_alumni_match"

    # Fail OPEN on a malformed or missing confluence result. If the confluence
    # stage raised, conf_result is still the default {signal_count: 1,
    # signal_types: []} — which would read as "solo signal" and let the gate
    # withhold scoring from a brand that may well be triangulated. When we do
    # not KNOW it is solo, score it.
    if not conf_result or conf_result.get("error"):
        return "confluence_unknown"

    try:
        signal_count = int(conf_result.get("signal_count") or 1)
    except (TypeError, ValueError):
        return "confluence_unknown"
    distinct_types = len({t for t in (conf_result.get("signal_types") or []) if t})

    if signal_count >= 2 or distinct_types >= 2:
        return "multi_signal"
    return None


def build_scoring_payload(meta: dict, fallback_title: str, owner: str = "",
                         conviction: dict = None, signal_types: list = None,
                         signal_count: int = None) -> dict:
    """
    THE payload handed to enrich_signal. Every caller must use this.

    enrichment.py reads `signal_types` for its tier floors ("never bury a
    trademark or Form D as cold") and `signal_count` for the multi-signal prompt
    boost. A caller that omits them silently disables both — which is what the
    two /api/enrich routes were doing, the third recurrence of the dual-path
    divergence this codebase keeps producing. Building the payload in one place
    is the control; tests/test_p3_enrich_payload_parity.py is the drift alarm.
    """
    sig_type = meta.get("signal_type", "trademark")
    return {
        "companyName":      meta.get("resolved_owner") or meta.get("company_name", fallback_title),
        "category":         meta.get("category", ""),
        "signal_type":      sig_type,
        "description":      meta.get("description", ""),
        "notes":            meta.get("notes", ""),
        "owner":            meta.get("resolved_owner") or owner or "",
        "conviction_match": conviction if conviction is not None else meta.get("conviction_match"),
        "signal_types":     signal_types or [sig_type],
        "signal_count":     signal_count or 1,
    }


def signal_types_for_brand(owner_id: int, brand_name: str) -> tuple:
    """
    (signal_types, count) already recorded for a brand — so an on-demand
    re-enrich gets the same tier floors a scan-time enrich would.
    """
    from .confluence import normalize_brand
    from ..models.signal_event import SignalEvent

    try:
        key = normalize_brand(brand_name)
        if not key:
            return [], 1
        rows = (SignalEvent.query
                .filter_by(owner_id=owner_id, brand_key=key)
                .with_entities(SignalEvent.signal_type).all())
        types = sorted({r.signal_type for r in rows if r.signal_type})
        return types, max(1, len(types))
    except Exception as exc:
        logger.warning("signal_types lookup failed for %r: %s", brand_name, exc)
        return [], 1


def founder_enrichment_threshold(signal_type: str) -> int:
    """Score floor above which a brand is worth spending founder-discovery budget on."""
    if signal_type == "newswire":
        return FOUNDER_ENRICHMENT_MIN_SCORE_NEWSWIRE
    return FOUNDER_ENRICHMENT_MIN_SCORE


def should_run_founder_enrichment(enrichment: dict, signal_type: str) -> bool:
    """
    (k) Union of what the two paths did before:
      - scheduled path: any HOT brand
      - manual path:    a score of at least FOUNDER_ENRICHMENT_MIN_SCORE_NEWSWIRE
                        for newswire, else FOUNDER_ENRICHMENT_MIN_SCORE

    HOT is kept as an independent trigger because enrichment.py's tier floors can
    force watch_level='hot' on a conviction match whose raw score is below the
    numeric floor — exactly the case Job 2 exists for.
    """
    if not enrichment.get("enriched"):
        return False
    if enrichment.get("watch_level") == "hot":
        return True
    try:
        score = float(enrichment.get("bullish_score") or 0)
    except (TypeError, ValueError):
        return False
    return score >= founder_enrichment_threshold(signal_type)


# ─── Main entry point ─────────────────────────────────────────────────────────

def process_saved_signal(item_id: int, owner_id: int = None, alert_emails: list = None) -> dict:
    """
    Run the full post-save pipeline for ONE already-persisted signal item.

    owner_id:     defaults to item.owner_id. Confluence clustering is owner-scoped.
    alert_emails: resolved from settings/env when omitted. Pass it in from a batch
                  loop to avoid a settings query per signal.

    Returns a bookkeeping dict for the caller's counters:
        {
          "item_id", "company_name", "category", "owner",
          "enriched", "watch_level", "bullish_score",
          "one_line_thesis", "cultural_theme",
          "conviction_match", "exit_alumni_match",
          "triage": {...} | None,        # Stage-1 verdict, when the gate ran
          "triage_skipped": bool,        # True when Stage 2 was skipped
          "confluence": {...}, "confluence_alert_sent", "watchlist_alert_sent",
          "founder_queued", "watchlist_queued", "errors": [str, ...]
        }

    A triaged-out signal returns enriched=False, watch_level=None and
    bullish_score=None — the same shape as an enrichment failure, which every
    caller already handles (both scan paths `continue` on `not enriched`). It is
    unscored, NOT cold: it is deliberately absent from the hot/warm/cold counts
    rather than being counted as a rejection.
    """
    from ..extensions import db
    from ..models.item import Item

    result = {
        "item_id":               item_id,
        "company_name":          "",
        "category":              "",
        "owner":                 "",
        "enriched":              False,
        "watch_level":           None,
        "bullish_score":         None,
        "one_line_thesis":       "",
        "cultural_theme":        "",
        "conviction_match":      None,
        "exit_alumni_match":     None,
        "triage":                None,
        "triage_skipped":        False,
        "confluence":            {"is_confluence": False, "signal_count": 1, "signal_types": [], "hit_id": None},
        "confluence_alert_sent": False,
        "watchlist_alert_sent":  False,
        "founder_queued":        False,
        "watchlist_queued":      False,
        "errors":                [],
    }

    def _fail(stage: str, exc) -> None:
        """
        Record a stage failure without aborting the pipeline.

        Rolls the session back first. A stage that raised AFTER a flush leaves
        the session in PendingRollbackError, and every later stage touches that
        session — on the manual path every later SIGNAL does too, since the whole
        batch shares one app context. Without this, one bad row silently takes
        out enrichment, confluence, people matching and alerts for everything
        behind it in the batch.
        """
        try:
            db.session.rollback()
        except Exception:
            pass
        result["errors"].append(f"{stage}: {exc}")
        logger.warning("Pipeline stage '%s' failed for item %s: %s", stage, item_id, exc)

    # ── (a) Load and parse ────────────────────────────────────────────────────
    item = db.session.get(Item, item_id)
    if not item:
        _fail("load", "item no longer exists")
        return result
    try:
        meta = json.loads(item.description or "{}")
    except Exception as exc:
        _fail("load", exc)
        return result
    if meta.get("_type") != "signal":
        result["errors"].append("load: not a signal item")
        return result

    if owner_id is None:
        owner_id = item.owner_id
    if alert_emails is None:
        alert_emails = resolve_alert_emails()

    sig_type = meta.get("signal_type", "trademark")
    result["company_name"] = meta.get("company_name", item.title)
    result["category"]     = meta.get("category", "")

    def _save() -> None:
        item.description = json.dumps(meta, separators=(",", ":"))
        db.session.commit()

    # ── (b) Owner extraction ──────────────────────────────────────────────────
    owner = ""
    try:
        owner = extract_owner(meta)
        result["owner"] = owner
        # Persist the derived owner so confluence can build a person key from it.
        # Trademark owners were parsed into a local variable only, so a trademark
        # and a Form D naming the same person never clustered.
        if owner and not meta.get("owner"):
            meta["owner"] = owner
            _save()
    except Exception as exc:
        _fail("owner_extraction", exc)

    # ── (c) Trademark assignment resolution ───────────────────────────────────
    if sig_type == "trademark" and owner:
        try:
            if resolve_trademark_assignment(meta, owner):
                _save()
                logger.info(
                    "Assignment resolved: '%s' → '%s' for item %s",
                    meta.get("stealth_entity"), meta.get("resolved_owner"), item_id,
                )
        except Exception as exc:
            _fail("trademark_assignment", exc)

    # ── (d) Conviction, then exit-alumni matching ─────────────────────────────
    conviction = meta.get("conviction_match")
    alumni     = meta.get("exit_alumni_match")
    try:
        matched_conviction, matched_alumni = match_people(meta, owner, item.title)
        if matched_conviction and not conviction:
            conviction = matched_conviction
            meta["conviction_match"] = conviction
            _save()
            logger.info(
                "Conviction match: '%s' in signal '%s' (item %s)",
                conviction["name"], meta.get("company_name"), item_id,
            )
        elif matched_alumni and not conviction and not alumni:
            alumni = matched_alumni
            meta["exit_alumni_match"] = alumni
            _save()
            logger.info(
                "Exit alumni match: '%s' in signal '%s' (item %s)",
                alumni["name"], meta.get("company_name"), item_id,
            )
    except Exception as exc:
        _fail("people_matching", exc)

    # ── (e) Form D related persons ────────────────────────────────────────────
    if sig_type == "delaware":
        try:
            related = extract_form_d_related_persons(meta)
            if related:
                meta["related_persons"] = related
                _save()
                logger.info("Form D related persons: %d found for item %s", len(related), item_id)

                # Promote a named officer's match to the top level only when the
                # signal itself has no match yet — founder outranks alumni.
                #
                # Precedence is across the WHOLE officer list, not per-officer:
                # scan every related person for a conviction match first, and
                # only fall through to alumni if none exists. Breaking on the
                # first officer carrying EITHER match let an alumni listed above
                # a conviction founder win, which silently downgrades the
                # strongest signal this product has. Mirrors the same rule in
                # delaware.py::_enrich_related_persons — keep them in step.
                #
                # Gated on `not conviction` ALONE, not `not conviction and not
                # alumni`. Conviction outranks alumni, so an alumni match found
                # by stage (d)'s text scan must NOT block a genuine conviction
                # founder named among the officers — that inverted the ranking
                # and buried the strongest signal behind the weaker one.
                if not conviction:
                    _top_conv = next(
                        (rp["conviction_match"] for rp in related if rp.get("conviction_match")),
                        None,
                    )
                    if _top_conv:
                        conviction = _top_conv
                        meta["conviction_match"] = conviction
                        # Conviction displaces a weaker alumni match so the two
                        # designations never co-exist on one signal with the
                        # lesser one presented first.
                        if alumni:
                            alumni = None
                            meta.pop("exit_alumni_match", None)
                        _save()
                        logger.info(
                            "Related person conviction: '%s' promoted for item %s",
                            conviction["name"], item_id,
                        )
                    elif not alumni:
                        _top_alumni = next(
                            (rp["exit_alumni_match"] for rp in related if rp.get("exit_alumni_match")),
                            None,
                        )
                        if _top_alumni:
                            alumni = _top_alumni
                            meta["exit_alumni_match"] = alumni
                            _save()
                            logger.info(
                                "Related person alumni: '%s' promoted for item %s",
                                alumni["name"], item_id,
                            )
        except Exception as exc:
            _fail("form_d_related_persons", exc)

    result["conviction_match"]  = conviction
    result["exit_alumni_match"] = alumni

    # ── (f) Acquisition detection ─────────────────────────────────────────────
    if sig_type in ("press_stealth", "newswire"):
        try:
            acq_brand = detect_acquisition(meta)
            if acq_brand:
                logger.info(
                    "Exit watch: auto-added '%s' from %s (item %s)", acq_brand, sig_type, item_id,
                )
        except Exception as exc:
            _fail("acquisition_detection", exc)

    # ── (g) Confluence FIRST, then enrichment ─────────────────────────────────
    # enrich_signal() reads signal["signal_types"] for its tier floors and
    # signal_count >= 2 for the confluence prompt boost. Running confluence
    # afterwards (as both paths used to) left both permanently unset.
    conf_result = result["confluence"]
    try:
        from .confluence import (
            extract_coined_term_keys,
            extract_person_keys,
            record_signal_and_check_confluence,
        )
        conf_result = record_signal_and_check_confluence(
            item_id=item_id,
            owner_id=owner_id,
            brand_name=meta.get("company_name") or item.title,
            signal_type=sig_type,
            source_url=meta.get("url"),
            person_keys=extract_person_keys(build_person_names(meta)),
            coined_term_keys=extract_coined_term_keys(meta.get("company_name") or item.title),
        )
        result["confluence"] = conf_result
    except Exception as exc:
        _fail("confluence_record", exc)
        # Flag the failure so the triage bypass can fail OPEN. Without this the
        # default {signal_count: 1} reads as a confirmed solo signal and the gate
        # could withhold scoring from a brand that is actually triangulated.
        conf_result = dict(conf_result or {})
        conf_result["error"] = str(exc)
        result["confluence"] = conf_result

    # The payload is built once and handed to BOTH stages, so the gate and the
    # scorer can never be looking at different text for the same signal.
    scoring_payload = build_scoring_payload(
        meta, item.title, owner=owner, conviction=conviction,
        signal_types=conf_result.get("signal_types"),
        signal_count=conf_result.get("signal_count"),
    )

    # ── (g2) TRIAGE — the cheap gate in front of the expensive scorer ──────────
    # Any failure here leaves worth_scoring True. Coverage never shrinks because
    # a gate broke; the whole point of P2 is that nothing goes unseen.
    triage = None
    bypass = triage_bypass_reason(conviction, alumni, conf_result, sig_type)
    if bypass:
        # Rare and meaningful — a high-value signal skipping the gate is worth
        # seeing in the log.
        logger.info("Triage bypassed for item %s (%s) — full scoring", item_id, bypass)
    elif not triage_enabled():
        # Applies to every signal in the batch, so debug rather than a per-signal
        # INFO line across a thousand-signal nightly scan.
        bypass = "disabled"
        logger.debug("Triage disabled (ENABLE_TRIAGE) — full scoring for item %s", item_id)
    if not bypass:
        try:
            from .enrichment import triage_signal
            triage = triage_signal(scoring_payload) or {}
        except Exception as exc:
            # triage_signal is written never to raise; if it somehow does, that
            # must still not cost coverage.
            _fail("triage", exc)
            triage = None
        if triage is not None:
            triage["at"] = datetime.now(timezone.utc).isoformat()
            meta["triage"] = triage
            result["triage"] = triage

    if triage is not None and not triage.get("worth_scoring", True):
        # Triaged out. The row STAYS in the database, visible on the dashboard,
        # with a record of why it was not scored — it is un-enriched, not hidden.
        # `flask re-enrich` and the /api/enrich batch endpoint both re-score it
        # on demand, so a wrong gate call is one command away from being undone.
        result["triage_skipped"] = True
        try:
            _save()
        except Exception as exc:
            _fail("triage_persist", exc)
        logger.info(
            "Triaged out %s (item %s): %s [confidence=%s]",
            result["company_name"], item_id,
            triage.get("reason", ""), triage.get("confidence", ""),
        )

    enrichment = {}
    if not result["triage_skipped"]:
        try:
            from .enrichment import enrich_signal
            enrichment = enrich_signal(scoring_payload) or {}
        except Exception as exc:
            _fail("enrichment", exc)

    # ── (h) Persist the enrichment ────────────────────────────────────────────
    if enrichment.get("enriched"):
        try:
            meta["enrichment"] = enrichment
            _save()
            result["enriched"]        = True
            result["watch_level"]     = enrichment.get("watch_level")
            result["bullish_score"]   = enrichment.get("bullish_score")
            result["one_line_thesis"] = enrichment.get("one_line_thesis", "")
            result["cultural_theme"]  = enrichment.get("cultural_theme", "")
            logger.info(
                "Auto-enriched %s (item %s) → %s / score %s",
                meta.get("company_name"), item_id,
                enrichment.get("watch_level"), enrichment.get("bullish_score"),
            )
        except Exception as exc:
            _fail("enrichment_persist", exc)

    # ── (i) Confluence alert — after enrichment, score-gated ──────────────────
    if conf_result.get("is_confluence") and conf_result.get("hit_id"):
        try:
            from .confluence import send_confluence_alert_for_hit, should_send_confluence_alert
            _backfill_hit_score(conf_result["hit_id"], enrichment)
            _conf_score = result["bullish_score"]
            if alert_delivery_mode() != "realtime":
                # Weekly mode (the default — Brent, 2026-07-30: one big Monday
                # email): do not send now. The hit keeps alert_sent=False, which
                # is exactly the queue the Monday digest drains. Nothing is
                # dropped — only deferred and batched.
                logger.info(
                    "Confluence hit for %s deferred to the Monday digest",
                    result["company_name"],
                )
            elif should_send_confluence_alert(_conf_score):
                if alert_emails:
                    send_confluence_alert_for_hit(conf_result["hit_id"], alert_emails)
                    result["confluence_alert_sent"] = True
                    logger.info(
                        "Confluence alert sent for %s (score=%s, %d signals)",
                        result["company_name"], _conf_score, conf_result.get("signal_count", 0),
                    )
            else:
                logger.info(
                    "Confluence detected, email suppressed for %s (score=%s) — below threshold or unscored",
                    result["company_name"], _conf_score,
                )
        except Exception as exc:
            _fail("confluence_alert", exc)

    # ── (j) Immediate watchlist-match alert ───────────────────────────────────
    # Fires the moment a signal names someone from the 193-person conviction /
    # alumni watchlist. No press article needed — the earliest possible signal.
    if conviction or alumni:
        try:
            from .email import send_watchlist_match_alert
            if alert_delivery_mode() != "realtime":
                # Weekly mode: the match is already persisted on the signal
                # (conviction_match / exit_alumni_match), which is what the
                # digest's People section reads. Defer, don't drop.
                logger.info(
                    "Watchlist match (%s) for %s deferred to the Monday digest",
                    match_type, result["company_name"],
                )
                raise _DeferAlert()
            match      = conviction or alumni
            match_type = "CONVICTION" if conviction else "ALUMNI"
            for addr in alert_emails:
                try:
                    send_watchlist_match_alert(
                        addr,
                        person_name=match.get("name", "Unknown"),
                        match_type=match_type,
                        brand_name=result["company_name"],
                        signal_type=sig_type,
                        brand_score=result["bullish_score"],
                        watch_level=result["watch_level"],
                        thesis=result["one_line_thesis"],
                        match_details=match,
                    )
                    result["watchlist_alert_sent"] = True
                    logger.info(
                        "Watchlist match alert sent for %s (%s match: %s)",
                        result["company_name"], match_type, match.get("name"),
                    )
                except Exception as exc:
                    logger.warning("Watchlist match alert failed to %s: %s", addr, exc)
        except _DeferAlert:
            pass
        except Exception as exc:
            _fail("watchlist_match_alert", exc)

    # ── (k) HOT handling — founder enrichment + auto-watchlist ────────────────
    if should_run_founder_enrichment(enrichment, sig_type):
        try:
            from .founder_enrichment import run_founder_enrichment_in_background
            run_founder_enrichment_in_background(
                app=current_app._get_current_object(),
                item_id=item_id,
                brand_name=result["company_name"],
                category=result["category"],
                one_line_thesis=result["one_line_thesis"],
                filer_name=meta.get("filer_name") or owner or None,
                alert_emails=alert_emails or None,
            )
            result["founder_queued"] = True
            logger.info(
                "Triggering founder enrichment for '%s' (item %s, level=%s, score=%s)",
                result["company_name"], item_id, result["watch_level"], result["bullish_score"],
            )
        except Exception as exc:
            _fail("founder_enrichment", exc)

    if result["watch_level"] == "hot":
        try:
            from .watchlist import auto_add_to_watchlist
            _founder = enrichment.get("founder") or {}
            threading.Thread(
                target=auto_add_to_watchlist,
                args=(
                    current_app._get_current_object().app_context(),
                    owner_id,
                    result["company_name"],
                    item_id,
                    result["bullish_score"],
                    _founder.get("name") if _founder.get("confidence") != "unknown" else None,
                    (enrichment.get("founder_score") or {}).get("total"),
                    _founder.get("linkedin_url"),
                    result["one_line_thesis"],
                    result["cultural_theme"],
                    [sig_type],
                ),
                daemon=True,
            ).start()
            result["watchlist_queued"] = True
        except Exception as exc:
            _fail("auto_watchlist", exc)

    return result


def _backfill_hit_score(hit_id: int, enrichment: dict) -> None:
    """
    Stamp the enrichment score onto an already-created ConfluenceHit.

    Confluence now runs BEFORE enrichment so signal_types/signal_count can reach
    enrich_signal, which means the hit row is written with no score. The alert
    email and the dashboard both read hit.bullish_score, so backfill it here
    rather than leaving every hit unscored.
    """
    if not enrichment.get("enriched"):
        return
    from ..extensions import db
    from ..models.confluence_hit import ConfluenceHit

    hit = db.session.get(ConfluenceHit, hit_id)
    if not hit:
        return
    hit.bullish_score = enrichment.get("bullish_score")
    hit.watch_level   = enrichment.get("watch_level")
    db.session.commit()
