import time
import json
import click
from flask import current_app
from .extensions import db
from .models.user import User


def register_commands(app):

    @app.cli.command("re-enrich")
    @click.option("--dry-run", is_flag=True, default=False, help="Preview without writing")
    @click.option("--limit", default=0, show_default=True, help="Max signals to process (0 = all)")
    @click.option("--workers", default=5, show_default=True, help="Parallel API workers")
    def re_enrich(dry_run, limit, workers):
        """Re-run AI enrichment on all existing signals using the current prompt."""
        import threading
        from .models.item import Item
        from .services.enrichment import enrich_signal

        rows = Item.query.filter(
            Item.description.contains('_type')
        ).filter(
            Item.description.contains('signal')
        ).order_by(Item.created_at.desc()).all()
        # Filter in Python to handle both "key":"val" and "key": "val" JSON formats
        rows = [r for r in rows if '"_type"' in (r.description or '') and 'signal' in (r.description or '')]

        if limit:
            rows = rows[:limit]

        total = len(rows)
        click.echo(f"{'[DRY RUN] ' if dry_run else ''}Found {total} signals to re-enrich with {workers} workers.")

        counters = {"updated": 0, "skipped": 0, "errors": 0}
        lock = threading.Lock()

        from flask import current_app
        from concurrent.futures import ThreadPoolExecutor, as_completed
        flask_app = current_app._get_current_object()

        def process(i, item):
            try:
                meta = json.loads(item.description or "{}")
            except Exception:
                with lock:
                    click.echo(f"  [{i}/{total}] SKIP id={item.id} — bad JSON")
                    counters["skipped"] += 1
                return

            if meta.get("_type") != "signal":
                with lock:
                    counters["skipped"] += 1
                return

            company = meta.get("company_name") or item.title
            category = meta.get("category", "Unknown")

            if dry_run:
                with lock:
                    click.echo(f"  [{i}/{total}] {company} ({category}) [dry run]")
                return

            try:
                # Look up confluence data for this brand
                from .services.confluence import normalize_brand
                from .models.signal_event import SignalEvent
                brand_key = normalize_brand(company)
                signal_count = 1
                signal_types_list = []
                if brand_key:
                    with flask_app.app_context():
                        events = (
                            SignalEvent.query
                            .filter_by(brand_key=brand_key)
                            .with_entities(SignalEvent.signal_type)
                            .all()
                        )
                        signal_types_list = list({e.signal_type for e in events})
                        signal_count = max(len(signal_types_list), 1)

                enrichment = enrich_signal({
                    "companyName": company,
                    "category": category,
                    "signal_type": meta.get("signal_type", "trademark"),
                    "description": meta.get("description", ""),
                    "notes": meta.get("notes", ""),
                    "owner": meta.get("owner", ""),
                    "signal_count": signal_count,
                    "signal_types": signal_types_list,
                })

                if not enrichment.get("enriched"):
                    with lock:
                        click.echo(f"  [{i}/{total}] {company} ERROR: {enrichment.get('error', 'unknown')}")
                        counters["errors"] += 1
                    return

                meta["enrichment"] = enrichment
                new_desc = json.dumps(meta)

                with flask_app.app_context():
                    from .models.item import Item as _Item
                    from .extensions import db as _db
                    obj = _db.session.get(_Item, item.id)
                    if obj:
                        obj.description = new_desc
                        _db.session.commit()

                score = enrichment.get("bullish_score", "?")
                level = (enrichment.get("watch_level") or "?").upper()
                with lock:
                    click.echo(f"  [{i}/{total}] {company} → {level} {score}")
                    counters["updated"] += 1

            except Exception as e:
                with lock:
                    click.echo(f"  [{i}/{total}] {company} EXCEPTION: {e}")
                    counters["errors"] += 1

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(process, i, item): item for i, item in enumerate(rows, 1)}
            for future in as_completed(futures):
                pass  # results logged inside process()

        click.echo(f"\nDone. updated={counters['updated']} skipped={counters['skipped']} errors={counters['errors']}")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True, help="Admin email address")
    @click.option("--password", prompt=True, hide_input=True,
                  confirmation_prompt=True, help="Admin password")
    @click.option("--first-name", prompt=True, help="First name")
    @click.option("--last-name", prompt=True, help="Last name")
    def create_admin(email, password, first_name, last_name):
        """Create an admin user."""
        if len(password) < 8:
            raise click.ClickException("Password must be at least 8 characters.")

        existing = User.query.filter_by(email=email.lower()).first()
        if existing:
            if existing.role == "admin":
                raise click.ClickException(f"{email} is already an admin.")
            existing.role = "admin"
            db.session.commit()
            click.echo(f"✓ Promoted {email} to admin.")
            return

        user = User(
            email=email.lower(),
            first_name=first_name,
            last_name=last_name,
            role="admin",
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        click.echo(f"✓ Admin user {email} created (id={user.id}).")
