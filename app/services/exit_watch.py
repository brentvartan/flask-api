"""
exit_watch.py
-------------
Tracks two things:

1. EXIT_ALUMNI — hand-curated early operators (#2–4) from notable consumer exits who
   are NOT already in the conviction founders list. Matched by name against signal text
   (same mechanism as conviction.py). Gives a +8 boost and an ALUMNI badge in the UI.

2. EXIT_WATCH_BRANDS — notable acquired/pivoted consumer brands whose alumni are
   worth flagging when found in future signals. Auto-populated from acquisition
   language detected in press/newswire signals; persisted in Redis.

HOW TO ADD AN ALUMNI:
  Add to EXIT_ALUMNI below. Format is identical to conviction.py:
    "Firstname Lastname": ("one-line reason", ["Known Brand 1", "Known Brand 2"]),
  Both first and last name must appear in signal text (prevents false matches).

HOW TO ADD A WATCHED BRAND:
  Add to EXIT_WATCH_BRANDS below. The dict is merged with any dynamically detected
  brands stored in Redis. Used by check_exit_alumni(past_companies) after a Proxycurl
  lookup returns a person's work history.
"""
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# ── Early operators / alumni to match by name ─────────────────────────────────
# People who were #2–4 at notable exits but aren't in the main conviction list.
# Match logic is identical to conviction.py — both first AND last name must appear.

