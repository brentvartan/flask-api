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
            Item.item_type == 'signal'
        ).order_by(Item.created_at.desc()).all()

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

    @app.cli.command("watchlist-mute")
    @click.argument("founder_name")
    @click.option("--unmute", is_flag=True, default=False, help="Remove muted flag instead")
    def watchlist_mute(founder_name, unmute):
        """Set or clear muted:true on a watchlist entry by founder name.

        Usage: flask watchlist-mute "Charlotte Tilbury"
               flask watchlist-mute "Charlotte Tilbury" --unmute
        """
        from .models.item import Item

        rows = Item.query.filter(Item.item_type == 'watchlist').all()
        updated = 0
        for row in rows:
            try:
                meta = json.loads(row.description or '{}')
            except Exception:
                continue
            if meta.get('name', '').lower() == founder_name.lower():
                if unmute:
                    meta.pop('muted', None)
                else:
                    meta['muted'] = True
                row.description = json.dumps(meta)
                db.session.commit()
                action = "unmuted" if unmute else "muted"
                click.echo(f"✓ {action} watchlist item id={row.id} name='{meta['name']}'")
                updated += 1
        if updated == 0:
            click.echo(f"No watchlist items found with name='{founder_name}'")

    @app.cli.command("export-form-d-officers")
    @click.option("--output", default="form_d_officers.csv", show_default=True, help="Output CSV path")
    @click.option("--limit", default=0, show_default=True, help="Max signals to process (0 = all)")
    @click.option("--workers", default=8, show_default=True, help="Parallel EDGAR fetch workers")
    def export_form_d_officers(output, limit, workers):
        """Export officer/director names from stored Form D signals as a Clay/Apollo seed list.

        Queries all delaware-type signals, re-fetches officer names from EDGAR XML,
        deduplicates by name, and writes a CSV ready to upload to Clay or Apollo.

        Usage: flask export-form-d-officers
               flask export-form-d-officers --output ~/Desktop/seed_list.csv --limit 200
        """
        import csv
        import threading
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from .models.item import Item
        from .services.delaware import fetch_form_d_related_persons

        rows = Item.query.filter(Item.item_type == 'signal').order_by(Item.created_at.desc()).all()

        # Filter to Form D signals that have _adsh + _cik
        form_d_signals = []
        for row in rows:
            try:
                meta = json.loads(row.description or '{}')
            except Exception:
                continue
            if meta.get('signal_type') != 'delaware':
                continue
            adsh = meta.get('_adsh', '')
            cik  = meta.get('_cik', '')
            if not adsh or not cik:
                continue
            form_d_signals.append({
                'id':          row.id,
                'company':     meta.get('companyName') or row.title,
                'adsh':        adsh,
                'cik':         cik,
                'file_date':   (meta.get('timestamp') or '')[:10],
                'description': meta.get('description', ''),
            })

        if limit:
            form_d_signals = form_d_signals[:limit]

        total = len(form_d_signals)
        click.echo(f"Found {total} Form D signals. Fetching officer names from EDGAR ({workers} workers)…")

        results = []
        lock = threading.Lock()
        counter = {'done': 0}
        flask_app = current_app._get_current_object()

        def fetch_one(sig):
            persons = fetch_form_d_related_persons(sig['adsh'], sig['cik'])
            rows_out = []
            for p in persons:
                rows_out.append({
                    'name':          p['name'],
                    'relationships': ', '.join(p.get('relationships') or []),
                    'company':       sig['company'],
                    'form_d_date':   sig['file_date'],
                    'edgar_adsh':    sig['adsh'],
                    'edgar_cik':     sig['cik'],
                })
            with lock:
                counter['done'] += 1
                n = counter['done']
                if n % 25 == 0 or n == total:
                    click.echo(f"  {n}/{total} processed…")
            return rows_out

        with flask_app.app_context():
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(fetch_one, sig): sig for sig in form_d_signals}
                for future in as_completed(futures):
                    batch = future.result()
                    if batch:
                        results.extend(batch)

        # Deduplicate: keep first occurrence of each name (case-insensitive),
        # but track all companies they appeared in.
        seen = {}
        for r in results:
            key = r['name'].lower().strip()
            if key not in seen:
                seen[key] = r.copy()
                seen[key]['all_companies'] = [r['company']]
            else:
                if r['company'] not in seen[key]['all_companies']:
                    seen[key]['all_companies'].append(r['company'])

        deduped = []
        for entry in seen.values():
            entry['all_companies'] = ' | '.join(entry['all_companies'])
            deduped.append(entry)

        # Sort: most recent filings first, then alphabetically by name
        deduped.sort(key=lambda r: (r['form_d_date'], r['name']), reverse=True)

        fieldnames = ['name', 'relationships', 'company', 'all_companies', 'form_d_date', 'edgar_adsh', 'edgar_cik']
        with open(output, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(deduped)

        click.echo(f"\n✓ Wrote {len(deduped)} unique officers/directors to {output}")
        click.echo(f"  (from {total} Form D signals, {len(results)} total name mentions)")

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
