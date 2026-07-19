#!/usr/bin/env python3
"""
founder_linkedin_pull.py
------------------------
One-time NinjaPear profile pull for all founders, alumni, and executives
in the Bullish Stealth Finder conviction list and 97-brand Consumer Deals dataset.

Outputs:
  ~/Desktop/founder_pull_results.json   — raw profiles (all found)
  ~/Desktop/founder_pull_hot_leads.json — people with NEW employers
  ~/Desktop/founder_dna_brief.txt       — pattern synthesis for enrichment prompt

Cost: ~$0.04/person (3 credits @ NinjaPear)
Run: python3 scripts/founder_linkedin_pull.py

Resumable: saves progress after every 10 calls. Re-running skips already-fetched names.
"""

import json
import os
import sys
import time
import pathlib

# ── Load .env ─────────────────────────────────────────────────────────────────
_env_path = pathlib.Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import requests

KEY = os.environ.get("PROXYCURL_API_KEY", "")
if not KEY:
    sys.exit("ERROR: PROXYCURL_API_KEY not set — check .env or export it.")

BASE         = "https://nubela.co"
TIMEOUT      = 45    # NinjaPear avg: 10.5s fast / 38.7s detailed
PAUSE        = 2.5   # seconds between calls
SAVE_EVERY   = 10    # checkpoint after this many calls

OUT_RAW      = pathlib.Path.home() / "Desktop" / "founder_pull_results.json"
OUT_HOT      = pathlib.Path.home() / "Desktop" / "founder_pull_hot_leads.json"
OUT_BRIEF    = pathlib.Path.home() / "Desktop" / "founder_dna_brief.txt"


# ── Brand → website URL mapping (NinjaPear requires URLs for best results) ────
# Company names often work for large brands, but URLs are more reliable.
BRAND_URLS = {
    "On Running":          "https://www.on.com",
    "Poppi":               "https://drinkpoppi.com",
    "SmartSweets":         "https://smartsweets.com",
    "Hello Products":      "https://helloproducts.com",
    "Primal Kitchen":      "https://primalkitchen.com",
    "Glossier":            "https://www.glossier.com",
    "Catalina Crunch":     "https://catalinacrunch.com",
    "Moon Juice":          "https://moonjuice.com",
    "Super Coffee":        "https://drinksupercoffee.com",
    "Liquid Death":        "https://liquiddeath.com",
    "Dollar Shave Club":   "https://www.dollarshaveclub.com",
    "Athleta":             "https://www.athleta.gap.com",
    "Honest Company":      "https://www.honest.com",
    "Chamberlain Coffee":  "https://chamberlaincoffee.com",
    "Gymshark":            "https://www.gymshark.com",
    "HeyDude":             "https://www.heydude.com",
    "Beats":               "https://www.beatsbydre.com",
    "Daily Harvest":       "https://www.daily-harvest.com",
    "Outdoor Voices":      "https://www.outdoorvoices.com",
    "DECIEM":              "https://deciem.com",
    "BarkBox":             "https://www.barkbox.com",
    "David Protein":       "https://davidprotein.com",
    "Parachute":           "https://www.parachutehome.com",
    "Whoop":               "https://www.whoop.com",
    "Alo Yoga":            "https://www.aloyoga.com",
    "Celsius":             "https://www.celsius.com",
    "Chobani":             "https://www.chobani.com",
    "Chewy":               "https://www.chewy.com",
    "Peloton":             "https://www.onepeloton.com",
    "e.l.f. Beauty":       "https://www.elfcosmetics.com",
    "Warby Parker":        "https://www.warbyparker.com",
    "BodyArmor":           "https://www.drinkbodyarmor.com",
    "Vuori":               "https://vuoriclothing.com",
    "Oura":                "https://ouraring.com",
    "Kind Snacks":         "https://www.kindsnacks.com",
    "Fabletics":           "https://www.fabletics.com",
    "Figs":                "https://www.wearfigs.com",
    "Allbirds":            "https://www.allbirds.com",
    "Skims":               "https://skims.com",
    "Therabody":           "https://www.therabody.com",
    "Savage X Fenty":      "https://www.savagex.com",
    "Rare Beauty":         "https://www.rarebeauty.com",
    "Bombas":              "https://bombas.com",
    "NotCo":               "https://notco.com",
    "Ruggable":            "https://ruggable.com",
    "Magic Spoon":         "https://magicspoon.com",
    "Harry's":             "https://harrys.com",
    "The Farmer's Dog":    "https://www.thefarmersdog.com",
    "Chomps":              "https://www.chomps.com",
    "Athletic Brewing":    "https://athleticbrewing.com",
    "Quest Nutrition":     "https://www.questnutrition.com",
    "AG1":                 "https://drinkag1.com",
    "Cirkul":              "https://www.cirkul.com",
    "Rothy's":             "https://rothys.com",
    "Eight Sleep":         "https://www.eightsleep.com",
    "Article":             "https://www.article.com",
    "Nom Nom":             "https://nomnomnow.com",
    "True Classic":        "https://trueclassic.com",
    "HexClad":             "https://hexclad.com",
    "Ritual":              "https://ritual.com",
    "Nutrafol":            "https://nutrafol.com",
    "Kodiak Cakes":        "https://kodiakcakes.com",
    "Reformation":         "https://www.thereformation.com",
    "Brooklinen":          "https://www.brooklinen.com",
    "Quip":                "https://www.getquip.com",
    "Lovevery":            "https://lovevery.com",
    "Simple Mills":        "https://www.simplemills.com",
    "Huel":                "https://huel.com",
    "Banza":               "https://eatbanza.com",
    "Liquid I.V.":         "https://www.liquid-iv.com",
    "Once Upon a Farm":    "https://onceuponafarmorganics.com",
    "Our Place":           "https://fromourplace.com",
    "MeUndies":            "https://www.meundies.com",
    "Cuts Clothing":       "https://cutsclothing.com",
    "Seed":                "https://seed.com",
    "Caraway":             "https://www.carawayhome.com",
    "Wild":                "https://wearewild.com",
    "Graza":               "https://graza.co",
    "Magic Mind":          "https://magicmind.com",
    "Perfect Bar":         "https://www.perfectbar.com",
    "Burrow":              "https://burrow.com",
    "Lalo":                "https://www.babybrandlalo.com",
    "Mid-Day Squares":     "https://mds.ca",
    "Cocofloss":           "https://cocofloss.com",
    "Native":              "https://nativecos.com",
    "Marine Layer":        "https://www.marinelayer.com",
    "Manscaped":           "https://www.manscaped.com",
    "Mirror":              "https://www.mirror.co",
    "RxBar":               "https://www.rxbar.com",
    "CAULIPOWER":          "https://eatcaulipower.com",
    "Hint Water":          "https://www.drinkhint.com",
    "TOMS":                "https://www.toms.com",
    "Bonobos":             "https://bonobos.com",
    "Honest Tea":          "https://www.honesttea.com",
    "JUST":                "https://www.ju.st",
    "Hungryroot":          "https://www.hungryroot.com",
    "Lemon Perfect":       "https://lemonperfect.com",
    "care/of":             "https://takecareof.com",
    "Fly by Jing":         "https://flybyjing.com",
    "Alani Nu":            "https://alaninu.com",
    "Olipop":              "https://drinkolipop.com",
    "Hims":                "https://www.forhims.com",
    "Rent the Runway":     "https://www.renttherunway.com",
    "Stitch Fix":          "https://www.stitchfix.com",
    "Thrive Market":       "https://thrivemarket.com",
    "Charlotte Tilbury":   "https://www.charlottetilbury.com",
    "Wild One":            "https://wildone.com",
    "KRAVE Jerky":         "https://kravejerky.com",
    "Casper":              "https://casper.com",
    "ILIA Beauty":         "https://www.iliabeauty.com",
    "Good Culture":        "https://www.goodculture.com",
    "Ollie":               "https://myollie.com",
    "JOLIE":               "https://jolieskinco.com",
    "Nasty Gal":           "https://www.nastygal.com",
    "Birchbox":            "https://www.birchbox.com",
    "Adore Me":            "https://www.adoreme.com",
    "Plated":              "https://www.plated.com",
    "Sweetgreen":          "https://www.sweetgreen.com",
    "Stasher":             "https://stasherbag.com",
    "Bloom Nutrition":     "https://bloomnu.com",
    "Away":                "https://www.awaytravel.com",
    "Bala":                "https://ba.la",
    "Create & Cultivate":  "https://createcultivate.com",
    "House of Wise":       "https://houseofwise.com",
    "Filament":            "https://filamentathletic.com",
}