EXIT_ALUMNI: dict[str, tuple[str, list[str]]] = {

    # ── Glossier ──────────────────────────────────────────────────────────────
    "Bryan Mahoney":    ("Glossier CTO; built the tech stack behind a $1B DTC brand", ["Glossier"]),

    # ── Peloton ───────────────────────────────────────────────────────────────
    "Robin Arzón":      ("Peloton VP Fitness Programming; the most recognizable face of the brand outside the CEO", ["Peloton"]),

    # ── Away ──────────────────────────────────────────────────────────────────
    "Selby Drummond":   ("Away Head of Brand (former Vogue editor); early brand architect at Away", ["Away"]),

    # ── Bala Weights ──────────────────────────────────────────────────────────
    "Natalie Holloway": ("Bala Weights co-founder; design-forward fitness accessories brand, Shark Tank success", ["Bala"]),
    "Max Kislevitz":    ("Bala Weights co-founder; premium fitness accessories — Shark Tank deal", ["Bala"]),

    # ── Media / community founders with DTC crossover ─────────────────────────
    "Jaclyn Johnson":   ("Create & Cultivate founder; female entrepreneurship media + events brand", ["Create & Cultivate"]),
    "Amanda Goetz":     ("House of Wise founder; ex-TheKnot.com VP Marketing turned wellness brand founder", ["House of Wise"]),

    # ── On Running ────────────────────────────────────────────────────────────
    "Martin Hoffmann":  ("On Running CEO; stepped down May 2026 — led On's global expansion to $11B valuation", ["On Running"]),
    "Marc Maurer":      ("On Running Co-CEO; departed 2025 — co-ran the brand during its IPO and breakout years", ["On Running"]),
    "David Allemann":   ("On Running co-founder + CMO; brand and creative force behind On's identity", ["On Running"]),
    "Caspar Coppetti":  ("On Running co-founder; led partnerships and go-to-market since founding", ["On Running"]),

    # ── Glossier ──────────────────────────────────────────────────────────────
    "Kyle Leahy":       ("Glossier CEO post-Emily Weiss; ran the brand through its post-2022 reset", ["Glossier"]),
    "Nikki Neuburger":  ("Glossier Chief Brand Officer until 2023; helped rebuild post-layoff brand reputation", ["Glossier"]),
    "Julie Bowerman":   ("Glossier CMO until 2024; ran marketing for one of the most talked-about beauty brands", ["Glossier"]),
    "Colin Walsh":      ("Glossier CEO (current); rebuilding the brand — worth tracking as alumni disperse", ["Glossier"]),

    # ── Poppi ─────────────────────────────────────────────────────────────────
    "Chris Hall":       ("Poppi CEO during PepsiCo acquisition; ran the commercial side of the $1.95B exit", ["Poppi"]),
    "Rohan Oza":        ("Poppi board + investor; ex-Coca-Cola (managed Vitaminwater acquisition) — serial brand exits", ["Poppi", "Vitaminwater"]),

    # ── Gymshark ──────────────────────────────────────────────────────────────
    "Steve Hewitt":     ("Gymshark CEO; scaled Gymshark from Ben Francis's bedroom brand to a $1.3B+ brand", ["Gymshark"]),
    "Mat Dunn":         ("Gymshark President; operational leader during the brand's international scale-up", ["Gymshark"]),
    "Noel Mack":        ("Gymshark Chief Brand Officer; built one of the most imitated fitness brand playbooks", ["Gymshark"]),
    "Mark Gillett":     ("Gymshark Chief Brand Officer until 2024; brand architect for Gymshark's athlete partnerships", ["Gymshark"]),

    # ── Savage X Fenty ────────────────────────────────────────────────────────
    "Hillary Super":    ("Savage X Fenty CEO; built the brand's commercial machine post-launch", ["Savage X Fenty"]),
    "Christiane Pendarvis": ("Savage X Fenty President + CCO; brand and creative lead for the Rihanna lingerie brand", ["Savage X Fenty"]),

    # ── HeyDude ───────────────────────────────────────────────────────────────
    "Terence Reilly":   ("HeyDude CEO; ex-Crocs CMO who brought the same cultural moment playbook to Hey Dude", ["HeyDude", "Crocs"]),
    "David Butler":     ("HeyDude President until 2024; ran operations during the Crocs acquisition integration", ["HeyDude"]),

    # ── Rare Beauty ───────────────────────────────────────────────────────────
    "Scott Friedman":   ("Rare Beauty CEO; built the brand infrastructure and retail partnerships behind Selena's brand", ["Rare Beauty"]),
    "Katie Welch":      ("Rare Beauty Chief Marketing Officer; architected the community-first brand playbook", ["Rare Beauty"]),

    # ── Bombas ────────────────────────────────────────────────────────────────
    "Jason LaRose":     ("Bombas CEO (incoming); scaling the next chapter post-Shark Tank founders", ["Bombas"]),

    # ── DECIEM / The Ordinary ─────────────────────────────────────────────────
    "Nicola Kilner":    ("DECIEM CEO; built The Ordinary into a cult brand and navigated the turbulent founder era", ["DECIEM", "The Ordinary"]),

    # ── Therabody ─────────────────────────────────────────────────────────────
    "Monty Sharma":     ("Therabody CEO; scaled the brand post-Jason Wersland; led the Theragun expansion era", ["Therabody"]),

    # ── Honest Company ────────────────────────────────────────────────────────
    "Nick Vlahos":      ("Honest Company CEO until 2023; scaled the brand post-Jessica Alba; now at Rhode", ["Honest Company", "Rhode"]),

    # ── Liquid Death ──────────────────────────────────────────────────────────
    "John Shea":        ("Liquid Death President until 2024; scaled distribution and retail for the water brand", ["Liquid Death"]),

    # ── Athleta ───────────────────────────────────────────────────────────────
    "Nancy Green":      ("Athleta CEO; built Athleta into Gap's most culturally relevant brand — women's activewear", ["Athleta"]),
    "Sally Gilligan":   ("Athleta CEO until 2023; led during the brand's peak relevance period", ["Athleta"]),

    # ── Daily Harvest ─────────────────────────────────────────────────────────
    "Ricky Silver":     ("Daily Harvest CEO (post-founder); ran operations after Rachel Drori stepped back", ["Daily Harvest"]),

    # ── Dollar Shave Club ─────────────────────────────────────────────────────
    "Jason Goldberger": ("Dollar Shave Club CEO until 2024; led Unilever-owned brand's DTC-to-retail evolution", ["Dollar Shave Club"]),

    # ── SmartSweets ───────────────────────────────────────────────────────────
    "Heidi Dorosin":    ("SmartSweets CEO post-acquisition; ran the brand after Tara Bosch stepped back", ["SmartSweets"]),

    # ── Primal Kitchen ────────────────────────────────────────────────────────
    "Morgan Zanotti":   ("Primal Kitchen Co-Founder + President; left ~5 years post-Kraft Heinz acquisition", ["Primal Kitchen"]),

    # ── Catalina Crunch ───────────────────────────────────────────────────────
    "Krishna Kaliannan": ("Catalina Crunch founder + CEO until 2024; built the keto cereal brand from scratch", ["Catalina Crunch"]),

    # ── Super Coffee ──────────────────────────────────────────────────────────
    "Tyler Ricks":      ("Super Coffee CEO until 2024; led commercial operations for the DeCicco family brand", ["Super Coffee"]),

    # ── Moon Juice ────────────────────────────────────────────────────────────
    "Nina Fuhrman":     ("Moon Juice CEO until 2025; scaled the adaptogenic supplement brand commercially", ["Moon Juice"]),

    # ── NotCo ─────────────────────────────────────────────────────────────────
    "Fernando Machado": ("NotCo CMO; ex-Burger King CMO who brought challenger-brand marketing to alt-protein", ["NotCo", "Burger King"]),

    # ── Add more here ──────────────────────────────────────────────────────────
    # "Firstname Lastname": ("reason", ["Brand"]),
}

