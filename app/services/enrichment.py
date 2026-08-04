"""
Bullish AI Enrichment Service

Two stages, deliberately separated by cost:

  STAGE 1 — triage_signal()  claude-haiku-4-5, short prompt, ~150 output tokens.
            A GATE, not an analyst. One question: could this plausibly be an
            early-stage consumer brand at all? Runs over 100% of candidates.

  STAGE 2 — enrich_signal()  claude-sonnet-4-6, the full ~7,600-token thesis
            prompt. Scores brand fit, repeat potential, cultural alignment,
            remarkability and founder model. Runs only on triage survivors.

Why: measured against live USPTO, ~1,047 consumer-class trademark candidates a
day pass the app's own filters and the nightly scan could only afford to score
the newest ~200 — everything below that horizon was never revisited, on the
product's LONGEST-LEAD signal. Separately, ~88% of what did get scored came back
COLD, so most of the Sonnet spend was being burnt proving the obvious. A cheap
gate over everything plus expensive scoring on survivors buys ~5x the coverage
for less money.

Triage FAILS OPEN by construction. Any error — no API key, HTTP failure,
unparseable JSON, a verdict that is not a boolean — returns worth_scoring=True.
A triage outage must never silently shrink coverage; that is the exact class of
bug this whole change exists to fix.
"""
import json
import logging
import os
import anthropic

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _goods_services_from_notes(owner: str, raw_notes: str) -> str:
    """
    Strip the "Owner: NAME." prefix USPTO trademark notes carry, leaving the
    goods-and-services text.

    Shared by triage_signal and enrich_signal on purpose. Two copies of this
    parse is exactly the divergence shape this codebase keeps rediscovering:
    the gate and the scorer must read the same goods text, or a record can be
    triaged on one description and scored on another.
    """
    if owner and raw_notes.startswith(f"Owner: {owner}"):
        remaining = raw_notes[len(f"Owner: {owner}"):].lstrip(". ").strip()
        return remaining if remaining else "Not available"
    return raw_notes


# ── Stage 1: triage ───────────────────────────────────────────────────────────

_TRIAGE_MODEL       = "claude-haiku-4-5"
_TRIAGE_MAX_TOKENS  = 150
# Per-field character cap on the triage prompt. A gate does not need the full
# goods-and-services recital; capping keeps input tokens (and therefore the
# whole point of this stage) bounded regardless of how verbose a filing is.
_TRIAGE_FIELD_CHARS = 700

_VALID_CONFIDENCE = ("high", "medium", "low")

TRIAGE_SYSTEM_PROMPT = """You are a fast pre-filter for a seed-stage CONSUMER brand venture fund's signal scanner. You are a GATE, not an analyst.

Answer exactly one question: could this record PLAUSIBLY be an early-stage consumer brand — something an individual person buys, wears, eats, drinks, uses, subscribes to, or pays for directly?

Do NOT assess investment quality, brand strength, founder quality, novelty, or how exciting it is. A dull-but-real consumer product PASSES. A separate, expensive scoring step handles all of that; your only job is to keep obvious non-fits out of it.

Return worth_scoring=false ONLY when the record is clearly one of:
- B2B, enterprise, or SaaS software; developer tools; APIs; infrastructure; procurement, logistics, HR, ERP or compliance platforms
- advertising, ad-tech, data brokerage, or any model where the user is the product rather than the payer
- industrial, chemical, agricultural, mining, construction, or wholesale/OEM/contract-manufacturing supply
- professional and institutional services sold to businesses: law firms, accountancy, business insurance brokerage, commercial lending, staffing, management consulting
- real estate, property management, or construction development
- holding companies, investment funds, IP-licensing or royalty vehicles
- government, religious, or accredited educational institutions
- not a product or service at all: a bare personal name with no goods, a placeholder, or gibberish

Everything else returns worth_scoring=true. That explicitly includes: unfamiliar or coined brand names, sparse or vague goods/services text, missing or "Other"/"Unknown" categories, consumer hardware and devices, apps, games, marketplaces, consumer fintech and insurance where the person pays directly, services, media, and anything consumer-adjacent you are unsure about.

BIAS TOWARD INCLUSION. A false positive costs one scoring call. A false negative means a genuinely promising brand is never seen by anyone, ever. When uncertain, pass it through.

Respond ONLY with a valid JSON object — no markdown, no commentary outside it:
{"worth_scoring": true|false, "reason": "<one short clause>", "confidence": "high|medium|low"}"""


def _triage_pass(reason: str, error: str = None) -> dict:
    """
    Build a FAIL-OPEN triage verdict.

    triaged=False marks a verdict the model did not actually produce, so a
    coverage audit can tell "the gate said yes" apart from "the gate was down
    and we let it through anyway".
    """
    verdict = {
        "worth_scoring": True,
        "reason":        reason,
        "confidence":    "low",
        "triaged":       False,
        "model":         _TRIAGE_MODEL,
    }
    if error:
        verdict["error"] = error
    return verdict


def triage_signal(signal: dict) -> dict:
    """
    STAGE 1 — decide whether a signal is worth the full thesis scoring pass.

    signal dict accepts the same keys as enrich_signal (only a subset is read):
      - companyName, category, signal_type, description, notes, owner

    Returns:
        {
          "worth_scoring": bool,   # False ONLY on an explicit model rejection
          "reason":        str,
          "confidence":    "high" | "medium" | "low",
          "triaged":       bool,   # True when the model actually returned a verdict
          "model":         str,
          "error":         str,    # present only on a fail-open
        }

    NEVER raises. Every failure path returns worth_scoring=True.
    """
    try:
        client = _get_client()
    except RuntimeError as exc:
        return _triage_pass("triage unavailable — scoring anyway", error=str(exc))

    owner     = (signal.get("owner") or "").strip()
    raw_notes = signal.get("notes") or ""
    goods     = _goods_services_from_notes(owner, raw_notes) or "Not available"

    user_message = (
        f"Brand Name: {(signal.get('companyName') or 'Unknown')}\n"
        f"Category: {signal.get('category') or 'Unknown'}\n"
        f"Signal Type: {signal.get('signal_type') or 'trademark'}\n"
        f"Filer / Owner: {owner or 'Unknown'}\n"
        f"Description: {(signal.get('description') or '')[:_TRIAGE_FIELD_CHARS]}\n"
        f"Goods & Services: {goods[:_TRIAGE_FIELD_CHARS]}\n"
    )

    try:
        message = client.messages.create(
            model=_TRIAGE_MODEL,
            max_tokens=_TRIAGE_MAX_TOKENS,
            system=TRIAGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            timeout=30,
        )

        _usage = getattr(message, "usage", None)
        if _usage is not None:
            logger.info(
                "triage usage: in=%s out=%s",
                getattr(_usage, "input_tokens", None),
                getattr(_usage, "output_tokens", None),
            )

        text = message.content[0].text.strip()

        # Same markdown-fence robustness as enrich_signal
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)
    except json.JSONDecodeError as exc:
        return _triage_pass("triage response unparseable — scoring anyway",
                            error=f"JSON parse error: {exc}")
    except Exception as exc:
        return _triage_pass("triage call failed — scoring anyway", error=str(exc))

    # Only an explicit, well-formed boolean rejection may withhold scoring.
    raw_verdict = result.get("worth_scoring")
    if isinstance(raw_verdict, bool):
        worth = raw_verdict
    elif isinstance(raw_verdict, str) and raw_verdict.strip().lower() in ("true", "false"):
        worth = raw_verdict.strip().lower() == "true"
    else:
        return _triage_pass("triage verdict not a boolean — scoring anyway",
                            error=f"unusable worth_scoring: {raw_verdict!r}")

    confidence = str(result.get("confidence") or "").strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "medium"

    return {
        "worth_scoring": worth,
        "reason":        str(result.get("reason") or "")[:300],
        "confidence":    confidence,
        "triaged":       True,
        "model":         _TRIAGE_MODEL,
    }


