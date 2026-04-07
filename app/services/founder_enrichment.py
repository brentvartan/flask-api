"""
Founder Enrichment Orchestration Service

Ties together:
  - founder_discovery  → identify who the founder is
  - proxycurl          → fetch their LinkedIn profile
  - enrichment         → rescore_founder_with_linkedin (Claude Haiku)
  - crunchbase         → optional company data (if CRUNCHBASE_API_KEY is set)
  - DB update          → persist results to item.meta["enrichment"]["founder"]

Entry points:
  run_founder_enrichment()              — synchronous, returns result dict
  run_founder_enrichment_in_background() — spawns a daemon thread with app context
"""
import json
import logging
import threading

logger = logging.getLogger(__name__)

_founder_semaphore = threading.Semaphore(3)  # max 3 concurrent founder enrichments


def run_founder_enrichment(
    item_id: int,
    brand_name: str,
    category: str,
    one_line_thesis: str,
    filer_name: str = None,
) -> dict:
    """
    Full founder enrichment pipeline for a single signal item.

    Flow:
      1. discover_founder  → who is the founder? (includes exit background search)
      2. proxycurl: use linkedin_url_hint if available, else search → fetch LinkedIn profile
      3. Inject exit background + Crunchbase data into profile text
      4. rescore_founder_with_linkedin  → score via Claude Haiku
      5. Persist results to DB

    Returns a result dict:
      {"enriched": True,  "founder_name": str, "founder_score": int,
       "tier": str, "linkedin_url": str}
    or
      {"enriched": False, "reason": str, ...}
    """
    from ..extensions import db
    from ..models.item import Item
    from .founder_discovery import discover_founder
    from . import proxycurl
    from .enrichment import rescore_founder_with_linkedin
    from .crunchbase import lookup_company, is_available as crunchbase_available

    # ── 1. Discover founder ───────────────────────────────────────────────────
    discovery = discover_founder(brand_name, filer_name, category)
    founder_name = discovery.get("name")

    if not founder_name:
        logger.info(
            "Founder enrichment: no founder identified for '%s' (item %s)",
            brand_name, item_id,
        )
        return {"enriched": False, "reason": "founder_not_found"}

    logger.info(
        "Founder enrichment: identified '%s' as founder of '%s' (source=%s, confidence=%s)",
        founder_name, brand_name, discovery.get("source"), discovery.get("confidence"),
    )

    # ── 2. Fetch LinkedIn profile ─────────────────────────────────────────────
    linkedin_url_hint = discovery.get("linkedin_url_hint")
    linkedin_data = None

    if linkedin_url_hint:
        # Use the hint directly — saves a Proxycurl search credit
        logger.info(
            "Founder enrichment: using linkedin_url_hint for '%s': %s",
            founder_name, linkedin_url_hint,
        )
        profile_raw = proxycurl.fetch_linkedin_profile(linkedin_url_hint)
        if profile_raw:
            linkedin_data = profile_raw
            linkedin_data["found"] = True

    if not linkedin_data or not linkedin_data.get("found"):
        # Fall back to full Proxycurl search
        linkedin_data = proxycurl.enrich_founder(founder_name, brand_name)

    # enrich_founder returns {"found": False} if nothing was found
    if not linkedin_data or not linkedin_data.get("found"):
        logger.info(
            "Founder enrichment: no LinkedIn profile found for '%s' (item %s)",
            founder_name, item_id,
        )
        return {
            "enriched":      False,
            "reason":        "linkedin_not_found",
            "founder_name":  founder_name,
        }

    linkedin_url = linkedin_data.get("linkedin_url") or linkedin_url_hint or ""

    # ── 3. Build augmented profile text with exit background + Crunchbase ─────
    # Pass a modified linkedin_context that includes exit background and Crunchbase
    # We use a shallow copy and inject extra fields for rescore_founder_with_linkedin
    augmented_context = dict(linkedin_data)

    # Exit background from discovery
    exit_info = discovery.get("exit_background", {})
    if exit_info.get("has_exit_background") and exit_info.get("details"):
        augmented_context["_exit_background_text"] = f"BRAND EXIT BACKGROUND: {exit_info['details']}"
    else:
        augmented_context["_exit_background_text"] = "BRAND EXIT BACKGROUND: No prior exit background found."

    # Crunchbase data (optional)
    crunchbase_text = ""
    _crunchbase_enriched = False
    if crunchbase_available():
        try:
            cb_data = lookup_company(brand_name)
            if cb_data:
                _crunchbase_enriched = True
                crunchbase_text = f"\n\nCRUNCHBASE: {cb_data.get('description', '')}. "
                if cb_data.get("total_funding"):
                    crunchbase_text += f"Total funding: ${cb_data['total_funding']:,.0f}. "
                if cb_data.get("last_funding_type"):
                    crunchbase_text += f"Last round: {cb_data['last_funding_type']}."
        except Exception as exc:
            logger.warning("Crunchbase enrichment failed for '%s': %s", brand_name, exc)

    augmented_context["_crunchbase_text"] = crunchbase_text

    # ── 4. Rescore with Claude ────────────────────────────────────────────────
    rescore = rescore_founder_with_linkedin(
        brand_name=brand_name,
        category=category,
        one_line_thesis=one_line_thesis,
        founder_name=founder_name,
        linkedin_context=augmented_context,
        discovery_result=discovery,
    )

    if not rescore.get("linkedin_enriched"):
        error_msg = rescore.get("error", "unknown error")
        logger.warning(
            "Founder enrichment: rescore failed for '%s' (item %s): %s",
            founder_name, item_id, error_msg,
        )
        return {
            "enriched":      False,
            "reason":        f"rescore_failed: {error_msg}",
            "founder_name":  founder_name,
        }

    founder_section = rescore.get("founder", {})
    score_section   = rescore.get("founder_score", {})
    total           = score_section.get("total", 0)
    tier            = score_section.get("tier", "PASS")

    logger.info(
        "Founder enrichment: scored '%s' → %d (%s) for '%s' (item %s)",
        founder_name, total, tier, brand_name, item_id,
    )

    # ── 5. Persist to DB ──────────────────────────────────────────────────────
    try:
        item = db.session.get(Item, item_id)
        if item:
            meta = json.loads(item.description or "{}")
            if "enrichment" not in meta:
                meta["enrichment"] = {}

            if _crunchbase_enriched:
                meta["crunchbase_enriched"] = True

            # Merge the founder data into meta.enrichment
            meta["enrichment"]["founder"] = {
                **(founder_section or {}),
                "name":        founder_name,
                "confidence":  "known",
                "linkedin_url": linkedin_url,
                "discovery_source": discovery.get("source"),
                "exit_background": exit_info,
            }
            meta["enrichment"]["founder_score"] = score_section

            item.description = json.dumps(meta, separators=(",", ":"))
            db.session.commit()
            logger.info(
                "Founder enrichment: persisted results for item %s ('%s')",
                item_id, brand_name,
            )
        else:
            logger.warning("Founder enrichment: item %s not found in DB", item_id)
    except Exception as exc:
        logger.warning("Founder enrichment: DB update failed for item %s: %s", item_id, exc)

    return {
        "enriched":      True,
        "founder_name":  founder_name,
        "founder_score": total,
        "tier":          tier,
        "linkedin_url":  linkedin_url,
        "breakdown":     score_section.get("breakdown", {}),
    }


