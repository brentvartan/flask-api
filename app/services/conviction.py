"""
conviction.py
-------------
Bullish conviction founders list — a hand-curated roster of named jockeys
Bullish has high conviction on: repeat consumer founders with exits, operators
who built iconic consumer brands and are known to be building again.

These names are matched against the free text already flowing through signals:
  - USPTO trademark owner/signatory fields
  - Form D "related persons" fields
  - Press release bylines and body text (newswire signals)
  - Product Hunt / App Store creator fields

A conviction match surfaces in the signal as `conviction_match` and bumps the
signal to the top of the digest. It is NEVER a gate or exclusion — unknown
first-timers still surface, just ranked below conviction matches.

---
HOW TO ADD A FOUNDER:
  Add an entry to CONVICTION_FOUNDERS. Format:
    "Firstname Lastname": ("one-line reason why Bullish has conviction",
                           ["Known Brand 1", "Known Brand 2"]),
  Names are matched case-insensitively. BOTH first and last name must appear
  in the target text (prevents false matches on common first names).
  Use the name as it would appear in a legal filing or press byline.
"""
import re
import logging

logger = logging.getLogger(__name__)

# ── Conviction founders list ──────────────────────────────────────────────────
# ~70 founders Bullish would back on sight regardless of brand thesis.
# Categories: (1) exits who may be building again, (2) iconic operators,
# (3) Bullish portfolio founders, (4) known category authorities.

