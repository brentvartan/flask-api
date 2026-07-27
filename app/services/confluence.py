"""
Confluence Detection Service

When a new signal is saved, this service:
  1. Normalises the brand name into a stable brand_key
  2. Extracts optional person keys and coined-term keys for cross-brand clustering
  3. Records the signal in signal_events
  4. Checks if a NEW distinct signal type has appeared for this brand CLUSTER
     — cluster = all events sharing brand_key OR a person key OR a coined-term key
  5. If yes → creates a ConfluenceHit and fires an alert email

Brand key normalisation strips legal suffixes (LLC, INC, CORP…) and
punctuation so "NEURO GUM LLC" and "NEURO GUM" match the same key.

Person key clustering catches same-founder signals under different entity names:
  Form D "Lightyear Incorporated" naming John Doe  +
  Trademark "Filament Sciences" also naming John Doe
  → both cluster via person key "john doe" → one confluence hit.

Coined-term clustering catches same-brand signals with slightly different entity names:
  Trademark "OLIPOP"                → coined key "olipop"
  Form D "OLIPOP BEVERAGES LLC"    → coined key "olipop"
  → cluster even though brand_keys differ ("olipop" vs "olipop beverages").

Guardrail: a wrong merge is worse than a missed link.
  - Person keys require BOTH first AND last name.
  - Coined terms must be ≥5 chars, purely alphabetic, and NOT in the common-word blocklist.
"""
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import or_

logger = logging.getLogger(__name__)

# ── Alert policy ──────────────────────────────────────────────────────────────

# Minimum bullish_score required to EMAIL a confluence hit. Hits below this are
# still recorded in the DB and appear in the dashboard and weekly digest — they
# just don't interrupt anyone.
CONFLUENCE_ALERT_MIN_SCORE = 50


def should_send_confluence_alert(bullish_score) -> bool:
    """
    Single source of truth for whether a confluence hit warrants an immediate email.

    BOTH scan paths must call this rather than inlining a threshold:
      - manual scans   → app/api/scans/routes.py
      - scheduled scans → app/services/scheduler.py

    These two paths have silently diverged before. On 2026-07-25 the score gate was
    live on the scheduled path only, while the manual path ran enrichment and
    confluence as parallel threads — so confluence always won the race and emailed
    before any score existed, flooding the inbox with COLD brands. An earlier
    divergence (independent hardcoded json.dumps field whitelists) had the same shape.

    An unscored signal (None) does NOT alert. Enrichment either failed or hasn't run,
    and alerting on an unknown score is precisely what produced the original noise bug.
    """
    if bullish_score is None:
        return False
    try:
        return float(bullish_score) >= CONFLUENCE_ALERT_MIN_SCORE
    except (TypeError, ValueError):
        return False


# Legal suffixes to strip before matching
_LEGAL_SUFFIXES = re.compile(
    r'\b(LLC|INC|CORP|LTD|LIMITED|LP|LLP|CO|COMPANY|INCORPORATED|'
    r'CORPORATION|HOLDINGS|GROUP|BRANDS|VENTURES|STUDIO|STUDIOS)\b\.?',
    re.IGNORECASE,
)
_NON_ALPHA = re.compile(r'[^A-Z0-9 ]')


def normalize_brand(name: str) -> str:
    """Return a lowercase slug used for cross-signal matching."""
    if not name:
        return ""
    upper = name.upper().strip()
    stripped = _LEGAL_SUFFIXES.sub('', upper)
    cleaned = _NON_ALPHA.sub('', stripped)
    return re.sub(r'\s+', ' ', cleaned).strip().lower()


# ── Person key helpers ────────────────────────────────────────────────────────

_ENTITY_WORDS = re.compile(
    r'\b(llc|inc|corp|ltd|lp|llp|pllc|trust|estate|holdings|group|n/a)\b',
    re.IGNORECASE,
)