# ── Brands whose alumni are worth tracking ────────────────────────────────────
# When we have Proxycurl data for a person (past_companies list), we check
# if they worked at any of these brands. Auto-detects from press signals too.

EXIT_WATCH_BRANDS: dict[str, dict] = {
    "Mirror":            {"acquirer": "lululemon",         "year": 2020, "notes": "$500M acquisition"},
    "Casper":            {"acquirer": "Durational Capital", "year": 2022, "notes": "went private at discount"},
    "Away":              {"acquirer": None,                "year": None, "notes": "leadership changes; alumni dispersed"},
    "Outdoor Voices":    {"acquirer": None,                "year": None, "notes": "struggled; founder out 2020"},
    "Glossier":          {"acquirer": None,                "year": None, "notes": "major layoffs 2022; many alumni now free"},
    "Daily Harvest":     {"acquirer": None,                "year": None, "notes": "leadership changes post-recall"},
    "RxBar":             {"acquirer": "Kellogg's",         "year": 2017, "notes": "$600M acquisition"},
    "Plated":            {"acquirer": "Albertsons",        "year": 2017, "notes": "~$200M acquisition"},
    "Birchbox":          {"acquirer": "FemBo Holdings",    "year": 2021, "notes": "sold out of bankruptcy"},
    "Bonobos":           {"acquirer": "Walmart",           "year": 2017, "notes": "$310M acquisition"},
    "Dollar Shave Club": {"acquirer": "Unilever",          "year": 2016, "notes": "$1B acquisition"},
    "Honest Tea":        {"acquirer": "Coca-Cola",         "year": 2011, "notes": "full acquisition 2011"},
    "Hu Kitchen":        {"acquirer": "Mondelez",          "year": 2021, "notes": "premium chocolate acquisition"},
    "Brandless":         {"acquirer": None,                "year": None, "notes": "shut down 2019; alumni watching"},
    "Foxtrot":           {"acquirer": None,                "year": None, "notes": "shut down April 2024; many alumni now free"},
    "Peloton":           {"acquirer": None,                "year": None, "notes": "public; major leadership changes 2022–23"},
    "Sweetgreen":        {"acquirer": None,                "year": None, "notes": "public; founding team watching"},
    "Thrive Market":     {"acquirer": None,                "year": None, "notes": "leadership watching"},
    "KRAVE Jerky":       {"acquirer": "Hershey",           "year": 2015, "notes": "better-snacking acquisition"},
    "Adore Me":          {"acquirer": "Walmart",           "year": 2022, "notes": "~$400M acquisition"},
    "Yes To":            {"acquirer": None,                "year": None, "notes": "various PE owners; team diaspora"},
    "Nasty Gal":         {"acquirer": "Boohoo",            "year": 2017, "notes": "sold out of bankruptcy; brand lives on"},
    "Greats":            {"acquirer": "Steve Madden",      "year": 2019, "notes": "sneakers DTC acquisition"},
    "Bala":              {"acquirer": None,                "year": None, "notes": "Shark Tank; strong brand, watching alumni"},
    "Ollie":             {"acquirer": None,                "year": None, "notes": "premium pet food; series B+ watching"},
    "Good Culture":      {"acquirer": None,                "year": None, "notes": "modern protein brand; alumni watching"},

    # ── From Recent Consumer Deals (Top 97) ───────────────────────────────────
    "On Running":        {"acquirer": "Public Markets",    "year": 2021, "notes": "IPO $11B; co-founders still active; CEO Hoffmann departed May 2026"},
    "Alo Yoga":          {"acquirer": None,                "year": None, "notes": "private, ~$10B val; no exit yet but alumni worth watching"},
    "Celsius":           {"acquirer": "Public Markets",    "year": None, "notes": "public; major Pepsi investment 2023; leadership watching"},
    "Chobani":           {"acquirer": None,                "year": None, "notes": "private ~$10B; IPO attempts; Hamdi Ulukaya is the founder"},
    "Chewy":             {"acquirer": "Public Markets",    "year": 2019, "notes": "IPO $8.7B; Ryan Cohen founded, now activist investor"},
    "Peloton":           {"acquirer": "Public Markets",    "year": None, "notes": "public; major leadership changes 2022-25; many alumni free"},
    "e.l.f. Beauty":     {"acquirer": "Public Markets",    "year": None, "notes": "public; strong brand — executive alumni worth tracking"},
    "BodyArmor":         {"acquirer": "Coca-Cola",         "year": 2021, "notes": "$5.6B acquisition; Mike Repole remains chairman"},
    "Vuori":             {"acquirer": None,                "year": None, "notes": "private ~$4B; no exit; Joe Kudla founder still active"},
    "Oura":              {"acquirer": None,                "year": None, "notes": "private ~$5B; wearable ring; team watching"},
    "Kind Snacks":       {"acquirer": "Mars",              "year": 2020, "notes": "Mars acquired majority stake; Daniel Lubetzky exited"},
    "Fabletics":         {"acquirer": None,                "year": None, "notes": "TechStyle / Adam Goldenberg; exploring IPO — alumni watching"},
    "Figs":              {"acquirer": "Public Markets",    "year": 2021, "notes": "IPO; Trina Spear + Heather Hasson co-founders still active"},
    "Allbirds":          {"acquirer": "Public Markets",    "year": 2021, "notes": "IPO; Joey Zwillinger + Tim Brown co-founders; major layoffs"},
    "Skims":             {"acquirer": None,                "year": None, "notes": "private ~$4B; Kim Kardashian + Jens Grede; no exit yet"},
    "Whoop":             {"acquirer": None,                "year": None, "notes": "private ~$3.6B; Will Ahmed founder; John Capodilupo CTO departed 2022"},
    "Beats":             {"acquirer": "Apple",             "year": 2014, "notes": "$3B Apple acquisition; Luke Wood president departed 2024"},
    "Therabody":         {"acquirer": None,                "year": None, "notes": "private; Theragun + Therabody brand; leadership changes 2025"},
    "Savage X Fenty":    {"acquirer": None,                "year": None, "notes": "private; Rihanna brand; Hillary Super CEO; alumni watching"},
    "HeyDude":           {"acquirer": "Crocs",             "year": 2022, "notes": "$2.5B Crocs acquisition; Terence Reilly CEO (ex-Crocs CMO)"},
    "Rare Beauty":       {"acquirer": None,                "year": None, "notes": "private ~$2B; Selena Gomez brand; Scott Friedman CEO"},
    "Bombas":            {"acquirer": None,                "year": None, "notes": "private ~$1B; David Heath + Randy Goldberg; Shark Tank exit watching"},
    "Poppi":             {"acquirer": "PepsiCo",           "year": 2025, "notes": "$1.95B PepsiCo acquisition; Allison Ellsworth expected to build again"},
    "Olipop":            {"acquirer": None,                "year": None, "notes": "private ~$1.85B; Ben Goodwin + David Lester; still growing"},
    "Glossier":          {"acquirer": None,                "year": None, "notes": "private; post-layoff rebuild; Emily Weiss stepped back; many alumni free"},
    "Bloom Nutrition":   {"acquirer": None,                "year": None, "notes": "private; Mari Llewellyn founder; influencer-to-brand playbook"},
    "Alani Nu":          {"acquirer": "Celsius",           "year": 2025, "notes": "$1.8B Celsius acquisition; Katy Hearn + Haydn Schneider founders"},
    "DECIEM":            {"acquirer": "Estee Lauder",      "year": 2021, "notes": "full acquisition; The Ordinary parent; Nicola Kilner runs it"},
    "BarkBox":           {"acquirer": "Public Markets",    "year": 2021, "notes": "SPAC/IPO; Matt Meeker founder; Zahir Ibrahim COO"},
    "NotCo":             {"acquirer": None,                "year": None, "notes": "private; Matias Muchnick founder; alt-protein with AI"},
    "Ruggable":          {"acquirer": None,                "year": None, "notes": "private ~$1.5B; Jeneva Bell founder; DTC rugs category"},
    "Magic Spoon":       {"acquirer": None,                "year": None, "notes": "private; Greg Sewitz + Gabi Lewis founders; Series B watching"},
    "Feastables":        {"acquirer": None,                "year": None, "notes": "private; MrBeast (Jimmy Donaldson) brand; celebrity CPG"},
    "Athleta":           {"acquirer": None,                "year": None, "notes": "Gap subsidiary; Nancy Green + Sally Gilligan CEOs departed"},
    "Honest Company":    {"acquirer": "Public Markets",    "year": 2021, "notes": "IPO; Jessica Alba brand; Nick Vlahos CEO departed 2023"},
    "Harry's":           {"acquirer": None,                "year": None, "notes": "private; FTC blocked Edgewell deal; Jeff Raider + Andy Katz-Mayfield"},
    "Liquid Death":      {"acquirer": None,                "year": None, "notes": "private ~$1.4B; Mike Cessario founder; John Shea president departed 2024"},
    "The Farmer's Dog":  {"acquirer": None,                "year": None, "notes": "private ~$1.8B; Jonathan Regev + Brett Podolsky founders"},
    "Gymshark":          {"acquirer": None,                "year": None, "notes": "private ~$1.3B; Ben Francis founder; Steve Hewitt CEO"},
    "Chomps":            {"acquirer": "Keurig Dr Pepper",  "year": 2024, "notes": "acquired; Pete Maldonado + Rashid Ali founders"},
    "Athletic Brewing":  {"acquirer": "Keurig Dr Pepper",  "year": 2024, "notes": "majority stake; Bill Shufelt + John Walker founders; NA beer category"},
    "Daily Harvest":     {"acquirer": None,                "year": None, "notes": "private; post-recall reset; Rachel Drori founder; leadership changes"},
    "Dollar Shave Club": {"acquirer": "Unilever",          "year": 2016, "notes": "$1B Unilever acquisition; Michael Dubin founder; Jason Goldberger CEO 2024"},
    "Quest Nutrition":   {"acquirer": "Simply Good Foods", "year": 2019, "notes": "$1B acquisition; Tom Bilyeu + Ron Penna founders"},
    "Rhode":             {"acquirer": None,                "year": None, "notes": "private; Hailey Bieber brand; celebrity skincare category"},
    "AG1":               {"acquirer": None,                "year": None, "notes": "private ~$1.2B; Kat Cole CEO; Chris Ashenden founder"},
    "Cirkul":            {"acquirer": None,                "year": None, "notes": "private ~$1B; Garrett Waggoner + Andy Gay founders; flavor subscription"},
    "Rothy's":           {"acquirer": None,                "year": None, "notes": "private; Roth Martin + Stephen Hawthornthwaite founders; sustainable footwear"},
    "Manscaped":         {"acquirer": "Nasdaq via SPAC",   "year": 2023, "notes": "SPAC; Paul Tran founder; men's grooming below-the-belt category"},
    "Eight Sleep":       {"acquirer": None,                "year": None, "notes": "private ~$500M; Matteo Franceschetti + Alexandra Zatarain founders"},
    "Article":           {"acquirer": None,                "year": None, "notes": "private; Aamir Baig + Andy Prochazka founders; profitable DTC furniture"},
    "Nom Nom":           {"acquirer": "Mars Petcare",      "year": 2022, "notes": "Mars Petcare acquisition; Brett Podolsky + Jonathan Regev founders"},
    "True Classic":      {"acquirer": None,                "year": None, "notes": "private; Ryan Bartlett + Matt Winnick founders; men's basics DTC"},
    "HexClad":           {"acquirer": None,                "year": None, "notes": "private ~$500M; Danny Winer + Cole Rubin founders; Gordon Ramsay partner"},
    "Ritual":            {"acquirer": None,                "year": None, "notes": "private; Katerina Schneider founder; transparent vitamin brand"},
    "Nutrafol":          {"acquirer": "Unilever",          "year": 2022, "notes": "$1B+ Unilever acquisition; Giorgos Tsetis + Roland Peralta founders"},
    "Kodiak Cakes":      {"acquirer": "L Catterton",       "year": 2021, "notes": "PE majority stake; Joel Clark founder; protein pancake category"},
    "Reformation":       {"acquirer": "Permira",           "year": 2020, "notes": "PE acquisition; Yael Aflalo founder; Hali Borenstein CEO"},
    "Brooklinen":        {"acquirer": None,                "year": None, "notes": "private; Rich + Vicki Fulop founders; DTC luxury bedding"},
    "Quip":              {"acquirer": None,                "year": None, "notes": "private; Simon Enever founder; DTC electric toothbrush + subscription"},
    "Lovevery":          {"acquirer": None,                "year": None, "notes": "private ~$800M; Jessica Rolph + Roderick Morris founders"},
    "Simple Mills":      {"acquirer": "Flowers Foods",     "year": 2025, "notes": "Flowers Foods acquisition; Katlin Smith founder; clean-label snacks"},
    "Huel":              {"acquirer": None,                "year": None, "notes": "private; Julian Hearn founder; meal replacement nutrition brand (UK)"},
    "Banza":             {"acquirer": "Sovos Brands",      "year": 2020, "notes": "Sovos Brands acquisition; Brian + Scott Rudolph founders; chickpea pasta"},
    "Liquid I.V.":       {"acquirer": "Unilever",          "year": 2020, "notes": "Unilever acquisition; Brandin Cohen founder; hydration multiplier"},
    "Beis":              {"acquirer": None,                "year": None, "notes": "private; Shay Mitchell founder; celebrity travel accessories brand"},
    "Once Upon a Farm":  {"acquirer": None,                "year": None, "notes": "private; John Foraker + Jennifer Garner; organic baby food"},
    "Our Place":         {"acquirer": None,                "year": None, "notes": "private; Shiza Shahid + Amir Tehrani founders; Always Pan"},
    "Catalina Crunch":   {"acquirer": None,                "year": None, "notes": "private; Krishna Kaliannan founder stepped down 2024; keto cereal"},
    "David Protein":     {"acquirer": None,                "year": None, "notes": "private; Peter Rahal founder (RxBar pedigree); premium protein bar"},
    "MeUndies":          {"acquirer": None,                "year": None, "notes": "private; Jonathan Shokrian founder; underwear subscription"},
    "Parachute":         {"acquirer": None,                "year": None, "notes": "private; Ariel Kaye founder; DTC luxury home textiles"},
    "Cuts Clothing":     {"acquirer": None,                "year": None, "notes": "private; Steven Borrelli founder; premium men's basics DTC"},
    "Seed":              {"acquirer": None,                "year": None, "notes": "private; Ara Katz + Raja Dhir founders; probiotic wellness brand"},
    "Super Coffee":      {"acquirer": None,                "year": None, "notes": "private; DeCicco family founders; Jordan + Jake + Jim DeCicco"},
    "SmartSweets":       {"acquirer": "TPG Growth",        "year": 2021, "notes": "PE acquisition; Tara Bosch founder stepped away; low-sugar candy"},
    "Caraway":           {"acquirer": None,                "year": None, "notes": "private; Jordan Nathan founder; aesthetic DTC cookware brand"},
    "Hello Products":    {"acquirer": "Colgate-Palmolive", "year": 2020, "notes": "Colgate acquisition; Craig Dubitsky founder left 2022"},
    "Chamberlain Coffee": {"acquirer": None,               "year": None, "notes": "private; Emma Chamberlain founder; creator-to-CPG coffee brand"},
    "Wild":              {"acquirer": None,                "year": None, "notes": "private (UK); Freddy Ward + Charlie Bowes-Lyon founders; refillable deodorant"},
    "Graza":             {"acquirer": None,                "year": None, "notes": "private; Andrew Benin founder; squeeze-bottle olive oil brand"},
    "Outdoor Voices":    {"acquirer": None,                "year": None, "notes": "private; Ty Haney founder out 2020; Cliff Moskowitz CEO departed 2025"},
    "Magic Mind":        {"acquirer": None,                "year": None, "notes": "private; William Hicks founder; productivity shot, functional bev"},
    "Perfect Bar":       {"acquirer": "Nestle",            "year": 2019, "notes": "Nestle acquisition; Bill + Leigh Keith founders; refrigerated protein bar"},
    "Fly By Jing":       {"acquirer": None,                "year": None, "notes": "private; Jing Gao founder; authentic Sichuan chili crisp brand"},
    "Burrow":            {"acquirer": None,                "year": None, "notes": "private; Lee Mayer + Stephen Kuhl founders; modular furniture DTC"},
    "Marine Layer":      {"acquirer": "Freestyle Solutions", "year": 2020, "notes": "PE acquisition; Mike Natenshon CEO; soft basics California brand"},
    "Moon Juice":        {"acquirer": None,                "year": None, "notes": "private; Amanda Chantal Bacon founder; Nina Fuhrman CEO departed 2025"},
    "Lalo":              {"acquirer": None,                "year": None, "notes": "private; Greg Davidson + Michael Wieder founders; modern baby brand"},
    "Mid-Day Squares":   {"acquirer": None,                "year": None, "notes": "private (Canada); Nick Saltarelli + Jake + Lezlie Karls founders"},
    "Stasher":           {"acquirer": "SC Johnson",        "year": 2023, "notes": "SC Johnson acquisition; Kat Nouri founder; silicone reusable bags"},
    "Care/of":           {"acquirer": "Bayer",             "year": 2019, "notes": "Bayer acquisition; shut down 2024; Craig Elbert + Akash Shah founders"},
    "Primal Kitchen":    {"acquirer": "Kraft Heinz",       "year": 2019, "notes": "$200M Kraft Heinz acquisition; Mark Sisson founder; Morgan Zanotti left"},
    "Cocofloss":         {"acquirer": None,                "year": None, "notes": "private; Catherine + Chrystle Cu founders; premium dental floss"},
    "Native":            {"acquirer": "Procter & Gamble",  "year": 2017, "notes": "$100M P&G acquisition; Moiz Ali founder; natural deodorant"},
}

