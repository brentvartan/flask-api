# Flask API — Phase 1

REST API with PostgreSQL, JWT auth, and Alembic migrations.

## Prerequisites

- Python 3.12 (via pyenv — already installed)
- PostgreSQL

## 1. Install PostgreSQL

**Option A — Postgres.app (easiest on macOS):**
Download and install from https://postgresapp.com, then start it from the menu bar.

**Option B — Homebrew:**
```bash
brew install postgresql@16
brew services start postgresql@16
```

## 2. Create the database

```bash
psql postgres -c "CREATE DATABASE flask_api_dev;"
```

## 3. Configure environment

```bash
cp .env.example .env
# Edit .env if your Postgres user/password differ from defaults
```

## 4. Activate the virtual environment

```bash
cd ~/Desktop/flask-api
source venv/bin/activate
```

## 5. Run migrations

```bash
flask db migrate -m "initial schema"   # generates the migration file
flask db upgrade                        # applies it to the database
```

## 6. Start the server

```bash
python run.py
# → http://127.0.0.1:5000
```

---

## API Endpoints

| Method | URL                  | Auth? | Description              |
|--------|----------------------|-------|--------------------------|
| POST   | /api/auth/register   | No    | Create account           |
| POST   | /api/auth/login      | No    | Login, returns JWT       |
| POST   | /api/auth/logout     | JWT   | Revoke token             |
| GET    | /api/auth/me         | JWT   | Current user profile     |
| POST   | /api/auth/refresh    | JWT (refresh) | New access token |

## Quick test with curl

```bash
# Register
curl -s -X POST http://localhost:5000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"secret123","first_name":"Jane","last_name":"Doe"}' | jq

# Login
TOKEN=$(curl -s -X POST http://localhost:5000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"secret123"}' | jq -r '.access_token')

# Get current user
curl -s http://localhost:5000/api/auth/me \
  -H "Authorization: Bearer $TOKEN" | jq
```