def normalize_person(name: str) -> str | None:
    """
    Return a stable person key or None if name doesn't look like a real person.

    Format: "firstname lastname" (lowercase, both required).
    Handles "Last, First" form. Rejects corporate entities, N/A values, names
    with digits, and names with fewer than 2 or more than 5 parts.
    """
    if not name or not name.strip():
        return None
    cleaned = name.strip()
    if _ENTITY_WORDS.search(cleaned):
        return None
    if re.search(r'\d', cleaned):
        return None
    if cleaned.lower().strip() in {'n/a', 'n/a n/a', 'na', 'unknown', 'see filing', ''}:
        return None
    # Handle "Last, First" form
    if ',' in cleaned:
        parts = [p.strip() for p in cleaned.split(',', 1)]
        if len(parts) == 2:
            cleaned = f"{parts[1]} {parts[0]}"
    parts = cleaned.split()
    if len(parts) < 2 or len(parts) > 5:
        return None
    return f"{parts[0].lower()} {parts[-1].lower()}"


def extract_person_keys(names: list[str]) -> list[str]:
    """
    Given a list of name strings, return normalized person keys for all that
    look like real persons. Deduplicates. Empty list if no valid names.
    """
    seen: set[str] = set()
    keys: list[str] = []
    for name in (names or []):
        key = normalize_person(name)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


# ── Coined-term key helpers ───────────────────────────────────────────────────