# Redis key for dynamically detected exit brands
_REDIS_KEY = "exit_watch:brands"


# ── Redis helpers ─────────────────────────────────────────────────────────────

def _get_redis():
    """Return a Redis client or None if REDIS_URL is not configured."""
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        import redis
        return redis.Redis.from_url(url, decode_responses=True, socket_timeout=2)
    except Exception as exc:
        logger.debug("exit_watch: Redis unavailable: %s", exc)
        return None


def get_exit_brands() -> dict[str, dict]:
    """
    Return the combined exit watch brand list (static dict + Redis-stored dynamic entries).
    """
    result = dict(EXIT_WATCH_BRANDS)
    try:
        r = _get_redis()
        if r:
            raw = r.get(_REDIS_KEY)
            if raw:
                result.update(json.loads(raw))
    except Exception as exc:
        logger.debug("exit_watch: could not load dynamic brands from Redis: %s", exc)
    return result


def add_exit_brand(
    brand_name: str,
    acquirer: str = None,
    year: int = None,
    notes: str = "",
) -> None:
    """
    Persist a dynamically detected exit brand to Redis.
    Called when acquisition language is found in a press or newswire signal.
    """
    if not brand_name or not brand_name.strip():
        return
    normalized = brand_name.strip().title()
    try:
        r = _get_redis()
        if not r:
            return
        existing_raw = r.get(_REDIS_KEY)
        existing = json.loads(existing_raw) if existing_raw else {}
        existing[normalized] = {
            "acquirer": acquirer,
            "year": year,
            "notes": notes,
            "auto_detected": True,
        }
        r.set(_REDIS_KEY, json.dumps(existing))
        logger.info("exit_watch: added '%s' to dynamic brand list", normalized)
    except Exception as exc:
        logger.warning("exit_watch: could not persist brand '%s': %s", brand_name, exc)


