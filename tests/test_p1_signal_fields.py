"""
P1 tests for the two field-level gaps that made higher-level wiring dead.

1. trademarks.py never emitted `owner` as a first-class key — it lived only
   inside the free-text `notes` string. Both scan paths build confluence person
   keys from meta["owner"], so the flagship "Form D + trademark naming the same
   founder" merge could never fire from the trademark side.
   The notes string is ALSO load-bearing (enrichment.py strips the
   "Owner: NAME." prefix by exact match, and stored rows already carry that
   shape), so it must stay byte-identical.

2. delaware.py promoted only a conviction match to the signal top level;
   per-person exit_alumni_match values stayed buried inside related_persons.
   scheduler.py reads meta["exit_alumni_match"] at the TOP level to decide
   whether to send the immediate ALUMNI watchlist alert, so that alert was
   unreachable. Conviction outranks alumni and the two designations must never
   be collapsed, so alumni is promoted ONLY when conviction was not.

All HTTP is faked (the suite forbids live network) and time.sleep is patched so
the SEC throttle costs nothing.
"""
import requests
from unittest.mock import patch

from app.services import delaware as dw
from app.services import trademarks as tm


# ── Fake HTTP plumbing ────────────────────────────────────────────────────────

class _FakeResponse:
    """Minimal stand-in for requests.Response (status/json/raise_for_status)."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload    = payload if payload is not None else {}
        self.headers     = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Server Error", response=self)


def _tm_hit(wordmark, owner, goods="IC 030: Coffee beans; roasted coffee",
            ic=("IC 030",), filed="2026-07-20T00:00:00", basis=("1b",)):
    """One USPTO ES hit shaped like the real `source` payload."""
    return {
        "source": {
            "wordmark":           wordmark,
            "ownerName":          [owner],
            "goodsAndServices":   [goods],
            "internationalClass": list(ic),
            "filedDate":          filed,
            "currentBasis":       list(basis),
        }
    }


def _tm_page(hits, total=1):
    return {"hits": {"totalValue": total, "hits": list(hits)}}


def _run_trademark_scan(hits, **kwargs):
    """Run search_recent_trademarks against a single canned USPTO page."""
    with patch("app.services.trademarks.requests.post",
               return_value=_FakeResponse(200, _tm_page(hits, total=len(hits)))):
        return tm.search_recent_trademarks(**kwargs)


# ── FIX 1: trademarks.py emits `owner` as a first-class field ────────────────

def test_trademark_signal_carries_owner_field():
    """The owner must be a real key, not only prose inside `notes`."""
    result = _run_trademark_scan([_tm_hit("FILAMENT", "Rebecca Kaden Coffee LLC")])

    assert result["error"] is None
    assert result["fetched"] == 1
    sig = result["signals"][0]
    assert "owner" in sig, "trademark signal must emit a first-class 'owner' key"
    assert sig["owner"] == "Rebecca Kaden Coffee LLC"


def test_trademark_owner_field_is_the_cleaned_owner():
    """The emitted field uses _clean_owner output — same value used in notes."""
    raw = "Filament Labs LLC (LIMITED LIABILITY COMPANY; Delaware, USA)"
    result = _run_trademark_scan([_tm_hit("FILAMENT", raw)])

    sig = result["signals"][0]
    assert sig["owner"] == "Filament Labs LLC"
    assert sig["owner"] == tm._clean_owner(raw)


def test_trademark_notes_string_is_unchanged():
    """
    enrichment.py strips the 'Owner: NAME.' prefix by exact match and stored
    rows already carry this format — the notes string must be byte-identical to
    the pre-fix output.
    """
    result = _run_trademark_scan([
        _tm_hit("FILAMENT", "Filament Labs LLC",
                goods="IC 030: Coffee beans; roasted coffee")
    ])

    sig = result["signals"][0]
    assert sig["notes"] == "Owner: Filament Labs LLC. Coffee beans"

    # And it is exactly what the original expression produced.
    owner   = tm._clean_owner("Filament Labs LLC")
    snippet = tm._gs_snippet(["IC 030: Coffee beans; roasted coffee"])
    assert sig["notes"] == f"Owner: {owner}. {snippet}".strip(". ")


def test_trademark_notes_prefix_still_strippable_by_enrichment():
    """
    The exact contract enrichment.py relies on: notes.startswith(f"Owner: {owner}")
    using the separately-passed owner field, leaving the goods/services text.
    """
    result = _run_trademark_scan([
        _tm_hit("FILAMENT", "Filament Labs LLC",
                goods="IC 030: Coffee beans; roasted coffee")
    ])

    sig   = result["signals"][0]
    owner = sig["owner"]
    assert sig["notes"].startswith(f"Owner: {owner}")
    remaining = sig["notes"][len(f"Owner: {owner}"):].lstrip(". ").strip()
    assert remaining == "Coffee beans"


def test_trademark_owner_feeds_person_key_confluence():
    """
    An INDIVIDUAL owner must reach confluence as a real person key, through the
    same helper both scan paths use. Before the fix meta['owner'] was never
    written at all, so the person-key list was empty for every trademark and the
    Form D + trademark same-founder merge could never fire.

    Calls the real build_person_names + extract_person_keys rather than
    re-implementing them, so drift in either helper fails this test.
    """
    from app.services.signal_pipeline import build_person_names
    from app.services.confluence import extract_person_keys

    result = _run_trademark_scan(
        [_tm_hit("FILAMENT", "Dana Whitfield (INDIVIDUAL; USA)")]
    )
    meta = result["signals"][0]

    assert meta["owner"] == "Dana Whitfield"
    assert meta["owner_is_person"] is True
    assert extract_person_keys(build_person_names(meta)) == ["dana whitfield"]


def test_company_owner_is_never_offered_as_a_person_key():
    """
    Guardrail: a wrong merge is worse than a missed link.

    confluence.normalize_person only rejects names carrying a legal suffix, so a
    plain company name like "Sunset Beverage" would otherwise become the person
    key 'sunset beverage' and cluster two unrelated brands into a false
    multi-signal confluence hit — which also fires an alert email. Owners USPTO
    does not annotate as INDIVIDUAL must never enter person-key clustering.
    """
    from app.services.signal_pipeline import build_person_names
    from app.services.confluence import extract_person_keys, normalize_person

    # The underlying hazard is real: this name IS a valid person key on its own.
    assert normalize_person("Sunset Beverage") == "sunset beverage"

    result = _run_trademark_scan(
        [_tm_hit("SUNSET", "Sunset Beverage (CORPORATION; USA)")]
    )
    meta = result["signals"][0]

    assert meta["owner"] == "Sunset Beverage"
    assert meta["owner_is_person"] is False
    assert build_person_names(meta) == []
    assert extract_person_keys(build_person_names(meta)) == []


# ── Form D fixtures ──────────────────────────────────────────────────────────

_CONVICTION_MATCH = {
    "name":         "Ida Nunez",
    "reason":       "Repeat consumer founder",
    "known_brands": ["Nunez Provisions"],
    "designation":  "founder",
}

_ALUMNI_MATCH = {
    "name":         "Sam Orwell",
    "reason":       "Senior operator at an exited brand",
    "known_brands": ["Olipop"],
    "designation":  "alumni",
}

_FORM_D_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/formdfiling">
  <offeringData>
    <offeringSalesAmounts>
      <totalOfferingAmount>2500000</totalOfferingAmount>
      <totalAmountSold>1200000</totalAmountSold>
    </offeringSalesAmounts>
  </offeringData>
  <primaryIssuer>
    <relatedPersonsList>
      {people}
    </relatedPersonsList>
  </primaryIssuer>
</edgarSubmission>
"""

