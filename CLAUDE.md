# Bullish API — Claude Code Context

## What this project is
A production Flask REST API for the Bullish platform. Handles user auth (JWT), a generic items resource, and admin tooling. Deployed on Railway.

**Live URL:** https://web-production-801ed.up.railway.app
**Health check:** https://web-production-801ed.up.railway.app/health → `{"status": "ok"}`
**Swagger docs (dev only):** http://localhost:5000/api/docs

---

## Stack
| Layer | Technology |
|---|---|
| Framework | Flask 3.1 |
| Database | PostgreSQL (via Flask-SQLAlchemy + Flask-Migrate) |
| Auth | Flask-JWT-Extended (access + refresh tokens) |
| Rate limiting | Flask-Limiter + Redis |
| Password hashing | Flask-Bcrypt |
| Email | Resend (`resend` SDK) |
| Error tracking | Sentry (`sentry-sdk[flask]`) |
| API docs | Flasgger / Swagger (dev only) |
| Testing | pytest + pytest-flask + pytest-cov |
| WSGI | Gunicorn |

---

## Project structure
```
flask-api/
├── app/
│   ├── __init__.py          # App factory (create_app)
│   ├── config.py            # Dev / Test / Production configs
│   ├── extensions.py        # db, migrate, jwt, bcrypt, limiter
│   ├── schemas.py           # Marshmallow schemas
│   ├── cli.py               # Flask CLI commands
│   ├── api/
│   │   ├── auth/routes.py   # Auth blueprint
│   │   ├── items/routes.py  # Items blueprint
│   │   └── admin/routes.py  # Admin blueprint
│   ├── models/
│   │   ├── user.py
│   │   ├── item.py
│   │   └── token_blocklist.py
│   ├── services/
│   │   ├── email.py         # Resend email sending
│   │   └── tokens.py        # Token helpers
│   └── utils/
├── migrations/              # Alembic migrations
├── tests/                   # pytest test suite
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── railway.json
├── Procfile                 # web: gunicorn wsgi:app
└── run.py                   # Local dev entry point
```

---

## API endpoints

### Auth — `/api/auth`
| Method | Path | Auth required | Description |
|---|---|---|---|
| POST | `/register` | No | Create user (email + password) |
| POST | `/login` | No | Returns access + refresh tokens |
| POST | `/logout` | Yes (access) | Revokes token (adds to blocklist) |
| GET | `/me` | Yes (access) | Returns current user profile |
| POST | `/refresh` | Yes (refresh) | Returns new access token |
| POST | `/forgot-password` | No | Sends reset email via Resend |
| POST | `/reset-password` | No | Validates token, updates password |

### Items — `/api/items`
| Method | Path | Auth required | Description |
|---|---|---|---|
| GET | `` | Yes | List all items |
| POST | `` | Yes | Create item |
| GET | `/<id>` | Yes | Get item |
| PUT | `/<id>` | Yes | Update item |
| DELETE | `/<id>` | Yes | Delete item |

### Admin — `/api/admin`
| Method | Path | Auth required | Description |
|---|---|---|---|
| GET | `/users` | Yes (admin) | List all users |
| PATCH | `/users/<id>` | Yes (admin) | Update user |

---

## Environment variables

### Local dev (`.env` file in project root)
```
FLASK_ENV=development
SECRET_KEY=<any-dev-secret>
JWT_SECRET_KEY=<any-dev-secret>
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/flask_api_dev
# Optional for local:
REDIS_URL=redis://localhost:6379/0
MAIL_SUPPRESS_SEND=true
```

### Production (Railway — set via dashboard or Raw Editor)
```
FLASK_ENV="production"
SECRET_KEY="fb24039bbdb1369a36f0b3ef86b9618a2212ee54d59c09a12fc5b81d017e613e"
JWT_SECRET_KEY="9de9fc68ad8f6fe4a51f4b147a2f214250b00bdde92c5026a5abcf00cf112b7c"
DATABASE_URL="${{Postgres.DATABASE_URL}}"
REDIS_URL="${{Redis.REDIS_URL}}"
SENTRY_DSN="https://f4e57883993cf2fba8b3695d01137ca9@o4511066098499584.ingest.us.sentry.io/4511066101055488"
RESEND_API_KEY="re_ean9ezLA_Gd9qd4ZHP9iogPyzohMztAqk"
MAIL_FROM="noreply@mail.bullish.co"
FRONTEND_URL="http://localhost:5173"
```

---

## Railway deployment

- **Dashboard:** https://railway.com/project/9d4dcc81-48e1-4485-8039-f16f3d54f928
- **Service ID:** 4af5b3e6-6258-4300-94e3-4663d9d37be5
- **Environment ID:** b8602d23-051d-4113-9e42-04fa9873d77e
- **Variables page:** https://railway.com/project/9d4dcc81-48e1-4485-8039-f16f3d54f928/service/4af5b3e6-6258-4300-94e3-4663d9d37be5/variables?environmentId=b8602d23-051d-4113-9e42-04fa9873d77e
- **Services:** web (Flask/Gunicorn), Postgres, Redis
- **Deploy trigger:** push to `main` branch OR manual deploy from dashboard
- **Use Raw Editor** to add env vars with special chars (avoids Railway's AI autocomplete triggering)

---

## External services

### Sentry
- Org: `bullish-wn` → https://bullish-wn.sentry.io
- Project: `python-flask` (ID: 4511066101055488)
- Only active when `SENTRY_DSN` is set (not in dev/test)
- `traces_sample_rate=0.2`, `send_default_pii=False`

### Resend (email)
- Account: brent@bullish.co
- Sending domain: `mail.bullish.co` (ID: `95a82217-b5d9-41b8-a906-5a9ee6f9b82c`)
- Domain status: **pending DNS verification** (DNS records need to be added to DreamHost)
- `MAIL_FROM=noreply@mail.bullish.co`
- Suppress sends in test/dev via `MAIL_SUPPRESS_SEND=true`

### DNS (bullish.co)
- **Registrar:** DreamHost (panel.dreamhost.com)
- **Nameservers:** Currently pointing to defunct WebFaction servers — need to be changed to `ns1/2/3.dreamhost.com`
- **Pending action:** After nameserver change, add 3 Resend DNS records to DreamHost, then verify domain in Resend dashboard

---

## Local development

```bash
# Install dependencies
pip install -r requirements.txt

# Set up .env (copy from above)

# Create local database
createdb flask_api_dev

# Run migrations
flask db upgrade

# Start dev server
python run.py
# or
flask run
```

---

## Testing

```bash
# Run full suite
pytest

# With coverage
pytest --cov=app --cov-report=term-missing

# Single file
pytest tests/test_auth.py -v
```

- Test config uses `MAIL_SUPPRESS_SEND=true` (no real emails sent)
- Rate limiting disabled in test config
- Requires a local `flask_api_test` PostgreSQL database

---

## Database migrations

```bash
# After changing a model
flask db migrate -m "describe the change"
flask db upgrade

# Railway runs migrations automatically on deploy via Procfile / start.sh
```

---

## Key design decisions
- **Token blocklist:** Logout revokes JWTs by storing the `jti` in `TokenBlocklist` table (PostgreSQL, not Redis) so revocations survive restarts
- **Rate limiting:** Falls back to in-memory if `REDIS_URL` is not set (fine for dev, use Redis in prod)
- **Sentry:** Guarded by `if sentry_dsn:` — never initialises in dev/test unless explicitly set
- **Swagger:** Only mounted in non-production environments (Flasgger has Flask 3.x compatibility quirks)
- **Password reset:** Token generated server-side, emailed via Resend, expires after use