# Purely alphabetic 5+ char tokens that are common English words or generic
# brand modifiers — NOT coined/invented. A token NOT in this set is a candidate
# coined term. Err on the side of including words (conservative = fewer false
# merges). Deliberately does NOT include invented brand tokens like "lightyear",
# "filament", "olipop", "oura", etc.
_COMMON_WORDS: frozenset[str] = frozenset({
    'about', 'above', 'after', 'again', 'ahead', 'alive', 'along', 'among',
    'apart', 'areas', 'aside', 'asset', 'avoid', 'aware', 'badly', 'basic',
    'basis', 'began', 'begin', 'being', 'below', 'birth', 'black', 'blank',
    'blend', 'block', 'blood', 'bloom', 'blown', 'boost', 'bound', 'brand',
    'brave', 'break', 'brief', 'bring', 'broad', 'broke', 'brown', 'build',
    'built', 'buyer', 'calls', 'carry', 'cases', 'catch', 'cause', 'chain',
    'chair', 'cheap', 'check', 'chief', 'chose', 'civic', 'civil', 'claim',
    'class', 'clean', 'clear', 'click', 'close', 'coast', 'color', 'comes',
    'costs', 'could', 'count', 'court', 'cover', 'craft', 'crash', 'cream',
    'cross', 'crowd', 'crown', 'curve', 'cycle', 'daily', 'dance', 'debut',
    'delay', 'depth', 'detox', 'doing', 'doors', 'doubt', 'draft', 'drama',
    'drawn', 'dream', 'dress', 'dried', 'drink', 'drive', 'drops', 'dying',
    'eager', 'early', 'earth', 'eight', 'elite', 'empty', 'ended', 'enjoy',
    'enter', 'entry', 'equal', 'error', 'event', 'every', 'exact', 'exist',
    'extra', 'falls', 'false', 'fancy', 'farms', 'fatal', 'fault', 'feast',
    'fiber', 'fifth', 'fifty', 'filed', 'files', 'fills', 'final', 'fired',
    'first', 'fixed', 'flair', 'flame', 'flash', 'flesh', 'float', 'floor',
    'flora', 'flour', 'fluid', 'flush', 'focus', 'force', 'forte', 'forty',
    'forum', 'found', 'frame', 'frank', 'fresh', 'front', 'frost', 'fruit',
    'fully', 'funds', 'gains', 'games', 'ghost', 'given', 'glass', 'globe',
    'gloss', 'going', 'grace', 'grade', 'grain', 'grand', 'grant', 'grasp',
    'grass', 'great', 'green', 'grind', 'group', 'grown', 'guard', 'guess',
    'guide', 'guilt', 'happy', 'heads', 'heart', 'heavy', 'helps', 'herbs',
    'holes', 'homes', 'honor', 'hours', 'house', 'human', 'ideal', 'image',
    'inner', 'issue', 'items', 'joint', 'juice', 'keeps', 'kinds', 'known',
    'label', 'large', 'later', 'layer', 'leads', 'learn', 'legal', 'level',
    'light', 'limit', 'links', 'lists', 'lives', 'local', 'looks', 'loose',
    'lower', 'lucky', 'lunch', 'magic', 'major', 'makes', 'match', 'media',
    'might', 'minds', 'model', 'money', 'month', 'moral', 'moved', 'moves',
    'multi', 'named', 'needs', 'never', 'night', 'noble', 'noise', 'north',
    'notes', 'novel', 'nurse', 'offer', 'often', 'opens', 'order', 'other',
    'outer', 'owned', 'owner', 'pages', 'paint', 'parts', 'pasta', 'patch',
    'paths', 'phase', 'phone', 'photo', 'picks', 'piece', 'pilot', 'plain',
    'plane', 'plans', 'plant', 'plays', 'point', 'posed', 'posts', 'power',
    'press', 'price', 'pride', 'prime', 'print', 'proof', 'proud', 'pulse',
    'quick', 'quiet', 'quite', 'quote', 'raise', 'range', 'rapid', 'reach',
    'ready', 'realm', 'renew', 'reset', 'right', 'risks', 'roots', 'rough',
    'round', 'royal', 'rules', 'rural', 'saved', 'scale', 'scene', 'score',
    'scout', 'seven', 'shake', 'shall', 'share', 'sharp', 'sheer', 'shift',
    'ships', 'shore', 'shots', 'sight', 'since', 'sixth', 'sized', 'skill',
    'slate', 'sleep', 'slide', 'slick', 'small', 'smart', 'smile', 'solar',
    'solid', 'solve', 'sound', 'south', 'space', 'spark', 'speak', 'speed',
    'spend', 'sport', 'spray', 'squad', 'stack', 'stage', 'stake', 'stand',
    'stark', 'start', 'state', 'steps', 'stick', 'still', 'stock', 'store',
    'storm', 'story', 'sugar', 'suite', 'sunny', 'super', 'sweet', 'swift',
    'table', 'takes', 'taste', 'teams', 'thing', 'think', 'three', 'tight',
    'times', 'today', 'token', 'tools', 'total', 'touch', 'towns', 'track',
    'trade', 'trail', 'train', 'trend', 'trial', 'tribe', 'trick', 'tried',
    'trust', 'truth', 'twice', 'ultra', 'under', 'union', 'unite', 'until',
    'upper', 'urban', 'usage', 'valid', 'value', 'vault', 'vital', 'vivid',
    'vocal', 'voice', 'voter', 'water', 'waves', 'weeks', 'wells', 'white',
    'whole', 'wider', 'winds', 'women', 'works', 'world', 'worry', 'worth',
    'would', 'write', 'years', 'young', 'yours',
    # Common wellness / beauty / food / brand modifiers
    'amino', 'apple', 'aroma', 'berry', 'brush', 'cacao', 'cedar', 'cells',
    'chill', 'citro', 'cocoa', 'coral', 'crisp', 'dandy', 'earthy', 'elixir',
    'fauna', 'feels', 'floss', 'flows', 'foods', 'forge', 'gummy', 'haven',
    'herby', 'honey', 'hydra', 'kefir', 'lemon', 'linen', 'liver', 'maple',
    'melon', 'merry', 'milky', 'minty', 'moist', 'mossy', 'musky', 'nervy',
    'ocean', 'omega', 'onion', 'ozone', 'peach', 'pearl', 'peppy', 'perky',
    'petal', 'piney', 'plant', 'plump', 'plush', 'popsy', 'potty', 'prune',
    'puffy', 'pulpy', 'relax', 'resin', 'rinse', 'roast', 'rocky', 'rosie',
    'rusty', 'salty', 'sandy', 'savor', 'seedy', 'silky', 'sleek', 'smoky',
    'snack', 'soapy', 'spicy', 'stony', 'straw', 'syrup', 'tangy', 'tasty',
    'thyme', 'tonic', 'tummy', 'vegan', 'vigor', 'vital', 'wheat', 'whole',
    'wispy', 'woody', 'yummy', 'zesty', 'zingy', 'zippy',
    # Additional suffixes / generic words that appear in brand-name entity filings
    'beverages', 'brands', 'bureau', 'center', 'circle', 'clouds', 'coding', 'colony',
    'coming', 'common', 'corner', 'create', 'design', 'direct', 'driven',
    'during', 'effect', 'enable', 'ending', 'engine', 'entity', 'evolve',
    'expand', 'expert', 'fabric', 'factor', 'family', 'fields', 'finger',
    'finish', 'flying', 'follow', 'foster', 'future', 'garden', 'gather',
    'gender', 'genius', 'gentle', 'giving', 'global', 'grants', 'growth',
    'happen', 'harbor', 'harder', 'health', 'helper', 'herbal', 'higher',
    'holder', 'honest', 'impact', 'inside', 'intent', 'island', 'itself',
    'joined', 'junior', 'justice', 'keeper', 'launch', 'leader', 'legacy',
    'living', 'loving', 'making', 'manage', 'manner', 'market', 'master',
    'matter', 'method', 'middle', 'modern', 'mother', 'motion', 'moving',
    'native', 'nature', 'nearby', 'needed', 'nourish', 'number', 'offers',
    'origin', 'output', 'parent', 'people', 'plenty', 'portal', 'pretty',
    'primal', 'prompt', 'proper', 'public', 'purely', 'pursue', 'putting',
    'radius', 'rather', 'really', 'recall', 'recent', 'record', 'reduce',
    'refine', 'relief', 'remain', 'repair', 'repeat', 'report', 'rescue',
    'result', 'return', 'reveal', 'reward', 'rising', 'robust', 'rolled',
    'safety', 'sample', 'school', 'search', 'second', 'select', 'sender',
    'series', 'simple', 'single', 'sister', 'sitting', 'skills', 'smooth',
    'source', 'spirit', 'spread', 'spring', 'square', 'stable', 'static',
    'status', 'steady', 'stream', 'street', 'strong', 'studio', 'submit',
    'summer', 'supply', 'system', 'target', 'tested', 'theory', 'throws',
    'toward', 'travel', 'united', 'unless', 'update', 'useful', 'vision',
    'visual', 'within', 'wonder', 'wooden', 'worked', 'writer', 'yellow',
})