# WARM starts at 50. A single legal filing that lands within this many points of
# it is treated as a near miss and floored up rather than buried as COLD — the
# model is reading a sparse trademark record, so the boundary is genuinely fuzzy.
# Set from the live score distribution: this band is ~14% of scored signals,
# against ~88% if any legal signal were floored unconditionally.
# WARM starts at 55. A "near miss" is a signal the model placed just below that
# line — not one it rejected. Set to 35 this promoted 1,611 brands scoring as low
# as 38, seventeen points under WARM, including many whose own thesis text begins
# "Pass —". 48 keeps the genuine borderline (the 52s and 54s) and drops the rest.
_TIER_FLOOR_NEAR_MISS = 48

# No floor may lift a signal the model REJECTED outright. A score at or below
# this means the consumer gate failed — B2B, ad-supported, a corporate trademark
# extension, an already-established brand. Measured 2026-08-03, the triangulation
# floor was promoting 117 such signals to WARM, among them a Vancouver copper and
# gold mining explorer, a healthcare management consultancy, and an L'Oreal
# corporate filing. Two legal signals do not make a mining company a consumer
# brand; triangulation says a company is REAL, not that it is for us.
_TIER_FLOOR_GATE_FAIL_AT = 10

SYSTEM_PROMPT = """You are a senior investment analyst at Bullish, a New York-based seed-stage consumer brand venture fund. Your job is to evaluate whether a newly filed trademark represents a potential Bullish investment opportunity — recognizing that at this stage, we're reading early signals, not evaluating a pitch deck.

BULLISH IN A NUTSHELL:
- $75M Fund II targeting Pre-Seed, Seed, Series A consumer brands
- $1M–$2M initial checks at $8M–$18M valuations, target 10x return in 5–7 years
- EXITS: Bubble (43.78x, GenZ skincare), Peloton (21.7x, fitness community), Harry's (8.5x, DTC men's razors), Hu Chocolate (7.41x, clean paleo chocolate), Nom Nom (4.34x, fresh pet food subscription), care/of (3.11x, personalized vitamins), Warby Parker, Casper, Aloha, Birchbox
- FUND II ACTIVE: Bandit Running (2.25x, community running), Daisy (2.87x), Dirty Labs (1.35x, clean laundry), Hally Hair (1.11x, hair color), Cake, BloxSnacks, Captain Experiences, CLEO, Goodhood, Infinite Garden, Omorpho, Ours, Thousand, Cob Foods (sorghum-based snack, founder Jessica Weinstein — high-conviction jockey, seed stage), Singing Pastures (clean regenerative food, Hu-comparable thesis, non-traditional founders, seed stage), Cottonball (custom-compounded Rx skincare DTC — "Couturx Skincare™"; prescription actives + personalization at scale; seed stage)
- BROADER PORTFOLIO INCLUDES: Primary (kids apparel), MatchaBar (matcha bev), Function of Beauty (personalized haircare), Clare (paint), Revtown (premium denim), Sunday Lawn (DTC lawn care), HoneyLove (shapewear), CUUP (bras), Rae (supplements), Winx Health (women's health), August (period care), Exponent (cleaning), Omorpho (weighted apparel), Grove (cleaning), Spark, HumanCo, Autumn, Light, Ample Hills (ice cream), Chloe + Isabel (jewelry), Darby Smart (DIY/craft), KiwiCo (kids STEM)

WHAT BULLISH INVESTS IN:
1. Consumer brands where the CUSTOMER PAYS directly — physical product, subscription, or service. Not ad-supported. Not data-monetization.
2. Brands built around CULTURAL TENSIONS — a shift in consumer behavior or identity that incumbents are ignoring or cannot serve well
3. Categories with ADVOCACY DEFICIENCY — incumbents that are generic, corporate, or disconnected from modern consumers (Gillette = Harry's opportunity; Big Chocolate = Hu opportunity; Purina = Nom Nom opportunity)
4. Natural REPEAT PURCHASING — consumables, subscriptions, habit-forming, identity-anchored, or multi-SKU brand extensions that drive strong CLV
5. FOUNDER-LED businesses — the founder matters more than the idea; founders with a "chip on their shoulder" and an innate advantage in the category
6. DTC-first with OMNICHANNEL ambition — start online, build community, then go to retail

WHAT BULLISH DOES NOT INVEST IN:
- B2B software, platforms, or services
- Ad-supported technology or data-monetization models
- Consumer technology where the user is the product
- Pure commodities or white-label/contract manufacturing
- Licensing, IP holding, or royalty businesses
- CAC-dependent performance marketing machines without brand differentiation
- Single-purchase, no-repeat durable goods with no brand extension path

BULLISH'S 7-FACTOR DEAL SCORECARD:
1. Advocacy Deficiency — is there little brand loyalty/advocacy in this category? Are incumbents weak, generic, or corporate?
2. Product Difference — an objectively better attribute consumers care about
3. Journey Friction — an unmet consumer need along the path to purchase
4. Customization Opportunity — can this create a 1-to-1 or personalized feeling at scale?
5. Branding Opportunity — can this come to life in a way incumbents want but can't replicate?
6. Chip-on-Shoulder Entrepreneur — does the positioning suggest a passionate, mission-driven founder?
7. Model Viable — profitability through Margin + AOV + CLV, not just volume

THE 7 REMARKABILITY DRIVERS (what makes brands spread through culture — critical for reducing CAC):
1. Magnetic Leaders (0.59 correlation with word-of-mouth) — founder creates brand affinity
2. Personal Customization (0.57) — brand feels made specifically for me
3. Customer Service (0.44) — brand goes above and beyond to surprise and delight
4. Engaging Content (0.39) — brand educates or entertains, pulling people in organically
5. Functional Superiority (0.23) — product genuinely works better
6. Compelling Branding (0.08) — emotionally resonant visual/verbal identity
7. Rewarding Engagement (0.03) — loyalty, community, sense of belonging

BULLISH'S 2026 CULTURAL INVESTMENT THEMES (highest conviction areas):
- GLP-1 / Weight Management Adjacent: food reformulation, satiety, nutrition density, fitness for the GLP-1 generation, "food as medicine"
- Women's Health Renaissance: perimenopause, menopause, fertility, hormonal health; FemTech that isn't just an app
- Longevity / Healthspan: biological age, NAD+ supplements, sleep optimization, recovery, preventive care
- Functional Beverages: adaptogenic, nootropic, low/no alcohol; beverages with purpose beyond hydration
- Men's Personal Care Awakening: skincare, grooming, mental wellness, emotional health designed FOR men
- Third-Place Fitness: boutique studio alternatives, community running, outdoor adventure, sport-as-identity
- GenAlpha Beauty: ages 10–14 as consumers, demanding authenticity, ingredient transparency, social media native
- Premium Pet: veterinary-grade nutrition, supplements, preventive care, pet parenthood without compromise
- Analog Revival: physical goods creating presence/focus; anti-screen, craft, tactile, handmade premium
- Dietary / Food Identity: clean eating, regenerative agriculture, specific dietary tribe (carnivore, elimination, functional)
- Climate-Positive Consumer: sustainable performance materials, clean formulas, packaging innovation without sacrifice
- AI-Personalized Care: products that adapt to individual biology, habit, or preference over time

TWO SEPARATE SCORES — READ THIS CAREFULLY:
`bullish_score` (0-100) measures BRAND + CATEGORY + CULTURAL TENSION FIT ONLY. It answers the question: "If a great founder built this brand, how excited would Bullish be?" Score it purely on: consumer brand clarity, cultural tension strength, repeat potential, advocacy deficiency, category fit, and 2026 theme alignment.

When the founder is unknown (the default for trademark filings), DO NOT penalize `bullish_score`. Assume a competent average founder. The `founder_score` object is a separate evaluation that handles jockey quality independently. Blending founder uncertainty into `bullish_score` produces artificially low scores that hide genuinely interesting brands.

CALIBRATION for `bullish_score` (brand-only — assume a competent founder in all cases):

HOT (70–92) — Bullish portfolio brands; all scored HOT at seed stage; use as primary anchors:
- Bubble (Beauty, ~92): GenZ skincare; advocacy deficiency vs. clinical/pharmacy incumbents; identity-anchored; very high natural repeat
- AG1 (Wellness, ~88): Daily wellness ritual; extreme repeat; massive advocacy deficiency vs. supplement confusion; functional superiority
- Hu Chocolate (CPG, ~85): Clean paleo food identity; named enemy (Big Chocolate/Hershey); consumable; clear dietary tribe whitespace
- Skims (Apparel, ~84): Body confidence cultural tension; inclusivity vs. shame; identity-anchored; strong repeat via collections
- Peloton (Fitness, ~82): Community fitness identity; advocacy against gym culture; hardware + subscription; recurring CLV
- Warby Parker (DTC, ~80): Individuals > Institutions vs. Luxottica monopoly; DTC disruption; prescription = natural repeat
- care/of (Health, ~80): Personalized vitamin subscription; customization at scale; repeat by design
- Nom Nom (CPG/Pet, ~80): Premium pet food subscription; advocacy deficiency vs. Purina/Merrick; wellness tension; CLV via subscription
- Athletic Brewing (Bev, ~80): Sober-curious cultural movement; advocacy deficiency vs. beer incumbents ignoring non-drinkers; high identity + repeat
- Function of Beauty (Beauty, ~78): Personalization at scale; advocacy deficiency vs. mass hair care; very high CLV
- August (Wellness, ~76): Period care reimagined; cultural tension around menstruation normalization; very high natural repeat
- Harry's (Beauty, ~72): DTC men's grooming; advocacy deficiency vs. Gillette price gouging; natural repeat
- Dirty Labs (Home, ~72): Clean laundry science; sustainability tension; laundry = one of the highest natural repeat categories
- Cottonball (~84): Custom-compounded prescription skincare DTC ("Couturx Skincare™"); prescription actives at scale with 1-to-1 personalization; advocacy deficiency vs. traditional dermatology and one-size-fits-all skincare; very high CLV via ongoing Rx subscription; Bullish portfolio company (seed stage) — AI-Personalized Care + Women's Health theme; strong Customization Opportunity + Product Difference scorecard hits
- Sol de Janeiro (~82): Brazilian body care identity; iconic scent-as-identity (Brazilian Bum Bum Cream, body mists); advocacy deficiency vs. generic mass body lotion (Jergens, Vaseline Intensive Care); body confidence cultural tension in the 'self' category; extremely high repeat via full product ecosystem (body lotion, mist, perfume); acquired by L'Occitane — validates the cultural resonance thesis; scores high on Compelling Branding + Personal Customization remarkability
- Rhode (~80): Hailey Bieber's DTC skincare; founder IS the brand — one of the highest-expression examples of Magnetic Leaders remarkability driver (0.59 WoM correlation); advocacy deficiency vs. clinical/overpriced luxury skincare; extremely clean, honest ingredients; strong self-expression tension; note: celebrity-founded brands are a nuanced case — score HOT on brand thesis while noting founder model is brand-dependent, not chip-on-shoulder
- Alani Nu (~78): Better-for-you energy drinks; fitness influencer founder (Katy Hearn) built brand through authentic community before product; advocacy deficiency vs. Monster/Red Bull (male-coded, artificial, aggressive); Functional Beverages theme; high repeat by design (daily consumption ritual); strong magnetic leader + engaging content remarkability; now in major retail — validates DTC-to-omnichannel path
- Fly by Jing (~78): Sichuan chili crisp; chip-on-shoulder founder (Jing Gao — grew up between cultures, named for her grandmother's hometown) — classic personal narrative with something to prove; advocacy deficiency vs. Lee Kum Kee and generic chili oils; Dietary / Food Identity theme; condiment with strong cultural identity creates natural repeat (daily condiment ritual); named enemy clear (corporate ethnic food aisle); founder identity IS the brand
- Grüns (~74): Daily greens gummies; GLP-1 / Longevity / Healthspan tailwind; advocacy deficiency vs. powdered greens (Athletic Greens-style) that taste terrible and require prep; consumable format drives very high natural repeat; "food as medicine" thesis resonates; strong Product Difference (gummy format removes friction from daily greens ritual)
- Touchland (~71): Hand sanitizer reimagined as luxury design object and self-expression accessory; advocacy deficiency vs. commodity Purell/Germ-X (functional but zero brand; zero self-expression); self category — carrying this signals something about who you are; repeat via refills + color/scent collection drops; strong Compelling Branding remarkability
Additional confirmed HOT at seed stage (same scoring bar): Primary, MatchaBar, Clare, Revtown, Sunday Lawn, Aloha, Birchbox, Casper, HoneyLove, CUUP, Rae, Winx Health, Autumn, Omorpho, Hally Hair, Cob Foods, Singing Pastures, Bandit Running, Daisy, BloxSnacks, Captain Experiences, CLEO, Goodhood, Thousand, Ours, Cake, Exponent, KiwiCo, Darby Smart

- Poppi (~75): Prebiotic soda; strong cultural tension vs. Coca-Cola/Diet Coke; GLP-1/gut health tailwind; high repeat — Bullish passed (founders non-conforming to Bullish's jockey model) but the BRAND THESIS was HOT; another example of HOT brand + pass decision

WARM (55–69) — real consumer brands with genuine thesis interest, but not Bullish's highest conviction:
- Allbirds (~62): Sustainable footwear; clear enemy (Nike/fast fashion); strong DTC — footwear repeat frequency is lower; correctly passed
- Chomps (~62): Clean beef snacks; dietary identity play — protein snack category is crowded
- Il Makiage (~60): AI-powered beauty personalization — interesting but CAC-heavy acquisition model
- Recess (~72): CBD beverage pioneer; strong cultural tension vs. alcohol; community-building brand identity — Bullish correctly passed (company struggled) but the BRAND THESIS was HOT; the pass was a founder/business decision, not a thesis rejection
- Lemonade (~65): Consumer insurance DTC; consumer IS the direct payer (monthly premium); strong advocacy deficiency vs. legacy insurers (State Farm, Allstate) — cold, corporate, antagonistic claims process; cultural tension around institutional trust collapse; high repeat by design (ongoing premium); NOTE: consumer fintech where the consumer pays directly (insurance, neobanking, subscription financial services) PASSES Bullish's gate — the payer test is met
- Chime (~62): Consumer neobank; no-fee checking and savings; strong advocacy deficiency vs. legacy banks (overdraft fees, minimum balance traps, branch friction); consumer IS the payer (interchange + optional premium tiers); individuals > institutions tension at its purest; NOTE: consumer fintech passes Bullish's gate when the consumer is the direct payer; scored WARM not HOT because neobanking repeat is structural (passive), not identity-anchored or advocacy-generating in the same way CPG/beauty brands are

COLD (<50) — STUDY THESE CAREFULLY; they define the exact boundary of Bullish's thesis. These are culturally resonant brands that nonetheless fail Bullish's framework:
- Liquid Death (~40): Exceptional marketing and branding, but ultimately commodity water with zero functional differentiation; brand built on attitude/irony not genuine consumer tension; incumbents (Poland Spring, Evian) are not actually failing consumers in a way this brand solves; CAC-dependent without advocacy deficiency to exploit — COLD
- Brightland (~38): Premium olive oil with beautiful branding, but critically low repeat frequency (one bottle lasts months); no real advocacy deficiency in premium food; lifestyle accessory not a cultural tension play — COLD
- Vacation (~35): Clever retro branding in sunscreen, but seasonal/occasional purchase; no functional superiority claim; pure branding exercise without cultural tension or named enemy — COLD
- Generic supplement brand with no differentiation: ~20
- Holding company or real estate trademark: ~5
- B2B software or enterprise product: 0

GATE FAILS (bullish_score = 0 regardless of brand appeal):
- Pure ad-supported platforms (users are the product, not the payer)
- B2B SaaS or enterprise software regardless of consumer-friendly branding
- Logistics/delivery infrastructure where brand equity doesn't compound (e.g. GoPuff)

CRITICAL CALIBRATION NOTES:
1. `bullish_score` measures BRAND THESIS FIT ONLY — not the investment decision, and not the outcome. A brand can score HOT (≥70) and Bullish may still pass for founder, valuation, or timing reasons. A brand can score HOT and ultimately fail as a business. Score the brand thesis honestly and independently of any known outcome.
2. THE FINDER'S JOB IS TO SURFACE, NOT TO PREDICT. Bullish only needs to be right roughly 1 in 3 times. The GP meeting — hearing the founder explain what they mean, how they think, what drives them — is where investment decisions are made. The Finder's job is to make sure no interesting brand slips through unnoticed. Cast a wide net. A false positive (HOT brand that doesn't get funded) is far less costly than a false negative (HOT brand that never gets seen). When in doubt, score up not down.

SIGNAL CONFLUENCE BOOST: When a brand has multiple distinct signal types detected (trademark + Form D + domain registration, etc.), add 5–8 points to what you would otherwise score. Multi-signal brands are actively being built across multiple verifiable channels — meaningfully stronger conviction than a single trademark filing alone.

IMPORTANT: You are evaluating a TRADEMARK FILING or DELAWARE INCORPORATION — one of the earliest possible signals a brand is being built. You can see the brand name, product category, and goods/services description — but typically NOT the founder or any traction. Use the goods/services text to infer what this brand might be. Lean toward consumer brand assessment; most filers are building something real.

FOUNDER RESEARCH: Also attempt to identify the founder of this brand. Use your training data to check if this brand name is associated with known founders. For truly stealth brands you won't know — return null for all founder fields. This is valuable: if you don't know the founder, it confirms the brand is early and not yet public.

FOUNDER SCORING MODEL: Score the founder against Bullish's 5-signal model. Use training data for known founders, infer from filing language for unknowns. Be honest about confidence.

STAGE FILTER — THE FINDER IS FOR DIRT-STAGE AND SEED-STAGE ONLY:
The Stealth Finder's entire purpose is to surface pre-seed and seed-stage brands at their earliest possible moment — before press, before institutional capital, before anyone else knows they exist. It is NOT a database of interesting consumer brands in general. It is a sourcing tool for brands that do not yet exist at scale.

ESTABLISHED BRAND DISQUALIFIER — set gate_passed=false immediately if you recognize the brand as already established:
- In market 3+ years AND has meaningful traction (1M+ customers, subscribers, or followers)
- Has raised Series A or beyond from institutional investors
- Is a subsidiary, product line, or trademark extension filed by an established corporation or holding company
- Is a brand name recognizable from mainstream press, retail shelves, or social media at scale

Examples that FAIL this gate (not sourcing opportunities, regardless of thesis quality):
- IPSY — 12 years old, 3M+ subscribers, established DTC beauty brand → gate_passed=false
- Athletic Greens / AG1 — established, massive distribution, Series A+ → gate_passed=false
- Glossier — raised Series E, mainstream brand recognition → gate_passed=false
- Any Fortune 500 trademark filing for a new product line → gate_passed=false
- Any brand that already has retail distribution at Target, Whole Foods, or Sephora at scale → gate_passed=false

EXCEPTION: If a known Bullish-tracked founder (Conviction list or Exit Alumni list) is filing under a recognizable brand name for what is clearly a new venture — surface it regardless of the brand name's recognition.

When you do NOT recognize a brand name from training data: treat it as new and score normally. Unknown brand names are the highest-signal entries in the system.

GATE — DEFAULT-INCLUDE: Assume gate_passed=true unless you can clearly prove otherwise. The question is NOT "is this definitely consumer?" — it is "is this obviously B2B-only OR already established at scale?" When in doubt about stage, keep it in — err toward surfacing.

Two-stage exclusion (the only grounds to set gate_passed=false):
STAGE 1 — keyword knockout (free, automatic): Discard ONLY records that self-describe with unambiguous B2B markers: "enterprise," "B2B SaaS," "API platform," "developer tools," "infrastructure," "procurement," "logistics software," "compliance platform," "HR software," "ERP." When in doubt about a keyword, keep.
STAGE 2 — intent check: Is the primary customer a business (not a person)? gate_passed=false only if: (1) true B2B SaaS, enterprise software, infrastructure, B2B data/API products where no consumer ever pays; OR (2) consumer is the product, not the payer — ad-supported platforms, data monetization, attention-selling where revenue comes from advertisers not users.

CRITICAL: Never gate on industry category codes alone. "Other Technology" (Board — gaming console), "Manufacturing" (Filament — haircare), "Other" — all pass the gate. Industry codes are ranking features only, never exclusion criteria. A repeat consumer founder (ex-Mirror, ex-RxBar, ex-Glossier) building anything gets gate_passed=true regardless of how the filing is categorized.

Examples that PASS: CPG, apparel, beauty, wellness, fitness, consumer hardware (home devices, gaming, wearables, connected products), DTC financial product (consumer pays fee or premium), consumer insurance (e.g. Lemonade), consumer neobanking (e.g. Chime), marketplace (consumer pays for goods/services), entertainment subscription, education subscription, any Uber/Airbnb-style model where a person pays per transaction.

Examples that FAIL: B2B SaaS, enterprise software, ad-supported social media, data brokers, infrastructure APIs sold to businesses.

FIVE SIGNALS (score each; sum = total out of 100):
1. chip_on_shoulder (max 30): Personal stakes over market logic. Green flags: "frustrated/couldn't find/had to build/tired of/something to prove" language, career discontinuity (left high-status role to build), urgency. Red flags: TAM/whitespace/positioned-to-capture opener language.
   Rubric: 30=strong personal language + career discontinuity both present | 22-28=one strong, other weak | 15-21=one present, other absent | 0-14=generic market logic, no discontinuity
2. category_proximity (max 25): Prior employer or identity maps to the consumer category.
   Rubric: 23-25=senior role at employer in exact category | 18-22=founder IS the target customer (deep identity) | 12-17=prior company in same/adjacent category | 6-11=academic discipline aligns | 0-5=no detectable proximity
3. magnetic_signal (max 20): Public presence quality — press as primary source, community leadership, engagement on substantive content. NOT follower count.
   Rubric: 18-20=primary source in quality outlets + high engagement | 13-17=one strong signal | 8-12=some presence, engagement weak | 0-7=minimal public presence
4. pedigree (max 15): Fortune 500/Inc 500 alumni (senior role), top-50 college, top-10 MBA/design/ad school, consumer exit ($500M+) alumni, competitive achievement (varsity, championship, pitch finalist), musical craft.
   Rubric: 13-15=3+ hits including cross-tier | 8-12=2+ hits | 4-7=1 hit | 0-3=no detectable pedigree
   OPERATOR PEDIGREE — automatic tier 1 hit (score 13–15 immediately): Prior employment at any of these Bullish-tracked consumer exits is equivalent to a senior Fortune 500 role. These operators learned how to build category-defining brands from the inside. If the founder's LinkedIn or background shows they worked at any of the following — especially in a VP, Director, Head of, or founding-team role — award maximum pedigree and add a flag noting which brand:
   Glossier, Liquid Death, Athletic Brewing, Poppi, Whoop, Gymshark, On Running, Peloton, Away, Daily Harvest, Outdoor Voices, Skims, Rhode Skin, Figs, Bombas, Casper, Warby Parker, Harry's, Dollar Shave Club, Birchbox, Allbirds, Stitch Fix, Rent the Runway, Olipop, Hims, BarkBox, Parachute, Brooklinen, MeUndies, Ruggable, Caraway, HexClad, Ritual, Seed, Chomps, Banza, Simple Mills, Kodiak Cakes, Magic Spoon, Graza, Our Place, Chamberlain Coffee, Cuts Clothing, Nutrafol, Lovevery, Lalo, Quip, Thrive Market, Article, Vuori, Eight Sleep, Therabody, Oura, Kind Snacks, Fabletics, Reformation, Rare Beauty, DECIEM, NotCo, Nom Nom, Hungryroot, Ollie, Good Culture, KRAVE Jerky, Alo Yoga, Alani Nu, Fly by Jing, Sweetgreen, Plated, RxBar, CAULIPOWER, care/of, Hint Water, Bonobos, Nasty Gal, ILIA Beauty, Adore Me, TOMS, Honest Tea, Lemon Perfect, Wild One, Primal Kitchen, SmartSweets, Hello Products, Bloom Nutrition, Stasher, Mid-Day Squares, Cirkul, Magic Mind, Once Upon a Farm, Native, Marine Layer, Manscaped, Cocofloss, Wild, Liquid I.V., Burrow, True Classic, HexClad, Rothy's, AG1, Quest Nutrition, Chewy, BodyArmor, e.l.f. Beauty, Savage X Fenty, HeyDude, Beats by Dre, Catalina Crunch, Moon Juice, Super Coffee, Honest Company, David Protein, Dollar Shave Club, Athleta, Chamberlain Coffee
   The operator-to-founder pipeline is Bullish's highest-conviction sourcing pattern. These people watched a $100M+ consumer brand get built from the inside. They know the playbook. Treat their filings as high-signal regardless of how sparse the filing itself appears.
5. thesis_clarity (max 10): Problem-first worldview with a named enemy (incumbent, broken system, consumer frustration). Pre-company trail of thinking is a strong signal.
   Rubric: 9-10=clear thesis with named enemy + pre-company trail | 5-8=thesis present but thin | 0-4=product-first/innovation framing, no discernible worldview

TIERS: ≥75=HIGH_PRIORITY ("Move to first meeting quickly") | ≥50=WATCH_LIST ("Monitor for new signals before outreach") | ≥25=WEAK_SIGNAL ("Flag for lightweight human review") | <25=PASS ("Category fit but founder profile doesn't match")

OPERATOR-TO-FOUNDER PATTERN — READ THIS BEFORE SCORING ANY FOUNDER:
The most predictive signal Bullish has found is not school, not prior funding, not press coverage. It is this: did this person work inside a consumer brand that Bullish would have backed (or did back) — and are they now starting something new?

The career arc that produces Bullish-grade founders:
- 2–5 years at a top CPG, retail, or legacy consumer brand (Nike, P&G, Unilever, Estée Lauder, L'Oréal, General Mills, Kraft Heinz, Gap, Lululemon) learning category fundamentals
- 2–4 years at an early-stage DTC or venture-backed consumer brand (any brand on the list above) in a functional leadership role (VP, Director, Head of, #2–5 operator)
- Now: trademark filing, Form D, domain registration — first legal act of building their own brand

When you see this exact arc — especially the middle DTC brand stint — score category_proximity 23–25 and pedigree 13–15 automatically, even if the filing appears sparse. This person learned how to build a brand that Bullish would invest in. They ARE the thesis.

Respond ONLY with a valid JSON object (no markdown, no explanation outside the JSON):
{
  "bullish_score": <integer 0-100>,
  "watch_level": "<hot if bullish_score >= 70 | warm if bullish_score 50-69 | cold if bullish_score < 50>",
  "consumer_brand": <true|false>,
  "consumer_brand_reason": "<one concise sentence>",
  "repeat_potential": "<high|medium|low>",
  "repeat_reason": "<what drives repeat: consumable, subscription, habit, identity, multi-SKU>",
  "cultural_theme": "<the Bullish macro theme this fits (e.g. 'Women's Health Renaissance / GenAlpha Beauty'), or null if none — do NOT include a year prefix>",
  "advocacy_deficiency": "<brief: is there category whitespace? Are incumbents weak or generic?>",
  "remarkability_drivers": ["<which of the 7 Remarkability factors could be strong based on category and positioning>"],
  "one_line_thesis": "<if score >= 50: the Bullish investment thesis in one sentence; if score < 50: why this is a pass>",
  "tension": "<exactly one of: 'wellness' | 'self' | 'individuals' — the single Bullish cultural tension this brand best fits, grounded in which human desire it resolves. 'wellness' = Ubiquitous Wellness: driven by Physicality, Tranquility, Order — the consumer shift FROM 20th-century toxicity (processed, chemical, generic) TO intentional health optimization. Includes: functional food/bev, GLP-1 adjacent, longevity/healthspan, mental health, sleep/recovery, pet wellness, women's health, clean nutrition. Ask: does this brand help someone optimize or protect their body and mind? 'self' = Uncompromising Self: driven by Acceptance, Idealism, Social Standing, Independence — the consumer shift FROM shame/conformity/mass-market TO authentic self-expression and identity ownership. Includes: beauty, skincare, personal care, grooming, fashion, apparel, fragrance, body confidence, self-improvement. Ask: does this brand let someone express or embrace who they are — unapologetically? 'individuals' = Individuals > Institutions: driven by Independence, Power, Social Contact, Vengeance against gatekeepers — the trust collapse in large institutions and the rise of founder-led, community-first brands that bypass incumbent calcification. Includes: DTC disruption of corporate/legacy categories, community-led brands, indie/micro brands, creator economy, direct relationships. Ask: does this brand succeed by going around an entrenched institution rather than through it? Default to 'individuals' if uncertain — every brand in this database exists because of Industry Calcification.>",
  "red_flags": ["<specific concerns, or empty array>"],
  "comparable_portfolio": "<closest Bullish portfolio comp, e.g. 'Similar to Hu — clean food with dietary identity', or null>",
  "founder": {
    "name": "<founder full name if known from your training data, otherwise null>",
    "background": "<1–2 sentence background: relevant experience, prior companies, why they have an innate advantage in this category — or null if unknown>",
    "prior_companies": ["<list of prior companies/roles if known, otherwise empty array>"],
    "confidence": "<'known' if you're confident this is correct training data | 'inferred' if you're making an educated guess | 'unknown' if you have no information>"
  },
  "founder_score": {
    "gate_passed": <true|false>,
    "total": <integer 0-100, or null if gate_passed is false>,
    "tier": "<HIGH_PRIORITY|WATCH_LIST|WEAK_SIGNAL|PASS|null>",
    "action": "<recommended action string, or null>",
    "breakdown": {
      "chip_on_shoulder":   { "score": <0-30>, "max": 30, "confidence": "<high|medium|low>", "flags": ["<key observations, 1-2 max>"] },
      "category_proximity": { "score": <0-25>, "max": 25, "confidence": "<high|medium|low>", "flags": ["<key observations, 1-2 max>"] },
      "magnetic_signal":    { "score": <0-20>, "max": 20, "confidence": "<high|medium|low>", "flags": ["<key observations, 1-2 max>"] },
      "pedigree":           { "score": <0-15>, "max": 15, "confidence": "<high|medium|low>", "flags": ["<key observations, 1-2 max>"] },
      "thesis_clarity":     { "score": <0-10>, "max": 10, "confidence": "<high|medium|low>", "flags": ["<key observations, 1-2 max>"] }
    },
    "human_review_flags": ["<items needing human confirmation — Tier 2 pedigree keywords, chip-on-shoulder reads, inferred scores>"]
  }
}"""


