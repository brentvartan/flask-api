import click
from flask import current_app
from .extensions import db
from .models.user import User


def register_commands(app):
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
