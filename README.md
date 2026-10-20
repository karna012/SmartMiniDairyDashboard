# Smart Mini-Dairy Dashboard

Standalone FastAPI dairy-management application, maintained separately from
Degen Predictions. The deployed application is mounted at
`https://degenpredictions.com/smartdairy/`.

This repository contains the deployed application source, routing-aware
templates, Degen charcoal/emerald theme, import templates and tests. It deliberately
does **not** contain the live SQLite database, uploaded photos, credentials,
environment files, or virtual environment.

## Features

- Live station monitoring and herd profiles
- Milk-yield and feed-inventory reports
- Feed, water and station-session records
- CSV/XLSX imports, sample files and photo-upload support
- Admin controls and business-rule validation
- Responsive Degen Dark theme, with an optional light palette

## Local development

Use a fresh checkout without production records. Python 3.11 or later is
recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ADMIN_PASSWORD = "choose-a-strong-local-password"
$env:DAIRY_BASE_PATH = ""
uvicorn app:app --host 127.0.0.1 --port 8001
```

Open `http://127.0.0.1:8001/`. Environment variables must be set in the process
or service; `.env.example` is documentation, not an automatically loaded file.

The app creates `data.db` on startup. `db_init.py` force-seeds demo records:
**never run it against a production database**. Demo seeding is optional and
should only be performed in an empty development checkout.

## Deployment

Set `DAIRY_BASE_PATH=/smartdairy` when serving under that prefix. The current Pi
deployment uses Uvicorn on loopback port 8001 behind nginx and the site's HTTPS
proxy. Keep the database and uploads on persistent private storage, outside Git.
Back up SQLite consistently before schema or application changes.

This source export does not redeploy, restart, or alter the running application.
Use the running server's configuration and secret store when deploying; do not
replace it with example values.

## Testing

Run business-rule tests only in a disposable checkout with a disposable
`data.db`; the existing tests write sample records and initialize the schema.

```powershell
python -m pytest tests/test_business_rules.py -q
```

`tests/check_degen_theme.py` is an optional Playwright-based, read-only browser
check against the deployed site. Playwright and its browsers are separate
development dependencies, not included in the server requirements. Its admin
check reflects the existing authentication implementation described below.

## Security status

This is a private source backup, **not a security-hardened release**. Known
issues in the deployed application remain:

- Admin authentication trusts an unsigned cookie.
- The dashboard and station API are publicly accessible; ingestion endpoints
  need appropriate authentication before untrusted access.
- The bare `/smartdairy` redirect needs an HTTPS-forwarding correction.
- The source's default admin password is for development only.

Keep this repository private. Private GitHub visibility does not fix these
application issues. Protect the running service with an appropriate access
gateway until authentication and API protection are hardened.