CONVICTION_FOUNDERS: dict[str, tuple[str, list[str]]] = {

    # ── Repeat founders with major consumer exits ──────────────────────────
    "Brynn Putnam":       ("Mirror → lululemon $500M exit; now building Board (consumer gaming/family)", ["Mirror", "Board"]),
    "Peter Rahal":        ("RxBar → Kellogg's $600M exit; category-defining protein bar founder", ["RxBar"]),
    "Gail Becker":        ("CAULIPOWER — one of fastest-growing frozen food brands; media exec turned founder", ["CAULIPOWER"]),
    "Kara Goldin":        ("Hint Water — built to $150M+ revenue DTC; category creator in better-for-you beverages", ["Hint Water"]),
    "Blake Mycoskie":     ("TOMS — invented the buy-one-give-one model; multiple consumer ventures since", ["TOMS"]),
    "Andy Dunn":          ("Bonobos → Walmart $310M exit; wrote the book on DTC menswear", ["Bonobos"]),
    "Ido Leffler":        ("Yes To → global scale; serial consumer CPG founder", ["Yes To"]),
    "Seth Goldman":       ("Honest Tea → Coca-Cola acquisition; mission-driven beverage pioneer", ["Honest Tea"]),
    "Josh Tetrick":       ("JUST Inc — plant-based food category pioneer; repeat challenger-brand founder", ["JUST", "Hampton Creek"]),
    "Ben McKean":         ("Hungryroot — personalized grocery/nutrition; repeat consumer founder", ["Hungryroot"]),
    "Mark Samuel":        ("Lemon Perfect — celebrity-backed hydration; clean-label beverage operator", ["Lemon Perfect"]),

    # ── Bullish portfolio founders ─────────────────────────────────────────
    "Jeff Raider":        ("Harry's co-founder; Warby Parker co-founder; serial DTC category disruptor", ["Harry's", "Warby Parker"]),
    "Andy Katz-Mayfield": ("Harry's co-founder; knows men's grooming DTC from inception to IPO", ["Harry's"]),
    "Neil Blumenthal":    ("Warby Parker co-founder; pioneer of DTC + social mission model", ["Warby Parker"]),
    "Dave Gilboa":        ("Warby Parker co-founder; vision care + retail disruption playbook", ["Warby Parker"]),
    "Jordan Schenck":     ("Consumer growth executive; Spotify, Impossible Foods — magnetic brand-builder pedigree", []),
    "Mark Lynn":          ("care/of co-founder; personalized vitamin subscription pioneer", ["care/of"]),
    "Craig Elbert":       ("care/of co-founder; Bonobos before that — DTC operator playbook", ["care/of", "Bonobos"]),

    # ── Iconic consumer brand builders ────────────────────────────────────
    "Emily Weiss":        ("Glossier founder; defined Gen Z beauty and DTC brand-building", ["Glossier", "Into The Gloss"]),
    "Mike Cessario":      ("Liquid Death founder; redefined water/CPG through brand irreverence", ["Liquid Death"]),
    "Jing Gao":           ("Fly by Jing founder; authentic food identity brand with chip-on-shoulder origin story", ["Fly by Jing"]),
    "Katy Hearn":         ("Alani Nu co-founder; community-before-product DTC playbook in functional beverages", ["Alani Nu"]),
    "Ben Goodwin":        ("Olipop co-founder; functional soda — gut health tension + massive repeat", ["Olipop"]),
    "David Lester":       ("Olipop co-founder; prior functional food ventures", ["Olipop"]),
    "Andrew Dudum":       ("Hims & Hers founder; DTC health/wellness with massive brand — men's and women's health", ["Hims", "Hers"]),
    "Ariel Kaye":         ("Parachute Home founder; quality-over-quantity home goods DTC; lifestyle brand builder", ["Parachute"]),
    "Jennifer Hyman":     ("Rent the Runway co-founder; circular fashion economy pioneer", ["Rent the Runway"]),
    "Katrina Lake":       ("Stitch Fix founder; personal styling + data — DTC fashion subscription", ["Stitch Fix"]),

    # ── Consumer hardware / connected products ─────────────────────────────
    "Tony Fadell":        ("iPod/Nest inventor; consumer hardware product genius", ["Nest", "Apple"]),
    "Yves Behar":         ("fuseproject — consumer product design authority; hardware-meets-brand", []),
    "Scott Bedbury":      ("Nike/Starbucks brand architect; when he's building, Bullish listens", []),

    # ── Food & beverage category authorities ──────────────────────────────
    "Nathan Puksta":      ("Filament founder; ex-Kiehl's / L'Oréal / Baxter — deep haircare category authority", ["Filament", "Kiehl's"]),
    "Nick Green":         ("Thrive Market co-founder; membership-model natural food pioneer", ["Thrive Market"]),
    "Taylor Holiday":     ("Common Thread Collective; knows DTC brand-building + performance better than anyone", []),
    "Greg Sewitz":        ("Magic Spoon co-founder; better-for-you cereal — food identity play", ["Magic Spoon"]),
    "Gabi Lewis":         ("Magic Spoon co-founder; Exo Protein before that — serial food identity founder", ["Magic Spoon", "Exo Protein"]),
    "Aihui Ong":          ("Love With Food — subscription snack box pioneer; food discovery model", ["Love With Food"]),

    # ── Beauty / personal care authorities ────────────────────────────────
    "Charlotte Tilbury":  ("Charlotte Tilbury — built a global beauty brand from scratch, then sold to Puig", ["Charlotte Tilbury"]),
    "Annie Jackson":      ("Credo Beauty co-founder; clean beauty retail pioneer", ["Credo Beauty"]),
    "Stephanie Phair":    ("Farfetch, Net-a-Porter — luxury fashion + DTC intersection authority", []),

    # ── Wellness / health ─────────────────────────────────────────────────
    "Halle Tecco":        ("Rock Health founder; consumer health + femtech authority", ["Rock Health"]),
    "Anne Wojcicki":      ("23andMe founder; consumer genomics — health data as product", ["23andMe"]),
    "Christy Turlington": ("Every Mother Counts; maternal health advocacy → brand authority", []),

    # ── Pet / premium category ─────────────────────────────────────────────
    "Renaldo Webb":       ("Wild One — premium pet accessories; consumer identity brand in pet space", ["Wild One"]),

    # ── Operators with proven exit pedigree in adjacent roles ─────────────
    "Alexa von Tobel":    ("LearnVest → Northwestern Mutual; serial consumer fintech founder", ["LearnVest"]),
    "Max Levchin":        ("Affirm — consumer-first financial product at scale", ["Affirm"]),
    "Tina Sharkey":       ("iVillage, Brandless — consumer community + DTC brand", ["Brandless"]),

    # ── Additional acquired-brand founders (exits not yet in list) ─────────
    "Steph Korey":        ("Away co-founder; 'lifestyle luggage' category creator — DTC brand storytelling at scale", ["Away"]),
    "Jen Rubio":          ("Away co-founder + President; designed the product line and built the brand voice", ["Away"]),
    "Tyler Haney":        ("Outdoor Voices founder; activewear built on 'Doing Things' culture — heavily VC-backed category maker", ["Outdoor Voices"]),
    "Rachel Drori":       ("Daily Harvest founder; plant-based meal delivery at $100M+ ARR — food-as-identity DTC leader", ["Daily Harvest"]),
    "Nick Taranto":       ("Plated co-founder; ~$200M Albertsons exit — first major meal-kit brand acquisition", ["Plated"]),
    "Josh Hix":           ("Plated co-founder; built and sold the meal-kit company that defined the category for acquirers", ["Plated"]),
    "Katia Beauchamp":    ("Birchbox co-founder; invented the beauty subscription model before it was a category", ["Birchbox"]),
    "Hayley Barna":       ("Birchbox co-founder; built beauty discovery subscription to 1M+ subscribers", ["Birchbox"]),
    "Morgan Hermand-Waiche": ("Adore Me founder; DTC lingerie at scale — sold to Walmart ~$400M", ["Adore Me"]),
    "Jon Sebastiani":     ("KRAVE Jerky founder → Hershey acquisition; now runs Sonoma Brands portfolio — serial better-snacking builder", ["KRAVE Jerky", "Sonoma Brands"]),
    "Philip Krim":        ("Casper co-founder + CEO; DTC mattress category creator, IPO'd then went private", ["Casper"]),
    "Jeff Chapin":        ("Casper co-founder + Chief Product Officer; industrial designer who made the mattress a brand", ["Casper"]),
    "Sophia Amoruso":     ("Nasty Gal founder; built $100M+ fashion empire from eBay store — culture-first DTC pioneer", ["Nasty Gal", "Girlboss"]),
    "Sasha Plavsic":      ("ILIA Beauty founder; defined the clean + performance beauty intersection before the category went mainstream", ["ILIA Beauty"]),
    "Jesse Merrill":      ("Good Culture co-founder; reinvented cottage cheese as a modern protein brand in a stagnant category", ["Good Culture"]),
    "Gabby Slome":        ("Ollie co-founder; premium fresh pet food subscription — first major premium DTC pet brand", ["Ollie"]),
    "Ryan Babenzien":     ("JOLIE Skin Co. founder; prior Greats sneakers co-founder — serial DTC product company builder", ["JOLIE", "Greats"]),

    # ── Early operators / #2–4 from iconic exits — exit-pedigree, below founder tier ──
    "Henry Davis":        ("Glossier President 2016–2021; ran the entire P&L while Emily Weiss built the brand — category-level operator", ["Glossier"]),
    "Neil Parikh":        ("Casper co-founder + COO; built operations and supply chain for a 9-figure DTC brand", ["Casper"]),
    "Luke Sherwin":       ("Casper co-founder + Chief Creative Officer; defined the visual language of DTC home goods", ["Casper"]),
    "Jonathan Neman":     ("Sweetgreen co-founder + CEO; scaled ingredient-transparent fast-casual to a public company", ["Sweetgreen"]),
    "Nicolas Jammet":     ("Sweetgreen co-founder; culinary + supply-chain architect behind the most iconic fast-casual brand", ["Sweetgreen"]),
    "Nathaniel Ru":       ("Sweetgreen co-founder; brand and community architect — defined Sweetgreen's cultural identity", ["Sweetgreen"]),

    # ── Poppi ─────────────────────────────────────────────────────────────────
    "Allison Ellsworth": ("Poppi co-founder; PepsiCo ~$1.95B exit — after the sale, widely expected to build again", ["Poppi"]),
    "Stephen Ellsworth": ("Poppi co-founder; built functional soda brand from home to $1.95B PepsiCo acquisition", ["Poppi"]),

    # ── Athletic Brewing ──────────────────────────────────────────────────────
    "Bill Shufelt":      ("Athletic Brewing co-founder; created the NA craft beer category — Keurig Dr Pepper exit", ["Athletic Brewing"]),
    "John Walker":       ("Athletic Brewing co-founder + Master Brewer; built NA beer from scratch to category leader", ["Athletic Brewing"]),

    # ── Gymshark ──────────────────────────────────────────────────────────────
    "Ben Francis":       ("Gymshark founder; built from his parents' garage to $1.3B+ — community-first fitness brand", ["Gymshark"]),

    # ── Whoop ─────────────────────────────────────────────────────────────────
    "Will Ahmed":        ("Whoop founder; pioneered physiological monitoring wearables for serious athletes", ["Whoop"]),

    # ── Eight Sleep ───────────────────────────────────────────────────────────
    "Matteo Franceschetti": ("Eight Sleep co-founder; turned sleep into a performance category — Pod cover at $2K+", ["Eight Sleep"]),
    "Alexandra Zatarain":   ("Eight Sleep co-founder + Chief Brand Officer; built the sleep performance brand narrative", ["Eight Sleep"]),

    # ── Vuori ─────────────────────────────────────────────────────────────────
    "Joe Kudla":         ("Vuori founder; premium performance apparel with California lifestyle brand identity, $4B val", ["Vuori"]),

    # ── BarkBox / BARK ────────────────────────────────────────────────────────
    "Matt Meeker":       ("BarkBox co-founder; built the pet subscription category — BARK went public 2021", ["BarkBox", "BARK"]),

    # ── Ritual ────────────────────────────────────────────────────────────────
    "Katerina Schneider": ("Ritual founder; redefined vitamins with radical transparency and design-forward identity", ["Ritual"]),

    # ── Caraway ───────────────────────────────────────────────────────────────
    "Jordan Nathan":     ("Caraway founder; made cookware a brand story — DTC home goods with aesthetic identity", ["Caraway"]),

    # ── Seed ──────────────────────────────────────────────────────────────────
    "Ara Katz":          ("Seed co-founder; turned probiotic science into a design-forward consumer wellness brand", ["Seed"]),
    "Raja Dhir":         ("Seed co-founder; microbiome science + brand — built Seed into a category-defining wellness co", ["Seed"]),

    # ── Banza ─────────────────────────────────────────────────────────────────
    "Brian Rudolph":     ("Banza co-founder; chickpea pasta from dorm room to Sovos Brands acquisition", ["Banza"]),
    "Scott Rudolph":     ("Banza co-founder; food identity brand with mission and mass retail distribution", ["Banza"]),

    # ── Simple Mills ──────────────────────────────────────────────────────────
    "Katlin Smith":      ("Simple Mills founder; built clean-label snacks/baking category — almond flour pioneer", ["Simple Mills"]),

    # ── Lovevery ──────────────────────────────────────────────────────────────
    "Jessica Rolph":     ("Lovevery co-founder; science-based children's toys subscription — premium parenting brand", ["Lovevery"]),
    "Roderick Morris":   ("Lovevery co-founder; The Play Kits — developmental learning subscription for parents", ["Lovevery"]),

    # ── Graza ─────────────────────────────────────────────────────────────────
    "Andrew Benin":      ("Graza founder; turned olive oil into a brand with a squeeze bottle and DTC food identity", ["Graza"]),

    # ── Our Place ─────────────────────────────────────────────────────────────
    "Shiza Shahid":      ("Our Place co-founder; Always Pan — multicultural cooking brand with identity-driven DTC", ["Our Place"]),
    "Amir Tehrani":      ("Our Place co-founder; design-forward cookware built on cultural storytelling", ["Our Place"]),

    # ── Super Coffee ──────────────────────────────────────────────────────────
    "Jordan DeCicco":    ("Super Coffee co-founder; protein-spiked coffee — family startup built to meaningful scale", ["Super Coffee"]),
    "Jake DeCicco":      ("Super Coffee co-founder; operational muscle behind the DeCicco family's beverage brand", ["Super Coffee"]),
    "Jim DeCicco":       ("Super Coffee co-founder; CPG distribution and retail strategy — family-run to exit", ["Super Coffee"]),

    # ── Lalo ──────────────────────────────────────────────────────────────────
    "Greg Davidson":     ("Lalo co-founder; modern baby brand — design-forward products for millennial parents", ["Lalo"]),
    "Michael Wieder":    ("Lalo co-founder; Rockets of Awesome before that — serial DTC brand builder for parents", ["Lalo", "Rockets of Awesome"]),

    # ── Mid-Day Squares ───────────────────────────────────────────────────────
    "Nick Saltarelli":   ("Mid-Day Squares co-founder; built a snack brand in public — radical content transparency", ["Mid-Day Squares"]),
    "Jake Karls":        ("Mid-Day Squares co-founder; Chief Vibes Officer — turned brand-building into entertainment", ["Mid-Day Squares"]),

    # ── Stasher ───────────────────────────────────────────────────────────────
    "Kat Nouri":         ("Stasher founder; platinum silicone reusable bags — sustainability + design consumer brand", ["Stasher"]),

    # ── Primal Kitchen ────────────────────────────────────────────────────────
    "Mark Sisson":       ("Primal Kitchen founder; Kraft Heinz $200M exit — paleo/keto condiments category creator", ["Primal Kitchen"]),

    # ── The Farmer's Dog ──────────────────────────────────────────────────────
    "Jonathan Regev":    ("The Farmer's Dog co-founder; fresh pet food subscription — $1.8B+ valuation", ["The Farmer's Dog", "Nom Nom"]),
    "Brett Podolsky":    ("The Farmer's Dog co-founder; disrupted kibble with human-grade fresh food subscription", ["The Farmer's Dog", "Nom Nom"]),

    # ── Chomps ────────────────────────────────────────────────────────────────
    "Pete Maldonado":    ("Chomps co-founder; better-for-you meat snacks built to $200M+ revenue DTC-first", ["Chomps"]),
    "Rashid Ali":        ("Chomps co-founder; clean-label meat snacks with DTC-to-retail crossover playbook", ["Chomps"]),

    # ── Chobani ───────────────────────────────────────────────────────────────
    "Hamdi Ulukaya":     ("Chobani founder; built Greek yogurt into a $10B brand — immigrant founder origin story", ["Chobani"]),

    # ── Bombas ────────────────────────────────────────────────────────────────
    "David Heath":       ("Bombas co-founder; socks + social mission — buy-one-give-one model, $100M+ revenue", ["Bombas"]),
    "Randy Goldberg":    ("Bombas co-founder; premium socks with mission-driven brand identity — Shark Tank to scale", ["Bombas"]),

    # ── Figs ──────────────────────────────────────────────────────────────────
    "Trina Spear":       ("Figs co-founder + CEO; reinvented medical scrubs as a fashion brand — IPO'd 2021", ["Figs"]),

    # ── HexClad ───────────────────────────────────────────────────────────────
    "Danny Winer":       ("HexClad co-founder; hybrid cookware + Gordon Ramsay — $500M+ premium kitchen brand", ["HexClad"]),
    "Cole Rubin":        ("HexClad co-founder; built HexClad to $500M+ revenue in the premium cookware category", ["HexClad"]),

    # ── Kodiak Cakes ──────────────────────────────────────────────────────────
    "Joel Clark":        ("Kodiak Cakes founder; protein pancakes from family wagon to $200M+ brand + Shark Tank", ["Kodiak Cakes"]),

    # ── Ruggable ──────────────────────────────────────────────────────────────
    "Jeneva Bell":       ("Ruggable founder; machine-washable rugs — category-creating home goods DTC brand", ["Ruggable"]),

    # ── Nutrafol ──────────────────────────────────────────────────────────────
    "Giorgos Tsetis":    ("Nutrafol co-founder; hair wellness brand — clinically-backed supplements, $1B+ valuation", ["Nutrafol"]),
    "Roland Peralta":    ("Nutrafol co-founder; built hair health category before anyone else was in it", ["Nutrafol"]),

    # ── Burrow ────────────────────────────────────────────────────────────────
    "Lee Mayer":         ("Burrow co-founder; modular furniture DTC — brought the Casper playbook to home furnishings", ["Burrow"]),
    "Stephen Kuhl":      ("Burrow co-founder; modular sofa brand with DTC-first distribution and strong brand identity", ["Burrow"]),

    # ── MeUndies ──────────────────────────────────────────────────────────────
    "Jonathan Shokrian":  ("MeUndies founder; underwear subscription — fabric-obsessed DTC brand with membership model", ["MeUndies"]),

    # ── Cuts Clothing ─────────────────────────────────────────────────────────
    "Steven Borrelli":   ("Cuts Clothing founder; premium men's T-shirts — the unofficial uniform of ambition", ["Cuts Clothing"]),

    # ── Bloom Nutrition ───────────────────────────────────────────────────────
    "Mari Llewellyn":    ("Bloom Nutrition founder; built greens supplement brand through personal transformation story", ["Bloom Nutrition"]),

    # ── SmartSweets ───────────────────────────────────────────────────────────
    "Tara Bosch":        ("SmartSweets founder; stepped away post-acquisition — candy with 3g sugar, $5K to exit story", ["SmartSweets"]),

    # ── Hello Products ────────────────────────────────────────────────────────
    "Craig Dubitsky":    ("Hello Products founder; natural oral care — left post-Colgate acquisition 2022, likely building", ["Hello Products"]),

    # ── Fabletics ─────────────────────────────────────────────────────────────
    "Adam Goldenberg":   ("Fabletics co-founder; TechStyle portfolio — serial DTC fashion brand operator at scale", ["Fabletics", "JustFab"]),

    # ── Reformation ───────────────────────────────────────────────────────────
    "Yael Aflalo":       ("Reformation founder; sustainable fashion brand with magnetic identity — sold to Permira", ["Reformation"]),

    # ── Brooklinen ────────────────────────────────────────────────────────────
    "Rich Fulop":        ("Brooklinen co-founder; luxury DTC bedding — disrupted the department store sheet category", ["Brooklinen"]),
    "Vicki Fulop":       ("Brooklinen co-founder; built direct-to-consumer home textiles brand with lifestyle identity", ["Brooklinen"]),

    # ── Quip ──────────────────────────────────────────────────────────────────
    "Simon Enever":      ("Quip co-founder; electric toothbrush subscription — design-driven oral care DTC brand", ["Quip"]),

    # ── Moon Juice ────────────────────────────────────────────────────────────
    "Amanda Chantal Bacon": ("Moon Juice founder; adaptogenic beauty/wellness brand with cult following — The Dusts", ["Moon Juice"]),

    # ── Manscaped ─────────────────────────────────────────────────────────────
    "Paul Tran":         ("Manscaped co-founder; men's grooming below-the-waist brand — category creation + bold brand voice", ["Manscaped"]),

    # ── Article ───────────────────────────────────────────────────────────────
    "Aamir Baig":        ("Article co-founder; DTC furniture — modern design at accessible prices, profitable without VC", ["Article"]),
    "Andy Prochazka":    ("Article co-founder; built one of the largest profitable DTC furniture brands in North America", ["Article"]),

    # ── Wild ──────────────────────────────────────────────────────────────────
    "Freddy Ward":       ("Wild co-founder; sustainable personal care with refillable deodorant — UK-origin DTC brand", ["Wild"]),
    "Charlie Bowes-Lyon": ("Wild co-founder; built refillable deodorant category with premium brand identity", ["Wild"]),

    # ── Liquid I.V. ───────────────────────────────────────────────────────────
    "Brandin Cohen":     ("Liquid I.V. founder; hydration multiplier sold to Unilever 2020 — electrolyte category creator", ["Liquid I.V."]),

    # ── Cirkul ────────────────────────────────────────────────────────────────
    "Garrett Waggoner":  ("Cirkul co-founder; flavor-infusion water bottle with subscription model — $1B+ valuation", ["Cirkul"]),

    # ── Magic Mind ────────────────────────────────────────────────────────────
    "William Hicks":     ("Magic Mind founder; world's first productivity shot — functional beverage category pioneer", ["Magic Mind"]),

    # ── Once Upon a Farm ──────────────────────────────────────────────────────
    "John Foraker":      ("Once Upon a Farm co-founder + CEO; organic baby food — ex-Annie's CEO, repeat CPG operator", ["Once Upon a Farm", "Annie's"]),

    # ── Chamberlain Coffee ────────────────────────────────────────────────────
    "Emma Chamberlain":  ("Chamberlain Coffee founder; Gen Z creator-to-brand — one of the cleanest creator-to-CPG plays", ["Chamberlain Coffee"]),

    # ── Add more here ──────────────────────────────────────────────────────
    # "Firstname Lastname": ("reason", ["Brand"]),
}


def check_conviction_match(text: str) -> dict | None:
    """
    Check if any conviction founder's name appears in the given text.

    Matches both "Firstname Lastname" and "Lastname, Firstname" forms.
    Both first and last name must be present (guards against common first names).
    Case-insensitive.

    Returns:
        {"name": str, "reason": str, "known_brands": list[str]} or None
    """
    if not text:
        return None

    text_lower = text.lower()

    for full_name, (reason, known_brands) in CONVICTION_FOUNDERS.items():
        parts = full_name.strip().split()
        if len(parts) < 2:
            continue
        first = parts[0].lower()
        last  = parts[-1].lower()

        # Both first and last must appear in the text
        if first in text_lower and last in text_lower:
            logger.info("Conviction match: '%s' found in signal text", full_name)
            return {
                "name":         full_name,
                "reason":       reason,
                "known_brands": known_brands,
            }

    return None


def check_conviction_match_multi(texts: list[str]) -> dict | None:
    """
    Check multiple text fields (e.g. owner, notes, description) for a match.
    Returns the first match found, or None.
    """
    for text in texts:
        match = check_conviction_match(text or "")
        if match:
            return match
    return None