# ══════════════════════════════════════════════════════════════════════════════
# PEOPLE TO LOOK UP
# ══════════════════════════════════════════════════════════════════════════════

PEOPLE = []

RECENT_DEPARTURES = [
    ("Martin Hoffmann",    "On Running",           "DEPARTURE"),
    ("Marc Maurer",        "On Running",           "DEPARTURE"),
    ("Allison Ellsworth",  "Poppi",                "DEPARTURE"),
    ("Stephen Ellsworth",  "Poppi",                "DEPARTURE"),
    ("Tara Bosch",         "SmartSweets",          "DEPARTURE"),
    ("Craig Dubitsky",     "Hello Products",       "DEPARTURE"),
    ("Morgan Zanotti",     "Primal Kitchen",       "DEPARTURE"),
    ("Kyle Leahy",         "Glossier",             "DEPARTURE"),
    ("Nikki Neuburger",    "Glossier",             "DEPARTURE"),
    ("Julie Bowerman",     "Glossier",             "DEPARTURE"),
    ("Krishna Kaliannan",  "Catalina Crunch",      "DEPARTURE"),
    ("Nina Fuhrman",       "Moon Juice",           "DEPARTURE"),
    ("Tyler Ricks",        "Super Coffee",         "DEPARTURE"),
    ("John Shea",          "Liquid Death",         "DEPARTURE"),
    ("Jason Goldberger",   "Dollar Shave Club",    "DEPARTURE"),
    ("Sally Gilligan",     "Athleta",              "DEPARTURE"),
    ("Nancy Green",        "Athleta",              "DEPARTURE"),
    ("Nick Vlahos",        "Honest Company",       "DEPARTURE"),
    ("Christopher Gallant","Chamberlain Coffee",   "DEPARTURE"),
    ("Mark Gillett",       "Gymshark",             "DEPARTURE"),
    ("David Butler",       "HeyDude",              "DEPARTURE"),
    ("Luke Wood",          "Beats",                "DEPARTURE"),
    ("Ricky Silver",       "Daily Harvest",        "DEPARTURE"),
    ("Heidi Dorosin",      "SmartSweets",          "DEPARTURE"),
    ("Cliff Moskowitz",    "Outdoor Voices",       "DEPARTURE"),
    ("Cher Fuller",        "Alo Yoga",             "DEPARTURE"),
    ("Peter Attia",        "David Protein",        "DEPARTURE"),
    ("Katie Sheeran",      "Parachute",            "DEPARTURE"),
    ("Stephen Kaplan",     "DECIEM",               "DEPARTURE"),
    ("Manish Joneja",      "BarkBox",              "DEPARTURE"),
]
PEOPLE.extend(RECENT_DEPARTURES)

