"""
The declared cost model behind Settings -> Spend.

The page shows one measured number and a dozen guesses. These tests pin the
thing that makes that safe: every entry says how it is known.
"""
from app.services.cost_model import VERIFIED_ON, cost_model

_VALID_BASIS = {"metered", "derived", "plan", "estimate"}


def _services(model):
    return [s for p in model["products"] for s in p["services"]]


def test_every_service_declares_a_valid_basis():
    for s in _services(cost_model()):
        assert s["basis"] in _VALID_BASIS, f"{s['service']} has basis {s['basis']!r}"


def test_every_service_explains_itself():
    """A cost line with no explanation is a number the reader cannot act on."""
    for s in _services(cost_model()):
        assert s["what_it_pays_for"].strip()
        assert s["what_makes_it_grow"], f"{s['service']} lists no growth driver"
        assert s["controls"], f"{s['service']} says nothing about caps"


def test_only_anthropic_on_stealth_finder_is_metered():
    """Exactly one thing is measured. If a second entry ever claims 'metered',
    either we built a real meter for it or someone overstated it."""
    metered = [s["service"] for s in _services(cost_model()) if s["basis"] == "metered"]
    assert metered == ["Anthropic (Claude)"], metered


def test_live_figures_replace_the_placeholder():
    m = cost_model({"spent_usd": 18.45, "cap_usd": 250.0, "remaining_usd": 231.55,
                    "pct_used": 7.4, "exhausted": False, "month": "2026-08"})
    a = next(s for s in _services(m) if s["basis"] == "metered")
    assert a["spent_usd"] == 18.45 and a["remaining_usd"] == 231.55


def test_survives_a_broken_meter():
    """The page must still render when the ledger is unreadable."""
    m = cost_model({"error": "ledger_unreadable"})
    a = next(s for s in _services(m) if s["basis"] == "metered")
    assert "spent_usd" not in a
    assert a["cap_usd"] == 250.0          # the declared ceiling still shows


def test_brand_manager_is_present_and_not_claimed_as_measured():
    m = cost_model()
    bm = next(p for p in m["products"] if p["product"] == "Brand Manager")
    assert bm["services"], "Brand Manager has no declared services"
    assert all(s["basis"] != "metered" for s in bm["services"]), \
        "nothing in Brand Manager is measured from this app"


def test_the_cap_boundary_is_stated_somewhere_a_reader_will_see_it():
    """The most misreadable fact on the page: $250 covers Stealth Finder's
    Anthropic spend and nothing else."""
    m = cost_model()
    bm = next(p for p in m["products"] if p["product"] == "Brand Manager")
    anth = next(s for s in bm["services"] if s["service"].startswith("Anthropic"))
    joined = " ".join(anth["controls"]).lower()
    assert "not capped" in joined or "stealth finder only" in joined


def test_basis_key_documents_every_basis_in_use():
    m = cost_model()
    used = {s["basis"] for s in _services(m)}
    assert used <= set(m["basis_key"]), used - set(m["basis_key"])


def test_verified_on_is_exposed_for_staleness():
    assert cost_model()["verified_on"] == VERIFIED_ON.isoformat()