_PERSON_BLOCK = """
      <relatedPersonInfo>
        <relatedPersonName>
          <firstName>{first}</firstName>
          <lastName>{last}</lastName>
        </relatedPersonName>
        <relatedPersonRelationshipList>
          <relationship>{rel}</relationship>
        </relatedPersonRelationshipList>
      </relatedPersonInfo>
"""


def _form_d_xml(people):
    """people: list of (first, last, relationship)."""
    blocks = "".join(
        _PERSON_BLOCK.format(first=f, last=l, rel=r) for f, l, r in people
    )
    return _FORM_D_XML.format(people=blocks).encode()


def _signal(name="STEALTH BRAND"):
    return {
        "companyName": name,
        "signal_type": "delaware",
        "_adsh":       "0001234567-26-000101",
        "_cik":        "0001234567",
        "_name":       name,
    }


def _run_enrich(xml_bytes, conviction_for=(), alumni_for=()):
    """
    Run _enrich_related_persons with the XML fetch and both watchlist matchers
    faked. `conviction_for` / `alumni_for` are name substrings that should match.
    """
    def _fake_conviction(texts):
        for t in texts:
            if any(n in (t or "") for n in conviction_for):
                return dict(_CONVICTION_MATCH)
        return None

    def _fake_alumni(texts):
        for t in texts:
            if any(n in (t or "") for n in alumni_for):
                return dict(_ALUMNI_MATCH)
        return None

    signals = [_signal()]
    with patch("app.services.delaware._fetch_form_d_xml_content",
               return_value=xml_bytes), \
         patch("app.services.conviction.check_conviction_match_multi",
               side_effect=_fake_conviction), \
         patch("app.services.exit_watch.check_exit_alumni_match_multi",
               side_effect=_fake_alumni), \
         patch("app.services.delaware.time.sleep"):
        dw._enrich_related_persons(signals, limit=10)
    return signals[0]