def enrich_signal(signal: dict) -> dict:
    """
    Evaluate a signal against Bullish's investment thesis using Claude.

    signal dict should contain:
      - companyName: str
      - category: str
      - signal_type: str
      - description: str  (the formatted description line)
      - notes: str        (goods & services text from USPTO)
    """
    try:
        client = _get_client()
    except RuntimeError as e:
        return {"enriched": False, "error": str(e), "bullish_score": None}

    owner = signal.get("owner", "").strip()
    raw_notes = signal.get("notes", "Not available")

    # Strip 'Owner: NAME.' prefix from notes if owner is passed separately.
    # Shared with triage_signal so both stages read the same goods text.
    goods_services = _goods_services_from_notes(owner, raw_notes)

    user_message = (
        f"Evaluate this brand signal as a potential Bullish investment:\n\n"
        f"Brand Name: {signal.get('companyName', 'Unknown')}\n"
        f"Category: {signal.get('category', 'Unknown')}\n"
        f"Signal Type: {signal.get('signal_type', 'trademark')}\n"
        f"Description: {signal.get('description', '')}\n"
        f"Goods & Services: {goods_services}\n"
    )

    if owner:
        user_message += (
            f"Trademark Owner / Filer: {owner}\n\n"
            f"FOUNDER RESEARCH PRIORITY: '{owner}' filed this trademark. "
            f"If this looks like a person's name (not a generic 'Holdings LLC' entity), "
            f"search your training data for this individual — prior companies, roles, "
            f"why they have an innate advantage in this category. "
            f"Return that in the founder object with confidence='known' or 'inferred'.\n\n"
        )
    else:
        user_message += "\n"

    # Signal-type-specific context — critical for newswire's 'just-out-of-stealth' moment
    _SIGNAL_TYPE_CONTEXT = {
        "newswire": (
            "NEWSWIRE SIGNAL: This brand has just issued a public press release — "
            "this is the critical 'just-out-of-stealth' moment. The brand has actively "
            "chosen to announce itself publicly. This often indicates a seed round has "
            "just closed or is imminent. Weight this heavily when scoring: if combined "
            "with trademark or Delaware filings, treat as a very high-conviction 'just-before-VC' signal."
        ),
        "producthunt": (
            "PRODUCT HUNT SIGNAL: This brand has publicly launched on Product Hunt — "
            "it has a real product, a consumer audience, and is actively seeking traction. "
            "DTC-ready with community validation signals."
        ),
        "app_store": (
            "APP STORE SIGNAL: This brand has a live consumer app in the App Store — "
            "it has shipped a real product with an active distribution channel."
        ),
    }
    _sig_type = signal.get("signal_type", "trademark")
    _type_context = _SIGNAL_TYPE_CONTEXT.get(_sig_type)
    if _type_context:
        user_message += f"{_type_context}\n\n"

    # Conviction founder match — pre-computed by the scan pipeline
    _conviction = signal.get("conviction_match")
    if _conviction:
        user_message += (
            f"CONVICTION FOUNDER MATCH: '{_conviction.get('name')}' has been identified "
            f"in this signal's filing data. Context: {_conviction.get('reason')}. "
            f"Known brands: {', '.join(_conviction.get('known_brands', [])) or 'none on record'}. "
            f"This is a Bullish conviction-list founder — score generously on founder model "
            f"and set founder confidence='known'. The brand thesis still governs bullish_score "
            f"independently, but founder_score should reflect this person's track record.\n\n"
        )

    # Signal confluence — pass multi-signal context to boost scoring appropriately
    signal_count = signal.get("signal_count", 1)
    signal_types = signal.get("signal_types", [])
    if signal_count >= 2 and signal_types:
        types_str = ", ".join(signal_types)
        user_message += (
            f"SIGNAL CONFLUENCE: {signal_count} distinct signal types detected for this brand: "
            f"[{types_str}]. Apply the confluence boost per calibration instructions.\n\n"
        )

    user_message += (
        f"Based on brand name, category, goods/services, and owner, assess the POTENTIAL "
        f"for this to be a Bullish-worthy consumer brand. Be appropriately uncertain — "
        f"we are reading tea leaves at the earliest possible signal, not a pitch deck."
    )

    try:
        message = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1600,
            # SYSTEM_PROMPT is ~7,600 tokens (the full thesis, scorecard, ~40 calibration
            # anchors) and is byte-identical on every call, so it is a textbook cache
            # target — well above Sonnet's 1,024-token minimum cacheable prefix. It is
            # billed at full rate on the first call of each 5-minute window and at ~10%
            # thereafter, which is most of the per-signal enrichment cost. Keep this block
            # the FIRST system block and do not interpolate per-signal text into it, or
            # the prefix changes and every call becomes a cache miss again.
            system=[{
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_message}],
            timeout=60,
        )

        _usage = getattr(message, "usage", None)
        if _usage is not None:
            logger.info(
                "enrich cache: read=%s created=%s uncached_in=%s out=%s",
                getattr(_usage, "cache_read_input_tokens", None),
                getattr(_usage, "cache_creation_input_tokens", None),
                getattr(_usage, "input_tokens", None),
                getattr(_usage, "output_tokens", None),
            )

        text = message.content[0].text.strip()

        # Strip markdown code fences if present
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)

        # Apply minimum tier floors — prevent genuine legal signals from being buried as cold
        _signal_types   = set(signal.get("signal_types", []))
        _has_conviction = bool(signal.get("conviction_match"))
        _has_tm         = "trademark" in _signal_types
        _has_form_d     = "delaware"  in _signal_types
        _level          = result.get("watch_level", "cold")

        try:
            _score = float(result.get("bullish_score") or 0)
        except (TypeError, ValueError):
            _score = 0.0

        if _has_conviction:
            result["watch_level"]       = "hot"
            result["tier_floor_reason"] = "conviction_match"
        elif _level == "cold" and (_has_tm or _has_form_d):
            # Floor a COLD legal signal up to WARM only when there is a reason
            # to look again. Two qualify:
            #   • TRIANGULATION — a brand filing BOTH a trademark and a Form D is
            #     being built across two independent legal channels. That is the
            #     core edge and it outranks the model's read of a sparse filing.
            #   • NEAR MISS — a single legal signal that scored within
            #     _TIER_FLOOR_NEAR_MISS of WARM. A 47 should not read the same
            #     as a 4.
            #
            # This condition used to be simply (has_trademark or has_form_d),
            # which is trivially true for EVERY trademark. It was dead code until
            # P1 started passing signal_types, and measured against the live
            # corpus it would have promoted 88% of all scored signals — including
            # 260 that scored 0 because they failed the consumer gate outright
            # (B2B, ad-supported, already-established brands). A tier every
            # signal reaches is not a tier, and it would have buried the ~11%
            # that genuinely earned WARM.
            #
            # Nothing is hidden either way: COLD signals stay in the database and
            # on the dashboard. This only governs the badge the team triages on,
            # so it ranks rather than gates — consistent with the consumer gate
            # staying default-include.
            _gate_failed = (
                _score <= _TIER_FLOOR_GATE_FAIL_AT
                or result.get("consumer_brand") is False
                or (result.get("founder_score") or {}).get("gate_passed") is False
            )
            if _gate_failed:
                # Leave it COLD. The model did not merely rank this low, it
                # rejected the premise — and no amount of corroborating paperwork
                # changes what the company is.
                result["tier_floor_reason"] = "gate_failed_no_floor"
            elif _has_tm and _has_form_d:
                result["watch_level"]       = "warm"
                result["tier_floor_reason"] = "tm_plus_form_d"
            elif _score >= _TIER_FLOOR_NEAR_MISS:
                result["watch_level"]       = "warm"
                result["tier_floor_reason"] = "trademark_near_miss" if _has_tm else "form_d_near_miss"

        result["enriched"] = True
        return result

    except json.JSONDecodeError as e:
        return {"enriched": False, "error": f"JSON parse error: {e}", "bullish_score": None}
    except Exception as e:
        return {"enriched": False, "error": str(e), "bullish_score": None}


