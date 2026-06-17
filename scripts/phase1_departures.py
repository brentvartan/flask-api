#!/usr/bin/env python3
"""
Phase 1: NinjaPear profile pull for recent departures (~30 people, ~$1.20).
Finds anyone already at a new company after their brand exit/departure.

API: GET https://nubela.co/api/v2/employee/profile
Cost: 3 credits per call (~$0.04)
"""
import json, os, sys, time, pathlib, requests

_env = pathlib.Path(__file__).parent.parent / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

KEY = os.environ.get("PROXYCURL_API_KEY", "")
if not KEY:
    sys.exit("ERROR: PROXYCURL_API_KEY not set")

BASE    = "https://nubela.co"
TIMEOUT = 40   # NinjaPear avg: 10.5s fast / 38.7s detailed
PAUSE   = 2.0

# NinjaPear requires employer_website as a URL (company name alone returns a 400)
DEPARTURES = [
    ("Martin Hoffmann",    "https://www.on.com"),
    ("Marc Maurer",        "https://www.on.com"),
    ("Allison Ellsworth",  "https://drinkpoppi.com"),
    ("Stephen Ellsworth",  "https://drinkpoppi.com"),
    ("Tara Bosch",         "https://smartsweets.com"),
    ("Craig Dubitsky",     "https://helloproducts.com"),
    ("Morgan Zanotti",     "https://primalkitchen.com"),
    ("Kyle Leahy",         "https://www.glossier.com"),
    ("Nikki Neuburger",    "https://www.glossier.com"),
    ("Julie Bowerman",     "https://www.glossier.com"),
    ("Krishna Kaliannan",  "https://catalinacrunch.com"),
    ("Nina Fuhrman",       "https://moonjuice.com"),
    ("Tyler Ricks",        "https://drinksupercoffee.com"),
    ("John Shea",          "https://liquiddeath.com"),
    ("Jason Goldberger",   "https://dollarshaveclub.com"),
    ("Sally Gilligan",     "https://www.athleta.gap.com"),
    ("Nancy Green",        "https://www.athleta.gap.com"),
    ("Nick Vlahos",        "https://www.honest.com"),
    ("Christopher Gallant","https://chamberlaincoffee.com"),
    ("Mark Gillett",       "https://www.gymshark.com"),
    ("David Butler",       "https://www.heydude.com"),
    ("Luke Wood",          "https://www.beatsbydre.com"),
    ("Ricky Silver",       "https://www.daily-harvest.com"),
    ("Heidi Dorosin",      "https://smartsweets.com"),
    ("Cliff Moskowitz",    "https://www.outdoorvoices.com"),
    ("Stephen Kaplan",     "https://deciem.com"),
    ("Manish Joneja",      "https://www.barkbox.com"),
    ("Peter Attia",        "https://davidprotein.com"),
    ("Katie Sheeran",      "https://www.parachutehome.com"),
    ("John Capodilupo",    "https://www.whoop.com"),
]

def fetch_profile(name, brand):
    parts = name.strip().split()
    params = {
        "first_name":       parts[0],
        "last_name":        " ".join(parts[1:]) if len(parts) > 1 else "",
        "employer_website": brand,
    }
    try:
        r = requests.get(
            f"{BASE}/api/v2/employee/profile",
            params=params,
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=TIMEOUT,
        )
    except Exception as e:
        return None, f"network error: {e}"
    if r.status_code == 402: sys.exit("OUT OF CREDITS")
    if r.status_code == 404: return None, "not found"
    if r.status_code != 200: return None, f"HTTP {r.status_code}"
    return r.json(), "ok"

def current_company(profile):
    for exp in (profile.get("work_experience") or []):
        if exp.get("end_date") is None:   # null end_date = currently there
            return exp.get("company_name"), exp.get("role")
    return None, None

print(f"\n{'─'*60}")
print(f"PHASE 1 — Recent Departures Pull ({len(DEPARTURES)} people)")
print(f"Estimated cost: ${len(DEPARTURES) * 0.04:.2f}")
print(f"API: NinjaPear /api/v2/employee/profile")
print(f"{'─'*60}\n")

results = []
NEW_COMPANY     = []
ADVISORY        = []
SAME_OR_UNKNOWN = []

SKIP_COMPANIES = {
    "pepsico", "pepsi", "unilever", "coca-cola", "nestle", "nestlé",
    "kraft heinz", "mars", "apple", "colgate-palmolive", "colgate",
    "l'oreal", "loreal", "estée lauder", "estee lauder", "lvmh",
    "crocs", "gap", "lululemon", "keurig dr pepper"
}

for i, (name, brand) in enumerate(DEPARTURES, 1):
    print(f"  [{i:02d}/{len(DEPARTURES)}] {name} (ex-{brand})", end=" ... ", flush=True)
    profile, status = fetch_profile(name, brand)
    if not profile:
        print(f"✗ {status}")
        results.append({"name": name, "brand": brand, "found": False})
        time.sleep(PAUSE)
        continue

    company, title = current_company(profile)
    bio = profile.get("bio") or ""

    row = {
        "name": name, "brand": brand, "found": True,
        "current_company": company, "current_title": title,
        "bio": bio,
        "slug": profile.get("slug"),
    }
    results.append(row)

    if not company:
        print(f"→ no current role listed")
        SAME_OR_UNKNOWN.append(row)
    else:
        brand_low   = brand.lower()
        company_low = company.lower()
        is_parent   = any(s in company_low for s in SKIP_COMPANIES)
        is_same     = brand_low in company_low or company_low in brand_low
        is_advisory = any(w in company_low for w in ["advisor", "board", "angel", "investor", "venture", "capital", "consulting"])

        if is_same or is_parent:
            print(f"→ still at {company}")
            SAME_OR_UNKNOWN.append(row)
        elif is_advisory:
            print(f"★ ADVISORY: {company} | {title}")
            ADVISORY.append(row)
        else:
            print(f"🔥 NEW COMPANY: {company} | {title}")
            NEW_COMPANY.append(row)

    time.sleep(PAUSE)

# ── Save raw ──────────────────────────────────────────────────────────────────
out = pathlib.Path.home() / "Desktop" / "phase1_departures.json"
out.write_text(json.dumps(results, indent=2))

# ── Print summary ─────────────────────────────────────────────────────────────
print(f"\n{'═'*60}")
print(f"PHASE 1 RESULTS")
print(f"{'═'*60}")
found = sum(1 for r in results if r["found"])
print(f"  Profiles found: {found}/{len(DEPARTURES)}")
print()

if NEW_COMPANY:
    print(f"🔥 BUILDING SOMETHING NEW ({len(NEW_COMPANY)} people):")
    for r in NEW_COMPANY:
        print(f"   • {r['name']} (ex-{r['brand']})")
        print(f"     Now: {r['current_title']} @ {r['current_company']}")
        print(f"     {r['bio'][:90]}")
        slug = r.get("slug")
        if slug:
            print(f"     https://nubela.co/people/{slug}")
        print()

if ADVISORY:
    print(f"★ IN ADVISORY / BOARD MODE ({len(ADVISORY)} people):")
    for r in ADVISORY:
        print(f"   • {r['name']} (ex-{r['brand']}) → {r['current_company']}")
    print()

if SAME_OR_UNKNOWN:
    print(f"○ Still at brand / no new role ({len(SAME_OR_UNKNOWN)} people):")
    for r in SAME_OR_UNKNOWN:
        label = r.get("current_company") or "no current role listed"
        print(f"   · {r['name']} (ex-{r['brand']}) → {label}")
    print()

print(f"Raw results → {out}")
print(f"{'═'*60}\n")