CONVICTION_FOUNDERS = [
    ("Brynn Putnam",          "Mirror"),
    ("Peter Rahal",           "RxBar"),
    ("Gail Becker",           "CAULIPOWER"),
    ("Kara Goldin",           "Hint Water"),
    ("Blake Mycoskie",        "TOMS"),
    ("Andy Dunn",             "Bonobos"),
    ("Seth Goldman",          "Honest Tea"),
    ("Josh Tetrick",          "JUST"),
    ("Ben McKean",            "Hungryroot"),
    ("Mark Samuel",           "Lemon Perfect"),
    ("Jeff Raider",           "Harry's"),
    ("Andy Katz-Mayfield",    "Harry's"),
    ("Neil Blumenthal",       "Warby Parker"),
    ("Dave Gilboa",           "Warby Parker"),
    ("Mark Lynn",             "care/of"),
    ("Craig Elbert",          "care/of"),
    ("Emily Weiss",           "Glossier"),
    ("Mike Cessario",         "Liquid Death"),
    ("Jing Gao",              "Fly by Jing"),
    ("Katy Hearn",            "Alani Nu"),
    ("Ben Goodwin",           "Olipop"),
    ("David Lester",          "Olipop"),
    ("Andrew Dudum",          "Hims"),
    ("Ariel Kaye",            "Parachute"),
    ("Jennifer Hyman",        "Rent the Runway"),
    ("Katrina Lake",          "Stitch Fix"),
    ("Nathan Puksta",         "Filament"),
    ("Nick Green",            "Thrive Market"),
    ("Greg Sewitz",           "Magic Spoon"),
    ("Gabi Lewis",            "Magic Spoon"),
    ("Charlotte Tilbury",     "Charlotte Tilbury"),
    ("Renaldo Webb",          "Wild One"),
    ("Steph Korey",           "Away"),
    ("Jen Rubio",             "Away"),
    ("Tyler Haney",           "Outdoor Voices"),
    ("Rachel Drori",          "Daily Harvest"),
    ("Nick Taranto",          "Plated"),
    ("Josh Hix",              "Plated"),
    ("Katia Beauchamp",       "Birchbox"),
    ("Hayley Barna",          "Birchbox"),
    ("Morgan Hermand-Waiche", "Adore Me"),
    ("Jon Sebastiani",        "KRAVE Jerky"),
    ("Philip Krim",           "Casper"),
    ("Jeff Chapin",           "Casper"),
    ("Sophia Amoruso",        "Nasty Gal"),
    ("Sasha Plavsic",         "ILIA Beauty"),
    ("Jesse Merrill",         "Good Culture"),
    ("Gabby Slome",           "Ollie"),
    ("Ryan Babenzien",        "JOLIE"),
    ("Henry Davis",           "Glossier"),
    ("Neil Parikh",           "Casper"),
    ("Luke Sherwin",          "Casper"),
    ("Jonathan Neman",        "Sweetgreen"),
    ("Nicolas Jammet",        "Sweetgreen"),
    ("Nathaniel Ru",          "Sweetgreen"),
    ("Allison Ellsworth",     "Poppi"),
    ("Stephen Ellsworth",     "Poppi"),
    ("Bill Shufelt",          "Athletic Brewing"),
    ("John Walker",           "Athletic Brewing"),
    ("Ben Francis",           "Gymshark"),
    ("Will Ahmed",            "Whoop"),
    ("Matteo Franceschetti",  "Eight Sleep"),
    ("Alexandra Zatarain",    "Eight Sleep"),
    ("Joe Kudla",             "Vuori"),
    ("Matt Meeker",           "BarkBox"),
    ("Katerina Schneider",    "Ritual"),
    ("Jordan Nathan",         "Caraway"),
    ("Ara Katz",              "Seed"),
    ("Raja Dhir",             "Seed"),
    ("Brian Rudolph",         "Banza"),
    ("Scott Rudolph",         "Banza"),
    ("Katlin Smith",          "Simple Mills"),
    ("Jessica Rolph",         "Lovevery"),
    ("Roderick Morris",       "Lovevery"),
    ("Andrew Benin",          "Graza"),
    ("Shiza Shahid",          "Our Place"),
    ("Amir Tehrani",          "Our Place"),
    ("Jordan DeCicco",        "Super Coffee"),
    ("Jake DeCicco",          "Super Coffee"),
    ("Jim DeCicco",           "Super Coffee"),
    ("Greg Davidson",         "Lalo"),
    ("Michael Wieder",        "Lalo"),
    ("Nick Saltarelli",       "Mid-Day Squares"),
    ("Jake Karls",            "Mid-Day Squares"),
    ("Kat Nouri",             "Stasher"),
    ("Mark Sisson",           "Primal Kitchen"),
    ("Jonathan Regev",        "The Farmer's Dog"),
    ("Brett Podolsky",        "The Farmer's Dog"),
    ("Pete Maldonado",        "Chomps"),
    ("Rashid Ali",            "Chomps"),
    ("Hamdi Ulukaya",         "Chobani"),
    ("David Heath",           "Bombas"),
    ("Randy Goldberg",        "Bombas"),
    ("Trina Spear",           "Figs"),
    ("Danny Winer",           "HexClad"),
    ("Cole Rubin",            "HexClad"),
    ("Joel Clark",            "Kodiak Cakes"),
    ("Jeneva Bell",           "Ruggable"),
    ("Giorgos Tsetis",        "Nutrafol"),
    ("Roland Peralta",        "Nutrafol"),
    ("Lee Mayer",             "Burrow"),
    ("Stephen Kuhl",          "Burrow"),
    ("Jonathan Shokrian",     "MeUndies"),
    ("Steven Borrelli",       "Cuts Clothing"),
    ("Mari Llewellyn",        "Bloom Nutrition"),
    ("Tara Bosch",            "SmartSweets"),
    ("Craig Dubitsky",        "Hello Products"),
    ("Adam Goldenberg",       "Fabletics"),
    ("Yael Aflalo",           "Reformation"),
    ("Rich Fulop",            "Brooklinen"),
    ("Vicki Fulop",           "Brooklinen"),
    ("Simon Enever",          "Quip"),
    ("Amanda Chantal Bacon",  "Moon Juice"),
    ("Paul Tran",             "Manscaped"),
    ("Aamir Baig",            "Article"),
    ("Andy Prochazka",        "Article"),
    ("Freddy Ward",           "Wild"),
    ("Charlie Bowes-Lyon",    "Wild"),
    ("Brandin Cohen",         "Liquid I.V."),
    ("Garrett Waggoner",      "Cirkul"),
    ("William Hicks",         "Magic Mind"),
    ("John Foraker",          "Once Upon a Farm"),
    ("Emma Chamberlain",      "Chamberlain Coffee"),
]
for name, brand in CONVICTION_FOUNDERS:
    PEOPLE.append((name, brand, "CONVICTION"))