# ── Name-based matching (like conviction.py) ──────────────────────────────────

def check_exit_alumni_match(text: str) -> dict | None:
    """
    Check if any exit alumni name appears in the given text.
    Returns {"name": str, "reason": str, "known_brands": list[str]} or None.
    """
    if not text:
        return None
    text_lower = text.lower()
    for full_name, (reason, known_brands) in EXIT_ALUMNI.items():
        parts = full_name.strip().split()
        if len(parts) < 2:
            continue
        first = parts[0].lower()
        last  = parts[-1].lower()
        if first in text_lower and last in text_lower:
            logger.info("Exit alumni match: '%s' found in signal text", full_name)
            return {"name": full_name, "reason": reason, "known_brands": known_brands}
    return None


def check_exit_alumni_match_multi(texts: list[str]) -> dict | None:
    """Check multiple text fields; return first alumni match found."""
    for text in texts:
        match = check_exit_alumni_match(text or "")
        if match:
            return match
    return None


# ── Post-Proxycurl company history check ─────────────────────────────────────

def check_exit_alumni(past_companies: list[str]) -> dict | None:
    """
    After a Proxycurl lookup, check if any company in the person's work history
    matches the exit watch brand list.

    Args:
        past_companies: list of company names from LinkedIn work history

    Returns:
        {"brand": str, "acquirer": str|None, "notes": str} or None
    """
    if not past_companies:
        return None
    brands = get_exit_brands()
    for company in past_companies:
        company_lower = company.lower().strip()
        for brand, info in brands.items():
            brand_lower = brand.lower()
            if brand_lower in company_lower or company_lower in brand_lower:
                return {
                    "brand":    brand,
                    "acquirer": info.get("acquirer"),
                    "notes":    info.get("notes", ""),
                }
    return None