# Also strip these from brand names before coined-term extraction
_ENTITY_SUFFIX_RE = re.compile(
    r'\s*,?\s*(LLC|L\.L\.C\.?|Inc\.?|Corp\.?|Corporation|Company|Co\.?'
    r'|Ltd\.?|Limited|L\.P\.?|LLP|PLLC|PC|P\.C\.|Sciences|Labs?\.?'
    r'|Brands?|Studios?|Ventures?|Holdings?|Group)\s*$',
    re.IGNORECASE,
)


def extract_coined_term_keys(brand_name: str) -> list[str]:
    """
    Extract distinctive coined-term keys from a brand name.

    A coined term is a token that:
    - Is purely alphabetic (no digits, no punctuation)
    - Is 5–20 characters long
    - Is NOT a common English word or generic brand modifier
    - Is NOT a legal entity suffix

    Conservative by design — a missed link is better than a wrong merge.
    """
    if not brand_name:
        return []
    clean = _ENTITY_SUFFIX_RE.sub('', brand_name.strip()).strip()
    keys: list[str] = []
    for token in re.split(r'[\s\-_&,\.]+', clean):
        token = token.strip()
        if not token.isalpha():
            continue
        if len(token) < 5 or len(token) > 20:
            continue
        lower = token.lower()
        if lower in _COMMON_WORDS:
            continue
        keys.append(lower)
    return keys


# ── Cluster resolution ────────────────────────────────────────────────────────

def _json_member(key: str) -> str:
    """The exact quoted form a key takes inside the stored JSON array."""
    return json.dumps(key)          # '"john smith"' — quotes on both sides