EXIT_ALUMNI = [
    ("Bryan Mahoney",        "Glossier"),
    ("Robin Arzón",          "Peloton"),
    ("Selby Drummond",       "Away"),
    ("Natalie Holloway",     "Bala"),
    ("Max Kislevitz",        "Bala"),
    ("Jaclyn Johnson",       "Create & Cultivate"),
    ("Amanda Goetz",         "House of Wise"),
    ("Martin Hoffmann",      "On Running"),
    ("Marc Maurer",          "On Running"),
    ("David Allemann",       "On Running"),
    ("Caspar Coppetti",      "On Running"),
    ("Kyle Leahy",           "Glossier"),
    ("Nikki Neuburger",      "Glossier"),
    ("Julie Bowerman",       "Glossier"),
    ("Colin Walsh",          "Glossier"),
    ("Chris Hall",           "Poppi"),
    ("Rohan Oza",            "Poppi"),
    ("Steve Hewitt",         "Gymshark"),
    ("Mat Dunn",             "Gymshark"),
    ("Noel Mack",            "Gymshark"),
    ("Mark Gillett",         "Gymshark"),
    ("Hillary Super",        "Savage X Fenty"),
    ("Christiane Pendarvis", "Savage X Fenty"),
    ("Terence Reilly",       "HeyDude"),
    ("David Butler",         "HeyDude"),
    ("Scott Friedman",       "Rare Beauty"),
    ("Katie Welch",          "Rare Beauty"),
    ("Jason LaRose",         "Bombas"),
    ("Nicola Kilner",        "DECIEM"),
    ("Monty Sharma",         "Therabody"),
    ("Nick Vlahos",          "Honest Company"),
    ("John Shea",            "Liquid Death"),
    ("Nancy Green",          "Athleta"),
    ("Sally Gilligan",       "Athleta"),
    ("Ricky Silver",         "Daily Harvest"),
    ("Jason Goldberger",     "Dollar Shave Club"),
    ("Heidi Dorosin",        "SmartSweets"),
    ("Morgan Zanotti",       "Primal Kitchen"),
    ("Krishna Kaliannan",    "Catalina Crunch"),
    ("Tyler Ricks",          "Super Coffee"),
    ("Nina Fuhrman",         "Moon Juice"),
    ("Fernando Machado",     "NotCo"),
]
for name, brand in EXIT_ALUMNI:
    PEOPLE.append((name, brand, "ALUMNI"))

