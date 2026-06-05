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