def _find_cluster(
    owner_id: int,
    brand_key: str,
    person_keys: list[str],
    coined_term_keys: list[str],
):
    """
    Find all existing SignalEvents in the same logical brand cluster.

    Matches by: brand_key equality, OR shared person key, OR shared coined-term key.
    Returns (canonical_brand_key: str, canonical_brand_name: str, matching_events: list).

    canonical_brand_key = earliest-detected event's brand_key; falls back to current
    brand_key if no existing events found.
    """
    from ..models.signal_event import SignalEvent

    # Fast path: direct brand_key match
    direct = SignalEvent.query.filter_by(owner_id=owner_id, brand_key=brand_key).all()

    # Cross-key matches: only scan when we have keys
    cross = []
    if person_keys or coined_term_keys:
        pkey_set = set(person_keys or [])
        ckey_set = set(coined_term_keys or [])

        # PREFILTER IN SQL, then decide in Python exactly as before.
        #
        # This used to load EVERY SignalEvent for the owner and json.loads() two
        # columns on each, for every new signal. Coined-term keys come from the
        # brand name, so nearly every signal took that path: at ~16k events and a
        # 2,000-signal run that is tens of millions of row-loads and JSON parses,
        # and it grows with the corpus. It was the dominant cost of scoring.
        #
        # The keys are stored as a JSON array, so '"john smith"' — quoted on both
        # sides — cannot partially match '"john smithson"'. The LIKE is therefore
        # a strict SUPERSET filter, and the Python check below still makes the
        # final call on exact set intersection. Linking power is unchanged by
        # construction: no row that used to match can be excluded here, because a
        # matching row necessarily contains the quoted key.
        # contains(..., autoescape=True) lets SQLAlchemy build and escape the LIKE
        # itself. Hand-rolling the escape looked right and passed the % case, but
        # an underscore in a key hung the query outright — the driver blocked
        # somewhere a Python signal could not even interrupt.
        conds = [SignalEvent.person_keys.contains(_json_member(k), autoescape=True)
                 for k in pkey_set]
        conds += [SignalEvent.coined_term_keys.contains(_json_member(k), autoescape=True)
                  for k in ckey_set]
        candidates = (
            SignalEvent.query
            .filter_by(owner_id=owner_id)
            .filter(SignalEvent.brand_key != brand_key)
            .filter(or_(*conds))
            .all()
        )
        for ev in candidates:
            matched = False
            if pkey_set:
                ev_pkeys = set(json.loads(ev.person_keys or '[]'))
                if pkey_set & ev_pkeys:
                    matched = True
            if not matched and ckey_set:
                ev_ckeys = set(json.loads(ev.coined_term_keys or '[]'))
                if ckey_set & ev_ckeys:
                    matched = True
            if matched:
                cross.append(ev)

    all_matching = direct + cross
    if not all_matching:
        return brand_key, None, []

    earliest = min(all_matching, key=lambda e: e.detected_at)
    return earliest.brand_key, earliest.brand_name, all_matching


# Safety valve for the cluster expansion below. Two passes is the realistic
# maximum (pass 1 finds the cluster, pass 2 confirms no growth); anything beyond
# that means the key graph is chaining further than expected and we stop rather
# than scan the owner's whole signal history indefinitely.
_MAX_CLUSTER_EXPANSION_PASSES = 4


def _event_keys(event) -> tuple[set, set]:
    """Return (person_keys, coined_term_keys) parsed off a SignalEvent row."""
    try:
        return (
            set(json.loads(event.person_keys or '[]')),
            set(json.loads(event.coined_term_keys or '[]')),
        )
    except (TypeError, ValueError):
        logger.warning(
            "SignalEvent %s has unparseable cluster keys — excluded from alert cluster",
            event.id,
        )
        return set(), set()


