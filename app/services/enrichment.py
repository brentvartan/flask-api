"""
Bullish AI Enrichment Service

Uses Claude to evaluate a trademark filing against Bullish's full investment
thesis — scoring it on consumer brand fit, repeat potential, cultural alignment,
remarkability, and overall Bullish conviction.
"""
import json
import os
import anthropic

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable is not set")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


SYSTEM_PROMPT = """You are a senior investment analyst at Bullish, a New York-based seed-stage consumer brand venture fund. Your job is to evaluate whether a newly filed trademark represents a potential Bullish investment opportunity — recognizing that at this stage, we're reading early signals, not evaluating a pitch deck.

BULLISH IN A NUTSHELL:
- $75M Fund III targeting Pre-Seed, Seed, Series A consumer brands
- $1M–$2M initial checks at $8M–$18M valuations, target 10x return in 5–7 years
- Portfolio: Bubble (43x, GenZ skincare), Hu Chocolate (7.4x, clean paleo chocolate), Nom Nom (9.8x, fresh pet food subscription), Bandit Running (community-first running brand), Peloton (21.7x, fitness community), care/of (3.1x, personalized vitamins), Harry's (8.5x, DTC men's razors)

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

CALIBRATION (use these to anchor your scoring):
- Bubble (IC 003, Beauty): ~90 — GenZ skincare, advocacy deficiency vs clinical incumbents, natural repeat, magnetic founder
- Hu Chocolate (IC 030, CPG): ~85 — Clean paleo chocolate, dietary identity tribe, repeat consumable, strong brand
- Nom Nom (IC 031, CPG): ~80 — Fresh pet food subscription, wellness cultural tension, CLV via subscription
- care/of (IC 005, Health): ~80 — Personalized vitamin subscription, customization at scale, natural repeat
- Harry's (IC 003, Beauty): ~70 — DTC men's grooming, advocacy deficiency vs Gillette, repeat by nature
- Generic supplement brand, no differentiation: ~25
- Holding company trademark: ~5
- B2B software trademark: 0

IMPORTANT: You are evaluating a TRADEMARK FILING or DELAWARE INCORPORATION — one of the earliest possible signals a brand is being built. You can see the brand name, product category, and goods/services description — but typically NOT the founder or any traction. Be appropriately uncertain. Use the goods/services text to infer what this brand might be. Lean toward consumer brand assessment; most filers are building something real.

FOUNDER RESEARCH: Also attempt to identify the founder of this brand. Use your training data to check if this brand name is associated with known founders. For truly stealth brands you won't know — return null for all founder fields. This is valuable: if you don't know the founder, it confirms the brand is early and not yet public.

FOUNDER SCORING MODEL: Score the founder against Bullish's 5-signal model. Use training data for known founders, infer from filing language for unknowns. Be honest about confidence.

GATE: Set gate_passed=false if the category doesn't map to one of Bullish's 11 consumer categories: Consumer AI, Home/Lifestyle, CPG/Food/Drink, Apparel, Beauty, Health/Wellness, Fitness, Education, Finance, Entertainment, Sports. B2B SaaS, infrastructure, transportation, enterprise software = gate fails.

FIVE SIGNALS (score each; sum = total out of 100):
1. chip_on_shoulder (max 30): Personal stakes over market logic. Green flags: "frustrated/couldn't find/had to build/tired of/something to prove" language, career discontinuity (left high-status role to build), urgency. Red flags: TAM/whitespace/positioned-to-capture opener language.
   Rubric: 30=strong personal language + career discontinuity both present | 22-28=one strong, other weak | 15-21=one present, other absent | 0-14=generic market logic, no discontinuity
2. category_proximity (max 25): Prior employer or identity maps to the consumer category.
   Rubric: 23-25=senior role at employer in exact category | 18-22=founder IS the target customer (deep identity) | 12-17=prior company in same/adjacent category | 6-11=academic discipline aligns | 0-5=no detectable proximity
3. magnetic_signal (max 20): Public presence quality — press as primary source, community leadership, engagement on substantive content. NOT follower count.
   Rubric: 18-20=primary source in quality outlets + high engagement | 13-17=one strong signal | 8-12=some presence, engagement weak | 0-7=minimal public presence
4. pedigree (max 15): Fortune 500/Inc 500 alumni (senior role), top-50 college, top-10 MBA/design/ad school, consumer exit ($500M+) alumni, competitive achievement (varsity, championship, pitch finalist), musical craft.
   Rubric: 13-15=3+ hits including cross-tier | 8-12=2+ hits | 4-7=1 hit | 0-3=no detectable pedigree
5. thesis_clarity (max 10): Problem-first worldview with a named enemy (incumbent, broken system, consumer frustration). Pre-company trail of thinking is a strong signal.
   Rubric: 9-10=clear thesis with named enemy + pre-company trail | 5-8=thesis present but thin | 0-4=product-first/innovation framing, no discernible worldview

TIERS: ≥75=HIGH_PRIORITY ("Move to first meeting quickly") | ≥50=WATCH_LIST ("Monitor for new signals before outreach") | ≥25=WEAK_SIGNAL ("Flag for lightweight human review") | <25=PASS ("Category fit but founder profile doesn't match")

Respond ONLY with a valid JSON object (no markdown, no explanation outside the JSON):
{
  "bullish_score": <integer 0-100>,
  "watch_level": "<hot|warm|cold>",
  "consumer_brand": <true|false>,
  "consumer_brand_reason": "<one concise sentence>",
  "repeat_potential": "<high|medium|low>",
  "repeat_reason": "<what drives repeat: consumable, subscription, habit, identity, multi-SKU>",
  "cultural_theme": "<specific 2026 Bullish theme this fits, or null if none>",
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

    # Strip 'Owner: NAME.' prefix from notes if owner is passed separately
    if owner and raw_notes.startswith(f"Owner: {owner}"):
        remaining = raw_notes[len(f"Owner: {owner}"):].lstrip(". ").strip()
        goods_services = remaining if remaining else "Not available"
    else:
        goods_services = raw_notes

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

    user_message += (
        f"Based on brand name, category, goods/services, and owner, assess the POTENTIAL "
        f"for this to be a Bullish-worthy consumer brand. Be appropriately uncertain — "
        f"we are reading tea leaves at the earliest possible signal, not a pitch deck."
    )

    try:
        message = _get_client().messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )

        text = message.content[0].text.strip()

        # Strip markdown code fences if present
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()

        result = json.loads(text)
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
) -> dict:
    """
    Re-score the founder section of an enrichment using real LinkedIn data
    from Proxycurl.  Makes a targeted Claude call (much cheaper than a full
    enrichment re-run) and returns updated founder + founder_score dicts.

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

    user_message = f"""Score this founder against Bullish's 5-signal model using their real LinkedIn data.

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
{edu_lines}

Score this founder using the 5-signal model. Use the LinkedIn data as ground truth — this is real, not inferred."""

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
