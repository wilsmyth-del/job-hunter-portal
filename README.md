# Job Finder Portal

Invite-only job digest service. Users set up search queries and a weekly delivery schedule; a daily scraper fetches LinkedIn listings and emails new results each morning.



---

## What it does

- Invite-code signup (Gmail-style — each code works once)
- Per-user search queries (up to 8) and location
- 7-day delivery schedule (day-of-week pill selector)
- Daily scraper deduplicates against each user's seen-job history — no repeat listings
- HTML digest email, dark-themed, matching the portal UI
- Feedback form routed back to the admin

## Stack

- **Flask** + **SQLite** — no ORM, plain `sqlite3`
- **PythonAnywhere** — web app (WSGI) + scheduled task
- **Gmail SMTP** — app password, port 465

## Project structure

```
app.py          Flask app — routes, auth, admin
db.py           SQLite helpers (users, queries, invite codes, seen jobs)
scraper.py      Daily scheduled task — fetch, dedup, email
templates/      Jinja2 templates
requirements.txt
```

## Environment variables

Set in PythonAnywhere's `.env` or environment config — never committed.

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session secret |
| `PORTAL_API_KEY` | Admin API key |
| `GMAIL_USER` | Sending address (Gmail) |
| `GMAIL_APP_PASSWORD` | Gmail app password (spaces stripped automatically) |

## Deployment (PythonAnywhere)

1. Clone the repo into your home directory
2. Create a virtualenv and `pip install -r requirements.txt`
3. Set the four environment variables above
4. Point the WSGI file at `app.py`
5. Add a daily scheduled task: `python3 /home/<user>/job-hunter-portal/scraper.py` at 14:00 UTC (6 am PST)

## Scraper behaviour

- Runs once daily via PythonAnywhere scheduled tasks
- Fetches LinkedIn guest API (no key required) — 2 pages per query
- Filters a blocklist of aggregator domains (Jooble, Talent.com, etc.)
- Per-user deduplication via `seen_jobs` table — users only ever see each listing once
- Sends HTML + plain-text digest only when there are new results
- 0.5 s gap between queries, 2 s gap between users

## Invite system

New users need a valid invite code to register. Each code is single-use. Existing users get codes via their dashboard to share with friends.
