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
  (g) CONFLUENCE, then ENRICH — enrichment.py's tier floors read signal["signal_types"]
                                and its confluence prompt boost reads signal_count >= 2.
                                Neither scan path passed them before, so the
                                "never bury a trademark or Form D as cold" floor and
                                the multi-signal boost were dead code at scan time.
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

from flask import current_app

logger = logging.getLogger(__name__)

# Founder enrichment triggers. A newswire brand has just broken cover, so it is
# the highest-urgency moment to identify the founder — hence the lower floor.
FOUNDER_ENRICHMENT_MIN_SCORE = 70
FOUNDER_ENRICHMENT_MIN_SCORE_NEWSWIRE = 50


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
    """
    from .conviction import check_conviction_match_multi
    from .delaware import fetch_form_d_related_persons as _fetch
    from .exit_watch import check_exit_alumni_match_multi

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
          "confluence": {...}, "confluence_alert_sent", "watchlist_alert_sent",
          "founder_queued", "watchlist_queued", "errors": [str, ...]
        }
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
        "confluence":            {"is_confluence": False, "signal_count": 1, "signal_types": [], "hit_id": None},
        "confluence_alert_sent": False,
        "watchlist_alert_sent":  False,
        "founder_queued":        False,
        "watchlist_queued":      False,
        "errors":                [],
    }

    def _fail(stage: str, exc) -> None:
        """Record a stage failure without aborting the pipeline."""
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
                if not conviction and not alumni:
                    _top_conv = next(
                        (rp["conviction_match"] for rp in related if rp.get("conviction_match")),
                        None,
                    )
                    if _top_conv:
                        conviction = _top_conv
                        meta["conviction_match"] = conviction
                        _save()
                        logger.info(
                            "Related person conviction: '%s' promoted for item %s",
                            conviction["name"], item_id,
                        )
                    else:
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

    enrichment = {}
    try:
        from .enrichment import enrich_signal
        enrichment = enrich_signal({
            "companyName":      meta.get("resolved_owner") or meta.get("company_name", item.title),
            "category":         meta.get("category", ""),
            "signal_type":      sig_type,
            "description":      meta.get("description", ""),
            "notes":            meta.get("notes", ""),
            "owner":            meta.get("resolved_owner") or owner,
            "conviction_match": conviction,
            "signal_types":     conf_result.get("signal_types") or [sig_type],
            "signal_count":     conf_result.get("signal_count") or 1,
        }) or {}
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
            if should_send_confluence_alert(_conf_score):
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