def _cluster_events_for_hit(hit) -> list:
    """
    Re-resolve the SAME cluster that produced a ConfluenceHit, oldest first.

    A ConfluenceHit records only owner_id + the canonical brand_key, but
    _find_cluster deliberately clusters across brand_key OR shared person key OR
    shared coined-term key. Re-reading the timeline with a brand_key filter alone
    therefore drops exactly the cross-key evidence the alert exists to show —
    Form D "Lightyear Incorporated" + trademark "Filament Sciences", both naming
    John Doe, rendered as a one-row timeline with span_days=0 while the header
    above it claimed 2 signals.

    Reconstruction seeds from the canonical brand_key's own events, unions the
    person/coined-term keys recorded on everything _find_cluster returns, and
    re-runs _find_cluster until the set stops growing. Matching is symmetric, so
    every member the hit was built from is reachable this way — including a member
    that was chained in through the triggering signal rather than through the
    canonical brand_key (a 3-signal cluster where a single seeded pass would show
    only 2 rows against a header claiming 3).

    Every merge is performed by _find_cluster itself. No second, divergent notion
    of a cluster is introduced, and the timeline can never show an event that the
    production cluster logic would not have merged into this brand's cluster.
    """
    from ..models.signal_event import SignalEvent

    events_by_id = {
        ev.id: ev
        for ev in SignalEvent.query.filter_by(
            owner_id=hit.owner_id, brand_key=hit.brand_key,
        ).all()
    }

    person_keys: set[str] = set()
    coined_term_keys: set[str] = set()

    for _pass in range(_MAX_CLUSTER_EXPANSION_PASSES):
        before_keys = (len(person_keys), len(coined_term_keys))
        before_events = len(events_by_id)

        for ev in list(events_by_id.values()):
            ev_persons, ev_coined = _event_keys(ev)
            person_keys.update(ev_persons)
            coined_term_keys.update(ev_coined)

        _canonical_key, _canonical_name, matched = _find_cluster(
            hit.owner_id, hit.brand_key, sorted(person_keys), sorted(coined_term_keys),
        )
        for ev in matched:
            events_by_id.setdefault(ev.id, ev)

        if (len(person_keys), len(coined_term_keys)) == before_keys and \
                len(events_by_id) == before_events:
            break
    else:
        logger.warning(
            "Confluence alert cluster for %s did not converge in %d passes — "
            "alerting on the %d events found so far",
            hit.brand_key, _MAX_CLUSTER_EXPANSION_PASSES, len(events_by_id),
        )

    return sorted(events_by_id.values(), key=lambda e: e.detected_at)


# ── Main entry point ──────────────────────────────────────────────────────────

def record_signal_and_check_confluence(
    item_id: int,
    owner_id: int,
    brand_name: str,
    signal_type: str,
    source_url: str = None,
    enrichment: dict = None,
    person_keys: list[str] = None,
    coined_term_keys: list[str] = None,
) -> dict:
    """
    Record a new signal event and check for confluence.

    person_keys: normalized person name keys (e.g. ["john smith"]) for cross-brand
        clustering. Extract via extract_person_keys() before calling.
    coined_term_keys: distinctive coined-term keys (e.g. ["lightyear"]) for
        cross-entity clustering. Extract via extract_coined_term_keys() before calling.

    Returns:
        {
            "is_confluence": bool,
            "signal_count":  int,
            "signal_types":  list[str],
            "hit_id":        int | None,
        }
    """
    from ..extensions import db
    from ..models.signal_event import SignalEvent
    from ..models.confluence_hit import ConfluenceHit

    # SignalEvent.brand_key and .brand_name are String(255). A USPTO wordmark can
    # be far longer — USPTO sometimes puts the DESIGN DESCRIPTION in that field
    # (572 chars observed). Truncating Item.title alone was a HALF fix: the
    # over-long value simply moved here, where the flush raises DataError, no
    # SignalEvent is written (so the brand is invisible to triangulation for
    # good), and the poisoned session takes the rest of the batch with it.
    # Clamp at the boundary that owns the columns so every caller is safe.
    brand_name = (brand_name or "")[:255]
    brand_key = normalize_brand(brand_name)[:255]
    if not brand_key:
        return {"is_confluence": False, "signal_count": 1, "signal_types": [signal_type], "hit_id": None}

    person_keys      = list(person_keys or [])
    coined_term_keys = list(coined_term_keys or [])

    # ── 1. Record this signal event ───────────────────────────────────────────
    event = SignalEvent(
        item_id=item_id,
        owner_id=owner_id,
        brand_key=brand_key,
        brand_name=brand_name,
        signal_type=signal_type,
        source_url=source_url,
        detected_at=datetime.now(timezone.utc),
        person_keys=json.dumps(person_keys) if person_keys else None,
        coined_term_keys=json.dumps(coined_term_keys) if coined_term_keys else None,
    )
    db.session.add(event)
    db.session.flush()  # get id without committing

    # ── 2. Resolve the cluster (brand_key + cross-key matches) ───────────────
    canonical_key, canonical_brand_name, cluster_events = _find_cluster(
        owner_id, brand_key, person_keys, coined_term_keys,
    )

    # Union of all signal types in the cluster (including the new one just flushed)
    all_types = list({ev.signal_type for ev in cluster_events} | {signal_type})
    signal_count = len(all_types)

    # ── 3. Only fire confluence if a NEW signal type has appeared ─────────────
    # previous_count = union size BEFORE adding this new event
    existing_types = {ev.signal_type for ev in cluster_events if ev.id != event.id}
    is_new_type    = signal_type not in existing_types
    is_confluence  = is_new_type and len(existing_types) >= 1

    if not is_confluence:
        db.session.commit()
        return {"is_confluence": False, "signal_count": signal_count, "signal_types": all_types, "hit_id": None}

    # ── 4. Log the confluence hit ─────────────────────────────────────────────
    hit_brand_key  = canonical_key
    hit_brand_name = canonical_brand_name or brand_name
    bullish_score  = enrichment.get("bullish_score") if enrichment else None
    watch_level    = enrichment.get("watch_level")   if enrichment else None

    hit = ConfluenceHit(
        owner_id=owner_id,
        brand_key=hit_brand_key,
        brand_name=hit_brand_name,
        signal_count=signal_count,
        signal_types=json.dumps(sorted(all_types)),
        bullish_score=bullish_score,
        watch_level=watch_level,
        alert_sent=False,
    )
    db.session.add(hit)
    db.session.commit()

    logger.info(
        "⚡ Confluence: %s (cluster_key=%s) — %d signals %s (score: %s)",
        hit_brand_name, hit_brand_key, signal_count, all_types, bullish_score,
    )

    # Trigger re-score if brand is on watchlist
    try:
        from flask import current_app
        from .watchlist import trigger_rescore_if_watchlisted
        trigger_rescore_if_watchlisted(
            current_app._get_current_object().app_context(),
            brand_name=hit_brand_name,
            new_signal_type=signal_type,
            owner_id=owner_id,
        )
    except Exception as exc:
        logger.warning("Rescore trigger failed: %s", exc)

    return {
        "is_confluence": True,
        "signal_count":  signal_count,
        "signal_types":  all_types,
        "hit_id":        hit.id,
    }


