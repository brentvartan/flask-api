"""
The SQL prefilter in _find_cluster must not change WHICH events cluster.

Confluence triangulation is the product's core edge, and the invariant is that
changes may only ADD linking power, never remove it. The optimisation replaced
"load every SignalEvent for the owner and json.loads two columns" with a SQL
prefilter plus the same Python decision — so the safety property is exact
equivalence, not merely "still finds some matches".

The reference implementation below mirrors the CURRENT merge policy — including
the entity-variant rule added 2026-08-03 after an audit found the coined-term
join was producing 288 false cross-brand merges. This test guards the SQL
PREFILTER (does pushing the filter into SQL change which rows match?), not the
policy; the policy has its own tests in test_p3_coined_term_precision.py.
"""
import json
from datetime import datetime, timezone, timedelta

import pytest

from app.models.item import Item
from app.models.signal_event import SignalEvent
from app.services.confluence import _find_cluster, _is_entity_variant

PEOPLE = ["john smith", "john smithson", "mary o'neil"]
COINED = ["olipop", "sun_beam", "fifty%off", "filament"]


def _reference(db, owner_id, brand_key, pks, cks):
    """The pre-optimisation algorithm: load everything, decide in Python."""
    direct = SignalEvent.query.filter_by(owner_id=owner_id, brand_key=brand_key).all()
    cross = []
    if pks or cks:
        ps, cs = set(pks or []), set(cks or [])
        for ev in (SignalEvent.query.filter_by(owner_id=owner_id)
                   .filter(SignalEvent.brand_key != brand_key).all()):
            if ps and ps & set(json.loads(ev.person_keys or "[]")):
                cross.append(ev); continue
            if (cs and cs & set(json.loads(ev.coined_term_keys or "[]"))
                    and _is_entity_variant(brand_key, ev.brand_key)):
                cross.append(ev)
    allm = direct + cross
    if not allm:
        return brand_key, None, set()
    e = min(allm, key=lambda x: x.detected_at)
    return e.brand_key, e.brand_name, {x.id for x in allm}


@pytest.fixture
def corpus(db, admin_user):
    base = datetime.now(timezone.utc)
    rows = [
        ("brand-a", ["john smith"],      ["olipop"]),
        ("brand-b", ["john smithson"],   ["sun_beam"]),
        ("brand-c", [],                  ["fifty%off"]),
        ("brand-a", ["mary o'neil"],     []),
        ("brand-d", ["john smith"],      ["filament"]),
    ]
    for i, (bk, pk, ck) in enumerate(rows):
        it = Item(title=f"B{i}", owner_id=admin_user.id, item_type="signal", description="{}")
        db.session.add(it); db.session.flush()
        db.session.add(SignalEvent(
            item_id=it.id, owner_id=admin_user.id, brand_key=bk, brand_name=bk.upper(),
            signal_type="trademark", detected_at=base - timedelta(minutes=i),
            person_keys=json.dumps(pk), coined_term_keys=json.dumps(ck)))
    db.session.commit()
    return admin_user.id


@pytest.mark.parametrize("brand_key", ["brand-a", "brand-c", "unseen-brand"])
@pytest.mark.parametrize("pks", [[], ["john smith"], ["john smithson"], ["mary o'neil"]])
@pytest.mark.parametrize("cks", [[], ["olipop"], ["sun_beam"], ["fifty%off"]])
def test_prefilter_is_equivalent_to_loading_everything(db, corpus, brand_key, pks, cks):
    ref = _reference(db, corpus, brand_key, pks, cks)
    k, n, evs = _find_cluster(corpus, brand_key, pks, cks)
    assert (k, n, {e.id for e in evs}) == ref


def test_a_key_is_not_matched_by_a_longer_key_sharing_its_prefix(db, corpus):
    """
    'john smith' must not match a row keyed 'john smithson'. The keys are stored
    as a JSON array, so matching on the QUOTED form ('"john smith"') makes the
    prefix collision impossible — that is what licenses the SQL prefilter.
    """
    _, _, evs = _find_cluster(corpus, "unseen-brand", ["john smith"], [])
    keys = {k for e in evs for k in json.loads(e.person_keys or "[]")}
    assert "john smithson" not in keys
    assert "john smith" in keys


@pytest.mark.parametrize("key", ["sun_beam", "fifty%off"])
def test_like_wildcards_in_a_key_do_not_widen_the_match(db, corpus, key):
    """A key containing % or _ must match itself and nothing else."""
    _, _, evs = _find_cluster(corpus, "unseen-brand", [], [key])
    for e in evs:
        assert key in json.loads(e.coined_term_keys or "[]")