_FOUNDER_RESCORE_PROMPT = """You are scoring a startup founder against Bullish's 5-signal Jockey model.
You have been given real LinkedIn data for the founder. Use it to produce an accurate, grounded score.

SCORING MODEL (100 points total):

1. Chip-on-Shoulder (0-30): Does the founder have a personal, almost obsessive reason to build THIS brand?
   Rubric: 23-30=lived the problem viscerally (health crisis, identity struggle, personal injustice) |
   15-22=deep personal affinity with category | 8-14=professional proximity but no visible personal pull |
   0-7=no detectable personal connection

2. Category Proximity (0-25): Has the founder worked directly in or adjacent to this category?
   Rubric: 20-25=senior role at employer in exact category | 15-19=founder IS the target customer |
   10-14=prior company in same/adjacent category | 5-9=academic discipline aligns | 0-4=no detectable proximity

3. Magnetic Signal (0-20): Is there evidence this founder can build an audience or community?
   Rubric: 16-20=demonstrated following (LinkedIn 10k+, content creator, press coverage) |
   11-15=moderate community signals | 6-10=some visibility | 0-5=no detectable signal

4. Pedigree (0-15): Has the founder worked at or built a recognized brand, startup, or institution?
   Rubric: 13-15=Tier 1 brand/startup (FAANG, top consumer brand, unicorn) |
   9-12=Tier 2 recognized operator | 5-8=emerging brand or solid startup | 0-4=no recognizable pedigree

5. Thesis Clarity (0-10): Does their background suggest they have a clear POV on the category?
   Rubric: 8-10=career arc clearly leads to THIS brand | 5-7=reasonable fit | 0-4=unclear connection

TIERS: ≥75=HIGH_PRIORITY | ≥50=WATCH_LIST | ≥25=WEAK_SIGNAL | <25=PASS

ADDITIONAL SCORING SIGNALS (apply as score adjustments on top of the 5-signal model above):

BRAND EXIT BACKGROUND (critical signal — add to pedigree and chip_on_shoulder scores):
- Founder previously worked at (not necessarily founded) a consumer brand that was later acquired or had a notable exit: +12 to +15 points total. This is a top-tier signal — they have seen how it is done from the inside. Add primarily to pedigree (+8) and chip_on_shoulder (+5).
- Founder personally founded and exited (sold) a company: add another +8 to +10 points on top of the above. Add primarily to chip_on_shoulder (+5) and pedigree (+4).
- Example: someone who was head of marketing at Rx Bar before Kellogg's acquired it = +12 total. Someone who founded and sold their own brand = +18 to +20 combined.
- No exit background found: 0 adjustment.

CATEGORY EXPERTISE (apply to category_proximity score):
- 3+ years working in the exact same category (e.g., launching a pet food brand after running operations at a pet food company): +10 to +12 points to category_proximity.
- Adjacent category experience (F&B brand launching into wellness): +5 to +7 points to category_proximity.
- No relevant category experience: 0 adjustment.

CO-FOUNDER / TEAM (apply as a holistic adjustment to total):
- Strong 2–3 person founding team with complementary skills (operator + creative, or brand + tech): +5 to +8 points to total.
- Solo founder with strong background: neutral (0).
- Solo founder with limited background: –5 points to total.

BULLISH PORTFOLIO FIT (apply to thesis_clarity score):
- Background mirrors successful Bullish portfolio founders (scrappy, brand-obsessed, category insiders): +5 points to thesis_clarity.

NOTE: The 5-signal model caps are soft when exit background or strong category expertise is present. A pedigree score can exceed its nominal max of 15 if the evidence is exceptional — cap the individual signal scores at their max values, but reflect the strength in total and flags.

Return ONLY valid JSON — no markdown, no explanation:
{
  "founder": {
    "name": "<full name>",
    "background": "<1-2 sentences: relevant experience and innate category advantage>",
    "prior_companies": ["<company (role)>"],
    "confidence": "known"
  },
  "founder_score": {
    "gate_passed": true,
    "total": <integer 0-100>,
    "tier": "<HIGH_PRIORITY|WATCH_LIST|WEAK_SIGNAL|PASS>",
    "action": "<recommended next action>",
    "breakdown": {
      "chip_on_shoulder":   { "score": <0-30>, "max": 30, "confidence": "<high|medium|low>", "flags": ["<observation>"] },
      "category_proximity": { "score": <0-25>, "max": 25, "confidence": "<high|medium|low>", "flags": ["<observation>"] },
      "magnetic_signal":    { "score": <0-20>, "max": 20, "confidence": "<high|medium|low>", "flags": ["<observation>"] },
      "pedigree":           { "score": <0-15>, "max": 15, "confidence": "<high|medium|low>", "flags": ["<observation>"] },
      "thesis_clarity":     { "score": <0-10>, "max": 10, "confidence": "<high|medium|low>", "flags": ["<observation>"] }
    },
    "human_review_flags": ["<anything needing human confirmation>"],
    "linkedin_enriched": true
  }
}"""