def send_confluence_alert_for_hit(hit_id: int, alert_emails: list) -> bool:
    """
    Send the confluence alert email for a given ConfluenceHit id.
    Marks hit.alert_sent = True on success.
    """
    from ..extensions import db
    from ..models.confluence_hit import ConfluenceHit
    from ..services.email import send_confluence_alert

    hit = db.session.get(ConfluenceHit, hit_id)
    if not hit or hit.alert_sent:
        return False

    # Build timeline: one row per signal type with earliest detection date.
    # Must be the FULL cluster (brand_key + person-key + coined-term-key matches),
    # not a brand_key-only re-query — see _cluster_events_for_hit.
    events = _cluster_events_for_hit(hit)

    # Deduplicate to first occurrence per signal_type
    seen = {}
    for ev in events:
        if ev.signal_type not in seen:
            seen[ev.signal_type] = ev

    timeline = [
        {
            "signal_type": ev.signal_type,
            "detected_at": ev.detected_at.strftime("%b %d, %Y"),
            "source_url":  ev.source_url,
        }
        for ev in seen.values()
    ]

    # Calculate days between the first and latest signal in the TRUE cluster
    if len(events) >= 2:
        span_days = (events[-1].detected_at - events[0].detected_at).days
    else:
        span_days = 0

    sent_any = False
    for addr in alert_emails:
        try:
            send_confluence_alert(
                to_email=addr,
                brand_name=hit.brand_name,
                brand_key=hit.brand_key,
                signal_count=hit.signal_count,
                signal_types=hit.get_signal_types(),
                timeline=timeline,
                span_days=span_days,
                bullish_score=hit.bullish_score,
                watch_level=hit.watch_level,
            )
            sent_any = True
        except Exception as exc:
            logger.warning("Confluence alert email failed to %s: %s", addr, exc)

    if sent_any:
        hit.alert_sent    = True
        hit.alert_sent_at = datetime.now(timezone.utc)
        db.session.commit()

    return sent_any