# ── Acquisition detection from press text ─────────────────────────────────────

# Patterns that suggest an acquisition announcement
_ACQ_PATTERNS = [
    r'([A-Z][A-Za-z\s]{2,25}?)\s+(?:has been|was|is)\s+acquired\s+by\b',
    r'\bacquires?\s+([A-Z][A-Za-z\s]{2,25}?)(?:\s+for|\s+in|\.|,)',
    r'\bacquisition\s+of\s+([A-Z][A-Za-z\s]{2,25?})(?:\s+for|\s+by|\.|,)',
    r'([A-Z][A-Za-z\s]{2,25}?)\s+sold\s+to\s+[A-Z][A-Za-z]',
]
_ACQ_RE = [re.compile(p) for p in _ACQ_PATTERNS]

# Words that indicate it's not a consumer brand acquisition
_ACQ_EXCLUDE = re.compile(
    r'\b(stake|shares|real estate|property|land|patent|technology platform|'
    r'software|SaaS|enterprise|B2B|infrastructure|facility|warehouse)\b',
    re.IGNORECASE,
)


def detect_acquisition_in_text(text: str) -> str | None:
    """
    Scan text for consumer brand acquisition language.
    Returns the acquired brand name if found, else None.
    """
    if not text:
        return None
    if _ACQ_EXCLUDE.search(text):
        return None
    for pattern in _ACQ_RE:
        match = pattern.search(text)
        if match:
            brand = match.group(1).strip().rstrip(".,")
            if 3 <= len(brand) <= 35:
                return brand
    return None