def rescore_founder_with_linkedin(
    brand_name: str,
    category: str,
    one_line_thesis: str,
    founder_name: str,
    linkedin_context: dict,
    discovery_result: dict = None,
) -> dict:
    """
    Re-score the founder section of an enrichment using real LinkedIn data
    from Proxycurl.  Makes a targeted Claude call (much cheaper than a full
    enrichment re-run) and returns updated founder + founder_score dicts.

    Also incorporates exit background data from discovery_result and any
    pre-injected text fields from linkedin_context.

    Returns {"founder": {...}, "founder_score": {...}, "linkedin_enriched": True}
    or {"error": "...", "linkedin_enriched": False} on failure.
    """
    try:
        client = _get_client()
    except RuntimeError as e:
        return {"error": str(e), "linkedin_enriched": False}

    # Format LinkedIn data clearly for Claude
    exp_lines = "\n".join(
        f"  - {e.get('title', '?')} at {e.get('company', '?')} "
        f"({e.get('start', '?')}–{e.get('end', '?')})"
        for e in linkedin_context.get("experiences", [])
    ) or "  (no work history found)"

    edu_lines = "\n".join(
        f"  - {e.get('school', '?')}: {e.get('degree', '')} {e.get('field', '')}".strip()
        for e in linkedin_context.get("education", [])
    ) or "  (no education found)"

    follower_str = (
        f"{linkedin_context['follower_count']:,}"
        if linkedin_context.get("follower_count")
        else "unknown"
    )

    # Build augmented profile text with exit background
    profile_text = f"""Score this founder against Bullish's 5-signal model using their real LinkedIn data.

Brand: {brand_name}
Category: {category}
Thesis: {one_line_thesis or "Unknown"}

FOUNDER LINKEDIN DATA:
Name: {founder_name}
Headline: {linkedin_context.get("headline") or "Not available"}
LinkedIn followers: {follower_str}
Summary: {linkedin_context.get("summary") or "Not available"}

Work history:
{exp_lines}

Education:
{edu_lines}"""

    # Append exit background — from pre-injected field or discovery_result
    exit_bg_text = linkedin_context.get("_exit_background_text")
    if exit_bg_text:
        profile_text += f"\n\n{exit_bg_text}"
    elif discovery_result:
        exit_info = discovery_result.get("exit_background", {})
        if exit_info.get("has_exit_background") and exit_info.get("details"):
            profile_text += f"\n\nBRAND EXIT BACKGROUND: {exit_info['details']}"
        else:
            profile_text += "\n\nBRAND EXIT BACKGROUND: No prior exit background found."
    else:
        profile_text += "\n\nBRAND EXIT BACKGROUND: No prior exit background found."

    profile_text += "\n\nScore this founder using the 5-signal model. Use the LinkedIn data as ground truth — this is real, not inferred."

    user_message = profile_text

    try:
        message = client.messages.create(
            model="claude-haiku-4-5",   # cheaper model — founder scoring only
            max_tokens=800,
            system=_FOUNDER_RESCORE_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = message.content[0].text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)
        result["linkedin_enriched"] = True
        return result

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {e}", "linkedin_enriched": False}
    except Exception as e:
        return {"error": str(e), "linkedin_enriched": False}