BRAND_EXECS = [
    ("Scott Maguire",           "On Running"),
    ("Frank Sluis",             "On Running"),
    ("Olivier Bernhard",        "On Running"),
    ("Danny Harris",            "Alo Yoga"),
    ("Marco DeGeorge",          "Alo Yoga"),
    ("Karen Peters",            "Alo Yoga"),
    ("John Fieldly",            "Celsius"),
    ("Eric Hanson",             "Celsius"),
    ("Jarrod Langhans",         "Celsius"),
    ("Kevin Burns",             "Chobani"),
    ("Tarkan Gürkan",           "Chobani"),
    ("Niel Sandfort",           "Chobani"),
    ("Sumit Singh",             "Chewy"),
    ("Chris Deppe",             "Chewy"),
    ("Scott Anderson",          "Chewy"),
    ("Peter Stern",             "Peloton"),
    ("Sid Thacker",             "Peloton"),
    ("Nick Caldwell",           "Peloton"),
    ("Tarang Amin",             "e.l.f. Beauty"),
    ("Mandy Fields",            "e.l.f. Beauty"),
    ("Kory Marchisotto",        "e.l.f. Beauty"),
    ("Adrian Mitchell",         "Warby Parker"),
    ("Kim Nemser",              "Warby Parker"),
    ("Federico Muyshondt",      "BodyArmor"),
    ("Sabrina Niland",          "BodyArmor"),
    ("Ashley Kechter",          "Vuori"),
    ("Desiree Swanson",         "Vuori"),
    ("Tom Hale",                "Oura"),
    ("Sean Brecker",            "Oura"),
    ("Holly Shelton",           "Oura"),
    ("Daniel Calderoni",        "Kind Snacks"),
    ("Jon Craig",               "Kind Snacks"),
    ("Meera Bhatia",            "Fabletics"),
    ("Sue Collyns",             "Fabletics"),
    ("Sarah Oughtred",          "Figs"),
    ("Steve Berube",            "Figs"),
    ("Joe Vernachio",           "Allbirds"),
    ("Joey Zwillinger",         "Allbirds"),
    ("Jens Grede",              "Skims"),
    ("Gary Schoenfeld",         "Skims"),
    ("Michener Chandlee",       "Whoop"),
    ("Garrett Bastable",        "Whoop"),
    ("John Capodilupo",         "Whoop"),
    ("Benjamin Nazarian",       "Therabody"),
    ("Natalie Guzman",          "Savage X Fenty"),
    ("Mike Reiter",             "HeyDude"),
    ("Joyce Kim",               "Rare Beauty"),
    ("Kate Hassenfeld",         "Bombas"),
    ("Nicola Kilner",           "DECIEM"),
    ("Zahir Ibrahim",           "BarkBox"),
    ("Lindsay Liebman",         "BarkBox"),
    ("Matias Muchnick",         "NotCo"),
    ("Karim Pichara",           "NotCo"),
    ("Monica Royer",            "Ruggable"),
    ("Scott Baldassari",        "Magic Spoon"),
    ("Carla Vernón",            "Honest Company"),
    ("Eric Hohl",               "Harry's"),
    ("Andy Pearson",            "Liquid Death"),
    ("Dan Murphy",              "Liquid Death"),
    ("Michael Kim",             "The Farmer's Dog"),
    ("Jennifer Bremner",        "The Farmer's Dog"),
    ("Damian Hopkins",          "Gymshark"),
    ("Dan D'Agostino",          "Chomps"),
    ("Susan Senecal",           "Athletic Brewing"),
    ("Michael Dubin",           "Dollar Shave Club"),
    ("Tom Bilyeu",              "Quest Nutrition"),
    ("Ron Penna",               "Quest Nutrition"),
    ("Kat Cole",                "AG1"),
    ("Chris Ashenden",          "AG1"),
    ("Andy Gay",                "Cirkul"),
    ("Jenny Ming",              "Rothy's"),
    ("Stephen Hawthornthwaite", "Rothy's"),
    ("Massimo Esposito",        "Eight Sleep"),
    ("Lindsay Jang",            "Article"),
    ("Bess Weatherman",         "Nom Nom"),
    ("Ryan Bartlett",           "True Classic"),
    ("Matt Winnick",            "True Classic"),
    ("Scott Grinker",           "HexClad"),
    ("Nat Noone",               "Ritual"),
    ("Cindy Gustafson",         "Nutrafol"),
    ("Adam Sager",              "Nutrafol"),
    ("Val Oswalt",              "Kodiak Cakes"),
    ("Hali Borenstein",         "Reformation"),
    ("Bill May",                "Quip"),
    ("Sean Leary",              "Lovevery"),
    ("Janelle Strauss",         "Simple Mills"),
    ("Julian Hearn",            "Huel"),
    ("James McMaster",          "Huel"),
    ("Mark Reinstra",           "Banza"),
    ("Mike Keech",              "Liquid I.V."),
    ("Cassandra Curtis",        "Once Upon a Farm"),
    ("Jason Worrel",            "Our Place"),
    ("Doug Behrens",            "Catalina Crunch"),
    ("Sarah Van Houten",        "Catalina Crunch"),
    ("Zach Ranen",              "David Protein"),
    ("Bryan Lalezarian",        "MeUndies"),
    ("Mehdi Oufkir",            "Parachute"),
    ("Sean Christman",          "Cuts Clothing"),
    ("Anisha Raghavan",         "Seed"),
    ("Katrina Hahn",            "SmartSweets"),
    ("Rachel Prescott",         "Caraway"),
    ("Diana Haussling",         "Hello Products"),
    ("Gustav Hossy",            "Chamberlain Coffee"),
    ("Elsie Rutterford",        "Wild"),
    ("Allen Dushi",             "Graza"),
    ("Gabrielle Conforti",      "Outdoor Voices"),
    ("Rob Webb",                "Magic Mind"),
    ("Bill Keith",              "Perfect Bar"),
    ("Leigh Keith",             "Perfect Bar"),
    ("Kabeer Chopra",           "Burrow"),
    ("Federico Troiani",        "Moon Juice"),
    ("Jen Kelly",               "Lalo"),
    ("Lezlie Karls",            "Mid-Day Squares"),
    ("Ana Goettsch",            "Primal Kitchen"),
    ("Catherine Cu",            "Cocofloss"),
    ("Chrystle Cu",             "Cocofloss"),
    ("Moiz Ali",                "Native"),
    ("Mike Natenshon",          "Marine Layer"),
    ("Lucas Braun",             "Manscaped"),
]
for name, brand in BRAND_EXECS:
    PEOPLE.append((name, brand, "EXEC"))