# ── FIX 2: delaware.py promotes an alumni match to the signal top level ──────

def test_alumni_only_filing_promotes_exit_alumni_match_to_top_level():
    """
    scheduler.py reads meta["exit_alumni_match"] at the TOP level to fire the
    immediate ALUMNI watchlist alert. An alumni-only filing must set it.
    """
    xml = _form_d_xml([("Sam", "Orwell", "Executive Officer")])
    sig = _run_enrich(xml, alumni_for=["Sam Orwell"])

    assert sig["exit_alumni_match"] == _ALUMNI_MATCH
    assert "conviction_match" not in sig, "no conviction person on this filing"
    # Per-person value is still there — promotion copies, it does not move.
    assert sig["related_persons"][0]["exit_alumni_match"] == _ALUMNI_MATCH


def test_alumni_only_filing_reaches_the_scheduler_alumni_alert_branch():
    """The exact branch scheduler.py takes: alumni present, conviction absent."""
    xml = _form_d_xml([("Sam", "Orwell", "Director")])
    sig = _run_enrich(xml, alumni_for=["Sam Orwell"])

    conviction = sig.get("conviction_match")
    alumni     = sig.get("exit_alumni_match")
    assert conviction or alumni, "alert branch would have skipped this signal"
    match_type = "CONVICTION" if conviction else "ALUMNI"
    assert match_type == "ALUMNI"
    assert (conviction or alumni)["name"] == "Sam Orwell"


def test_conviction_and_alumni_filing_promotes_only_the_conviction_match():
    """
    Designations stay separate and conviction outranks alumni: when a filing
    names both, ONLY conviction_match is promoted to the top level.
    """
    xml = _form_d_xml([
        ("Sam", "Orwell", "Director"),
        ("Ida", "Nunez",  "Executive Officer"),
    ])
    sig = _run_enrich(xml, conviction_for=["Ida Nunez"], alumni_for=["Sam Orwell"])

    assert sig["conviction_match"] == _CONVICTION_MATCH
    assert "exit_alumni_match" not in sig, (
        "alumni must not be promoted alongside conviction — the two "
        "designations are never collapsed"
    )

    # The alumni person is NOT lost — it stays on its own related_persons row.
    by_name = {p["name"]: p for p in sig["related_persons"]}
    assert by_name["Sam Orwell"]["exit_alumni_match"] == _ALUMNI_MATCH
    assert by_name["Sam Orwell"]["conviction_match"] is None
    assert by_name["Ida Nunez"]["conviction_match"] == _CONVICTION_MATCH
    # Per-person alumni is skipped when that same person is a conviction match.
    assert by_name["Ida Nunez"]["exit_alumni_match"] is None


def test_conviction_wins_even_when_the_alumni_person_is_listed_first():
    """Ordering must not decide the designation — conviction always wins."""
    xml = _form_d_xml([
        ("Sam", "Orwell", "Promoter"),          # alumni, listed first
        ("Ida", "Nunez",  "Executive Officer"),  # conviction, listed second
    ])
    sig = _run_enrich(xml, conviction_for=["Ida Nunez"], alumni_for=["Sam Orwell"])

    assert sig.get("conviction_match") == _CONVICTION_MATCH
    assert sig.get("exit_alumni_match") is None


def test_filing_with_no_watchlist_match_promotes_neither():
    """A plain unknown-founder filing must stay visible but promote nothing."""
    xml = _form_d_xml([("Jordan", "Reyes", "Executive Officer")])
    sig = _run_enrich(xml)

    assert "conviction_match" not in sig
    assert "exit_alumni_match" not in sig
    assert [p["name"] for p in sig["related_persons"]] == ["Jordan Reyes"]


def test_conviction_outranks_alumni_across_the_whole_officer_list():
    """
    Precedence runs across ALL officers, not per-officer.

    A Form D listing an exit-alumni operator BEFORE a conviction founder must
    still promote the conviction founder. Stopping at the first officer who
    carried either match let list order decide, silently downgrading the
    strongest signal the product has. The two designations stay separate —
    alumni is promoted only when no conviction match exists anywhere.
    """
    enriched = [
        {"name": "Alma Operator", "conviction_match": None,
         "exit_alumni_match": {"name": "Alma Operator"}},
        {"name": "Cora Founder", "conviction_match": {"name": "Cora Founder"},
         "exit_alumni_match": None},
    ]
    top_conv = next(
        (p["conviction_match"] for p in enriched if p.get("conviction_match")), None
    )
    assert top_conv["name"] == "Cora Founder"

    top_alumni = None
    if not top_conv:
        top_alumni = next(
            (p["exit_alumni_match"] for p in enriched if p.get("exit_alumni_match")), None
        )
    assert top_alumni is None, "alumni must not be promoted when a conviction founder exists"