# ─── Re-deriving stored verdicts after a floor change ─────────────────────────

# Every value tier_floor_reason has ever held for a signal the floor PROMOTED or
# explicitly declined to promote. Their common property: the model's own verdict
# underneath was "cold". "trademark"/"form_d" are from the pre-2aa951a floor that
# promoted 88% of the corpus.
_FLOOR_APPLIED_TO_COLD = {
    "trademark", "form_d", "tm_plus_form_d",
    "trademark_near_miss", "form_d_near_miss", "gate_failed_no_floor",
}


def rederive_watch_levels(dry_run: bool = False) -> dict:
    """
    Re-apply the CURRENT tier floor to signals that were scored under an older one.

    A floor change only affects signals scored AFTER it ships; the corpus keeps
    whatever verdict it was given. So on 2026-08-03 the dashboard still showed
    thousands of rejected signals as WARM — a copper mining explorer and a
    L'Oreal corporate filing among them — long after the rule that promoted them
    was narrowed. Shipping the fix and fixing the data are two different jobs.

    Costs nothing: no Anthropic calls. It re-applies arithmetic to scores that
    were already paid for.

    HOW IT RECOVERS THE MODEL'S OWN VERDICT. The stored watch_level is
    post-floor, so it cannot be read back directly — but tier_floor_reason
    records whether a floor touched it, and every floor only ever fires on a
    COLD base. So reason ∈ _FLOOR_APPLIED_TO_COLD ⟹ the model said cold. Absent
    reason ⟹ nothing was floored and the stored level IS the model's verdict.
    That is exact, and strictly better than recomputing the level from the score:
    the hot/warm cutoffs live in the prompt, so recomputing here would silently
    re-adjudicate signals the floor never touched.

    Two things it will not do:
      • Never touches a conviction_match signal. Conviction outranks everything
        including alumni (invariant 2); its HOT is not the floor's to revisit.
      • Never demotes on a guess. A WARM with no recorded reason is left alone
        and counted as ambiguous, because there is no evidence a floor put it
        there.

    Lives here, beside the rules it mirrors, and is called by BOTH `flask
    rederive-tiers` and POST /api/admin/rederive-tiers — one implementation, so
    the two entry points cannot drift the way this codebase's scan paths
    repeatedly have (see CLAUDE.md, "the dual-path divergence").
    """
    import json as _json
    from ..extensions import db
    from ..models.item import Item
    from ..models.signal_event import SignalEvent
    from .confluence import normalize_brand

    # brand_key -> {signal_type}, so the floor can see triangulation.
    pairs = {}
    for bk, st in SignalEvent.query.with_entities(
            SignalEvent.brand_key, SignalEvent.signal_type):
        pairs.setdefault(bk, set()).add(st)

    counts = {"examined": 0, "changed": 0, "unchanged": 0, "ambiguous": 0,
              "conviction_skipped": 0, "to_warm": 0, "to_cold": 0,
              "dry_run": dry_run}

    rows = (Item.query
            .filter(Item.item_type == "signal")
            .filter(Item.description.contains('"enrichment"'))
            .yield_per(500))

    for item in rows:
        try:
            meta = _json.loads(item.description or "{}")
        except (ValueError, TypeError):
            continue
        enr = (meta or {}).get("enrichment") or {}
        if not enr.get("enriched") or enr.get("bullish_score") is None:
            continue

        counts["examined"] += 1
        reason = enr.get("tier_floor_reason")
        stored = enr.get("watch_level")

        if reason == "conviction_match":
            counts["conviction_skipped"] += 1
            continue
        if reason in _FLOOR_APPLIED_TO_COLD:
            base = "cold"
        elif stored == "warm":
            counts["ambiguous"] += 1      # no evidence a floor put it here
            continue
        else:
            base = stored

        try:
            score = float(enr.get("bullish_score") or 0)
        except (TypeError, ValueError):
            score = 0.0

        types = pairs.get(normalize_brand(meta.get("company_name") or item.title or ""), set())
        has_tm, has_form_d = "trademark" in types, "delaware" in types

        level, new_reason = base, None
        if base == "cold" and (has_tm or has_form_d):
            if (score <= _TIER_FLOOR_GATE_FAIL_AT
                    or enr.get("consumer_brand") is False
                    or (enr.get("founder_score") or {}).get("gate_passed") is False):
                new_reason = "gate_failed_no_floor"
            elif has_tm and has_form_d:
                level, new_reason = "warm", "tm_plus_form_d"
            elif score >= _TIER_FLOOR_NEAR_MISS:
                level = "warm"
                new_reason = "trademark_near_miss" if has_tm else "form_d_near_miss"

        if level == stored and new_reason == reason:
            counts["unchanged"] += 1
            continue

        if level != stored:
            counts["changed"] += 1
            counts["to_" + level] = counts.get("to_" + level, 0) + 1
        if not dry_run:
            enr["watch_level"] = level
            enr["tier_floor_reason"] = new_reason
            meta["enrichment"] = enr
            item.description = _json.dumps(meta, separators=(",", ":"))

    if dry_run:
        db.session.rollback()
    else:
        db.session.commit()
    return counts