# ══════════════════════════════════════════════════════════════════════════════
# DEDUPLICATION
# ══════════════════════════════════════════════════════════════════════════════

seen_names = set()
deduped = []
for name, brand, tier in PEOPLE:
    key = name.strip().lower()
    if key not in seen_names:
        seen_names.add(key)
        deduped.append({"name": name, "brand": brand, "tier": tier})

print(f"Total unique people to look up: {len(deduped)}")
print(f"  DEPARTURE:  {sum(1 for p in deduped if p['tier'] == 'DEPARTURE')}")
print(f"  CONVICTION: {sum(1 for p in deduped if p['tier'] == 'CONVICTION')}")
print(f"  ALUMNI:     {sum(1 for p in deduped if p['tier'] == 'ALUMNI')}")
print(f"  EXEC:       {sum(1 for p in deduped if p['tier'] == 'EXEC')}")
print(f"\nEstimated cost: ${len(deduped) * 0.04:.2f} (at $0.04/person)")
print()


# ══════════════════════════════════════════════════════════════════════════════
# NINJAPEAR HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_profile(name: str, brand: str) -> dict | None:
    """Single NinjaPear call: GET /api/v2/employee/profile"""
    parts      = name.strip().split()
    first_name = parts[0] if parts else ""
    last_name  = " ".join(parts[1:]) if len(parts) > 1 else ""
    employer   = BRAND_URLS.get(brand, brand)   # URL if known, else company name

    try:
        resp = requests.get(
            f"{BASE}/api/v2/employee/profile",
            params={"first_name": first_name, "last_name": last_name,
                    "employer_website": employer},
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=TIMEOUT,
        )
    except Exception as e:
        print(f"  [NETWORK ERROR] {name}: {e}")
        return None

    if resp.status_code == 402:
        sys.exit("NINJAPEAR: Out of credits. Top up at nubela.co/dashboard.")
    if resp.status_code in (404, 400):
        return None
    if resp.status_code == 503:
        print(f"  [503 retry] {name}", end=" ", flush=True)
        time.sleep(5)
        try:
            resp = requests.get(
                f"{BASE}/api/v2/employee/profile",
                params={"first_name": first_name, "last_name": last_name,
                        "employer_website": employer},
                headers={"Authorization": f"Bearer {KEY}"},
                timeout=TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None
    if resp.status_code != 200:
        print(f"  [HTTP {resp.status_code}] {name}")
        return None

    return resp.json()


def extract_current_company(profile: dict) -> tuple[str | None, str | None]:
    """Return (company_name, title) for current role (end_date is null)."""
    for exp in (profile.get("work_experience") or []):
        if exp.get("end_date") is None:
            return exp.get("company_name"), exp.get("role")
    return None, None


def summarize_profile(profile: dict) -> dict:
    exps    = profile.get("work_experience") or []
    edu     = profile.get("education") or []
    current, _ = extract_current_company(profile)

    past_companies = []
    for exp in exps[:8]:
        c = exp.get("company_name")
        if c and c != current:
            past_companies.append(c)
    schools = [e.get("school") for e in edu[:3] if e.get("school")]

    slug = profile.get("slug")
    profile_url = f"https://nubela.co/people/{slug}" if slug else ""

    return {
        "profile_url":     profile_url,
        "bio":             profile.get("bio") or "",
        "current_company": current,
        "past_companies":  past_companies,
        "schools":         schools,
        "follower_count":  profile.get("follower_count"),
        "x_handle":        profile.get("x_handle"),
        "location":        profile.get("city") or "",
    }


# ══════════════════════════════════════════════════════════════════════════════
# LOAD EXISTING PROGRESS
# ══════════════════════════════════════════════════════════════════════════════

results: dict[str, dict] = {}
if OUT_RAW.exists():
    try:
        results = json.loads(OUT_RAW.read_text())
        print(f"Resuming from {len(results)} already-fetched profiles.")
    except Exception:
        results = {}

already_done = set(results.keys())


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PULL LOOP
# ══════════════════════════════════════════════════════════════════════════════

todo           = [p for p in deduped if p["name"] not in already_done]
total_todo     = len(todo)
found          = 0
not_found      = 0
new_since_save = 0

print(f"Fetching {total_todo} profiles (skipping {len(already_done)} already done)...\n")

for i, person in enumerate(todo, 1):
    name  = person["name"]
    brand = person["brand"]
    tier  = person["tier"]
    print(f"  ({i:03d}/{total_todo}) [{tier}] {name} @ {brand} ...", end=" ", flush=True)

    profile = fetch_profile(name, brand)
    if not profile:
        print("NOT FOUND")
        results[name] = {"found": False, "name": name, "brand": brand, "tier": tier}
        not_found += 1
    else:
        summary = summarize_profile(profile)
        current = summary.get("current_company") or "unknown"
        summary.update({"found": True, "name": name, "brand": brand, "tier": tier})
        results[name] = summary
        print(f"OK — currently: {current}")
        found += 1

    new_since_save += 1
    time.sleep(PAUSE)

    if new_since_save >= SAVE_EVERY:
        OUT_RAW.write_text(json.dumps(results, indent=2))
        new_since_save = 0

OUT_RAW.write_text(json.dumps(results, indent=2))
print(f"\n✓ Pull complete. Found: {found} | Not found: {not_found}")
print(f"  Raw results → {OUT_RAW}")


# ══════════════════════════════════════════════════════════════════════════════
# HOT LEADS
# ══════════════════════════════════════════════════════════════════════════════

EXCLUDE_CURRENT = {
    "pepsi", "pepsico", "unilever", "coca-cola", "cocacola", "nestlé", "nestle",
    "kraft", "heinz", "mars", "procter", "gamble", "colgate", "l'oreal", "loreal",
    "estee", "lauder", "lvmh", "puig", "apple", "crocs", "gap", "lululemon",
    "walmart", "amazon", "meta", "google", "microsoft",
}

hot_leads = []
for name, data in results.items():
    if not data.get("found"):
        continue
    current = (data.get("current_company") or "").strip()
    if not current:
        continue
    known_brand   = data.get("brand", "").lower()
    current_lower = current.lower()
    if known_brand in current_lower or current_lower in known_brand:
        continue
    if any(ex in current_lower for ex in EXCLUDE_CURRENT):
        continue
    is_advisory = any(w in current_lower for w in ["advisor", "consultant", "board", "angel", "investor", "venture"])

    hot_leads.append({
        "name":        name,
        "tier":        data.get("tier"),
        "known_brand": data.get("brand"),
        "current":     current,
        "bio":         data.get("bio", ""),
        "profile_url": data.get("profile_url", ""),
        "advisory":    is_advisory,
    })

tier_order = {"DEPARTURE": 0, "CONVICTION": 1, "ALUMNI": 2, "EXEC": 3}
hot_leads.sort(key=lambda x: (tier_order.get(x["tier"], 4), x["advisory"]))
OUT_HOT.write_text(json.dumps(hot_leads, indent=2))

print(f"\n🔥 HOT LEADS ({len(hot_leads)} people at new companies):")
for hl in hot_leads:
    tag  = "⚡" if hl["tier"] in ("DEPARTURE", "CONVICTION") else "🏆"
    note = " [advisory]" if hl["advisory"] else " ← BUILDING?"
    print(f"  {tag} {hl['name']} ({hl['known_brand']}) → {hl['current']}{note}")
    if hl.get("bio"):
        print(f"     {hl['bio'][:100]}")
print(f"\n  Full hot leads → {OUT_HOT}")


# ══════════════════════════════════════════════════════════════════════════════
# PATTERN BRIEF
# ══════════════════════════════════════════════════════════════════════════════

found_profiles      = [d for d in results.values() if d.get("found")]
conviction_profiles = [d for d in found_profiles if d.get("tier") in ("CONVICTION", "DEPARTURE")]

all_schools: list[str] = []
for d in conviction_profiles:
    all_schools.extend(d.get("schools", []))
school_counts: dict[str, int] = {}
for s in all_schools:
    school_counts[s] = school_counts.get(s, 0) + 1
top_schools = sorted(school_counts.items(), key=lambda x: -x[1])[:15]

followers = [d["follower_count"] for d in found_profiles if d.get("follower_count")]
avg_followers = int(sum(followers) / len(followers)) if followers else 0

past_company_counts: dict[str, int] = {}
for d in conviction_profiles:
    for c in d.get("past_companies", []):
        past_company_counts[c] = past_company_counts.get(c, 0) + 1
top_past = sorted(past_company_counts.items(), key=lambda x: -x[1])[:20]

brief = f"""BULLISH FOUNDER DNA — Pattern Analysis from {len(found_profiles)} NinjaPear Profiles
Generated from {len(deduped)} founder/exec lookups across 97 top consumer brand exits.
============================================================

HOT LEADS — Already Building Something New ({len([h for h in hot_leads if not h['advisory']])} people):
{chr(10).join(f"  • {h['name']} ({h['known_brand']}) → {h['current']}" for h in hot_leads if not h['advisory']) or '  (none found yet)'}

ADVISORY / BOARD MODE ({len([h for h in hot_leads if h['advisory']])} people):
{chr(10).join(f"  • {h['name']} ({h['known_brand']}) → {h['current']}" for h in hot_leads if h['advisory']) or '  (none found yet)'}

TOP SCHOOLS (among conviction founders):
{chr(10).join(f"  {count}x  {school}" for school, count in top_schools) or '  (insufficient data)'}

TOP PRIOR EMPLOYERS (conviction founders before founding their brand):
{chr(10).join(f"  {count}x  {company}" for company, count in top_past) or '  (insufficient data)'}

SOCIAL PRESENCE:
  Average X/Twitter followers (all found): {avg_followers:,}
  Total profiles found: {len(found_profiles)} / {len(deduped)} ({int(len(found_profiles)/len(deduped)*100) if deduped else 0}%)

============================================================
USE THIS TO UPDATE SYSTEM_PROMPT in enrichment.py:
  - School patterns signal founder quality
  - Prior employer patterns (CPG, consulting, brand-adjacent) are strong signals
  - Any "hot lead" filing a trademark → IMMEDIATE HOT signal
"""

OUT_BRIEF.write_text(brief)
print(f"\n📊 Pattern brief → {OUT_BRIEF}")
print("\nDone. Check ~/Desktop/founder_pull_hot_leads.json for who's building something new.")


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-IMPORT INTO THE APP DB
# ══════════════════════════════════════════════════════════════════════════════

API_URL     = os.environ.get("APP_API_URL", "https://web-production-801ed.up.railway.app/api")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "brent@bullish.co")
ADMIN_PASS  = os.environ.get("ADMIN_PASSWORD", "")

