import time
import json
import click
from flask import current_app
from .extensions import db
from .models.user import User


def register_commands(app):

    @app.cli.command("re-enrich")
    @click.option("--batch-size", default=10, show_default=True, help="Signals per batch")
    @click.option("--delay", default=1.5, show_default=True, help="Seconds between API calls")
    @click.option("--dry-run", is_flag=True, default=False, help="Preview without writing")
    @click.option("--limit", default=0, show_default=True, help="Max signals to process (0 = all)")
    def re_enrich(batch_size, delay, dry_run, limit):
        """Re-run AI enrichment on all existing signals using the current prompt."""
        from .models.item import Item
        from .services.enrichment import enrich_signal

        rows = Item.query.filter(
            Item.description.contains('"_type":"signal"')
        ).order_by(Item.created_at.desc()).all()

        if limit:
            rows = rows[:limit]

        total = len(rows)
        click.echo(f"{'[DRY RUN] ' if dry_run else ''}Found {total} signals to re-enrich.")

        updated = skipped = errors = 0

        for i, item in enumerate(rows, 1):
            try:
                meta = json.loads(item.description or "{}")
            except Exception:
                click.echo(f"  [{i}/{total}] SKIP  id={item.id} — bad JSON")
                skipped += 1
                continue

            if meta.get("_type") != "signal":
                skipped += 1
                continue

            company = meta.get("company_name") or item.title
            category = meta.get("category", "Unknown")
            signal_type = meta.get("signal_type", "trademark")
            description = meta.get("description", "")
            notes = meta.get("notes", "")
            owner = meta.get("owner", "")

            click.echo(f"  [{i}/{total}] {company} ({category}) ...", nl=False)

            if dry_run:
                click.echo(" [skip — dry run]")
                continue

            try:
                enrichment = enrich_signal({
                    "companyName": company,
                    "category": category,
                    "signal_type": signal_type,
                    "description": description,
                    "notes": notes,
                    "owner": owner,
                })

                if not enrichment.get("enriched"):
                    click.echo(f" ERROR: {enrichment.get('error', 'unknown')}")
                    errors += 1
                    continue

                meta["enrichment"] = enrichment
                item.description = json.dumps(meta)
                db.session.commit()

                score = enrichment.get("bullish_score", "?")
                level = enrichment.get("watch_level", "?").upper()
                click.echo(f" {level} {score}")
                updated += 1

            except Exception as e:
                db.session.rollback()
                click.echo(f" EXCEPTION: {e}")
                errors += 1

            if i % batch_size == 0:
                click.echo(f"  --- batch {i // batch_size} done, sleeping {delay}s ---")
            time.sleep(delay)

        click.echo(f"\nDone. updated={updated} skipped={skipped} errors={errors}")

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
