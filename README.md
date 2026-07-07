# Job Finder Portal

Invite-only job digest service. Users set up search queries and a delivery schedule; an hourly scraper fetches LinkedIn listings and emails new results at each user's own chosen Pacific hour.

---

## What it does

- Invite-code signup (Gmail-style — each code works once)
- Per-user search queries (up to 8) and location
- Delivery schedule: day-of-week pill selector + a chosen delivery hour (5am-10am Pacific, DST-correct year-round)
- On-demand "Test My Searches" — run your saved queries live from the dashboard without waiting for the scheduled digest
- Scraper deduplicates against each user's seen-job history — no repeat listings
- HTML digest email, dark-themed, matching the portal UI, with a pause/delete link in the footer
- Self-serve account management: forgot/change password, update display name, pause or delete account (dashboard or the emailed link) — deleting sends a farewell email with a fresh invite code
- Feedback form routed back to the admin

## Stack

- **Flask** + **SQLite** — no ORM, plain `sqlite3`
- **PythonAnywhere** — web app (WSGI) + scheduled task
- **Gmail SMTP** — app password, port 465

## Project structure

```
app.py          Flask app — routes, auth, admin, account management
db.py           SQLite helpers (users, queries, invite codes, seen jobs)
scraper.py      Hourly scheduled task — fetch, dedup, email
tokens.py       Signed, non-expiring tokens (unsubscribe link), shared by app.py and scraper.py
templates/      Jinja2 templates
requirements.txt
```

## Environment variables

Set in PythonAnywhere's `.env` or environment config — never committed.

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session secret — also signs the unsubscribe token (`tokens.py`); changing it invalidates unsubscribe links already sent |
| `PORTAL_API_KEY` | Scraper's `/api/users` polling key |
| `ADMIN_KEY` | Admin panel passphrase |
| `GMAIL_USER` | Sending address (Gmail) |
| `GMAIL_APP_PASSWORD` | Gmail app password (spaces stripped automatically) |

## Deployment (PythonAnywhere)

1. Clone the repo into your home directory
2. Create a virtualenv and `pip install -r requirements.txt`
3. Set the four environment variables above
4. Point the WSGI file at `app.py`
5. Add an hourly scheduled task: `python3 /home/<user>/job-hunter-portal/scraper.py` at :00 past every hour
6. After pulling code changes, click **Reload** on the Web tab — a `git pull` alone doesn't restart the running app

## Scraper behaviour

- Runs hourly via a PythonAnywhere scheduled task; each user is delivered at their own chosen Pacific hour (`delivery_hour`), computed via `zoneinfo` so it's correct through DST automatically
- Fetches LinkedIn guest API (no key required) — 2 pages per query
- Filters a blocklist of aggregator domains (Jooble, Talent.com, etc.)
- Per-user deduplication via `seen_jobs` table — users only ever see each listing once
- Sends HTML + plain-text digest only when there are new results
- 0.5 s gap between queries, 2 s gap between users

## Invite system

New users need a valid invite code to register. Each code is single-use. Existing users get codes via their dashboard to share with friends.

## Account management

- Pause and delete are both reachable from the dashboard or a signed, non-expiring unsubscribe link in every digest email footer (`tokens.py`).
- Pausing is link-only (no login) — fully reversible, no data lost.
- Deleting via the emailed link requires being logged in as that specific account first, since the link alone can end up in someone else's hands if the email gets forwarded. Deletion removes the user's queries and seen-job history and sends a farewell email with a fresh, non-expiring invite code.