if not ADMIN_PASS:
    print("\n⚠️  ADMIN_PASSWORD not set in .env — skipping auto-import to app DB.")
    print("   Add ADMIN_PASSWORD to .env and re-run to import, or use the Founder Radar upload.")
else:
    print(f"\n🔄 Auto-importing {len(results)} profiles into the app DB...")

    # 1. Log in to get a JWT token
    try:
        login_resp = requests.post(
            f"{API_URL}/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
            timeout=15,
        )
        if login_resp.status_code != 200:
            print(f"   ✗ Login failed (HTTP {login_resp.status_code}) — skipping auto-import.")
            login_resp = None
    except Exception as e:
        print(f"   ✗ Login request failed: {e} — skipping auto-import.")
        login_resp = None

    if login_resp and login_resp.status_code == 200:
        token = login_resp.json().get("access_token", "")

        # 2. Build the import payload — shape matches POST /api/admin/founder-profiles/import
        PARENT_COS = {
            "pepsico", "pepsi", "unilever", "coca-cola", "nestlé", "nestle",
            "kraft", "heinz", "mars", "procter", "gamble", "colgate", "l'oreal",
            "loreal", "estee", "lauder", "lvmh", "apple", "crocs", "gap",
            "lululemon", "walmart", "amazon", "meta", "google", "microsoft",
        }

        def derive_status(d):
            if not d.get("found"):
                return "not_found"
            current = (d.get("current_company") or "").strip().lower()
            if not current:
                return "quiet"
            known = (d.get("brand") or "").strip().lower()
            if known in current or current in known:
                return "still_at_brand"
            if any(p in current for p in PARENT_COS):
                return "still_at_brand"
            if any(w in current for w in ["advisor", "board", "investor", "venture", "angel", "consultant"]):
                return "advisory"
            return "building"

        profiles_payload = []
        for name, d in results.items():
            profiles_payload.append({
                "name":            d.get("name", name),
                "brand":           d.get("brand"),
                "tier":            d.get("tier"),
                "current_company": d.get("current_company"),
                "status":          derive_status(d),
                "bio":             d.get("bio") or d.get("headline"),
                "profile_url":     d.get("profile_url"),
                "schools":         d.get("schools", []),
                "past_companies":  d.get("past_companies", []),
                "follower_count":  d.get("follower_count"),
                "x_handle":        d.get("x_handle"),
            })

        # 3. POST to the import endpoint in batches of 100
        total_imported = 0
        total_updated  = 0
        for batch_start in range(0, len(profiles_payload), 100):
            batch = profiles_payload[batch_start:batch_start + 100]
            try:
                import_resp = requests.post(
                    f"{API_URL}/admin/founder-profiles/import",
                    json={"profiles": batch},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                )
                if import_resp.status_code == 200:
                    data = import_resp.json()
                    total_imported += data.get("imported", 0)
                    total_updated  += data.get("updated", 0)
                    print(f"   batch {batch_start//100 + 1}: +{data.get('imported',0)} new, {data.get('updated',0)} updated")
                else:
                    print(f"   ✗ Import batch failed: HTTP {import_resp.status_code}")
            except Exception as e:
                print(f"   ✗ Import batch error: {e}")

        print(f"\n✅ Founder Radar populated: {total_imported} new + {total_updated} updated")
        print(f"   View at: https://brentvartan.github.io/stealth-finder-frontend/founder-radar")