def run_founder_enrichment_in_background(
    app,
    item_id: int,
    brand_name: str,
    category: str,
    one_line_thesis: str,
    filer_name: str = None,
    alert_emails: list = None,
) -> None:
    """
    Spawn a daemon thread to run founder enrichment with an app context.

    If enrichment succeeds and founder_score >= 75, sends a founder alert email
    to each address in alert_emails.
    """
    def _run():
        with _founder_semaphore:
            with app.app_context():
                try:
                    result = run_founder_enrichment(
                        item_id=item_id,
                        brand_name=brand_name,
                        category=category,
                        one_line_thesis=one_line_thesis,
                        filer_name=filer_name,
                    )

                    if not result.get("enriched"):
                        logger.info(
                            "Background founder enrichment: not enriched for item %s (%s): %s",
                            item_id, brand_name, result.get("reason"),
                        )
                        return

                    founder_score = result.get("founder_score", 0)
                    logger.info(
                        "Background founder enrichment complete: item %s ('%s') → score=%d tier=%s",
                        item_id, brand_name, founder_score, result.get("tier"),
                    )

                    # Fire alert if score is HIGH_PRIORITY (>= 75)
                    if founder_score >= 75 and alert_emails:
                        _send_alert(result, brand_name, item_id, alert_emails)

                except Exception as exc:
                    logger.warning(
                        "Background founder enrichment failed for item %s ('%s'): %s",
                        item_id, brand_name, exc,
                    )

    threading.Thread(target=_run, daemon=True).start()


def _send_alert(result: dict, brand_name: str, item_id: int, alert_emails: list) -> None:
    """
    Fire send_founder_alert() for each email address in alert_emails.
    Loads the item from DB to get brand_score and watch_level.
    """
    import json as _json
    from ..extensions import db
    from ..models.item import Item
    from .email import send_founder_alert

    brand_score  = None
    watch_level  = None
    one_line_thesis = ""

    try:
        item = db.session.get(Item, item_id)
        if item:
            meta        = _json.loads(item.description or "{}")
            enrichment  = meta.get("enrichment", {})
            brand_score = enrichment.get("bullish_score")
            watch_level = enrichment.get("watch_level")
            one_line_thesis = enrichment.get("one_line_thesis", "")
    except Exception as exc:
        logger.warning("Founder alert: could not load brand metadata for item %s: %s", item_id, exc)

    for addr in alert_emails:
        try:
            send_founder_alert(
                to_email=addr,
                brand_name=brand_name,
                founder_name=result.get("founder_name", ""),
                founder_score=result.get("founder_score", 0),
                founder_tier=result.get("tier", ""),
                brand_score=brand_score,
                watch_level=watch_level,
                linkedin_url=result.get("linkedin_url", ""),
                breakdown=result.get("breakdown", {}),
            )
            logger.info(
                "Founder alert sent to %s for '%s' (score=%d)",
                addr, brand_name, result.get("founder_score", 0),
            )
        except Exception as exc:
            logger.warning("Founder alert email failed to %s: %s", addr, exc)
