import json
from app.extensions import db as _db
from app.models.item import Item

def _sig(owner, name, **enr):
    _db.session.add(Item(title=name, item_type="signal", owner_id=owner,
        description=json.dumps({"company_name": name, "enrichment": enr})))

def test_corpus_stats(client, db, admin_user, admin_token):
    _sig(admin_user.id, "a", enriched=True, watch_level="hot", bullish_score=80)
    _sig(admin_user.id, "b", enriched=True, watch_level="warm", bullish_score=60)
    _sig(admin_user.id, "c", enriched=True, watch_level="cold", bullish_score=20)
    db.session.add(Item(title="d", item_type="signal", owner_id=admin_user.id,
                        description=json.dumps({"company_name": "d"})))
    db.session.commit()
    r = client.get("/api/admin/corpus-stats",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, r.get_data()
    d = r.get_json()
    assert d["total_signals"] == 4 and d["assessed"] == 3 and d["unassessed"] == 1
    assert d["tiers"] == {"hot": 1, "warm": 1, "cold": 1}
