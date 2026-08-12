#!/usr/bin/env python3
"""
Portal scraper — runs hourly via PythonAnywhere scheduled task.
Each tick, checks which active users' delivery_hour matches the current
America/Vancouver local hour (DST-correct via zoneinfo) and haven't already
run today, fetches LinkedIn jobs for them, filters against their personal
seen-job history, and emails new listings.
"""

import json
import logging
import os
import re
import smtplib
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

from db import (
    init_db,
    get_queries_for_user,
    get_users_for_run,
    mark_run_today,
    is_seen_for_user,
    mark_seen_for_user,
    PORTAL_TZ,
)
from tokens import unsubscribe_token

load_dotenv(Path(__file__).parent / ".env")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")

PORTAL_BASE_URL = "https://portal-wilsmyth.pythonanywhere.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-CA,en;q=0.9",
}

BLOCKED_DOMAINS = {
    "bebee.com", "rapidojob.com", "jooble.org", "talent.com", "neuvoo.com",
    "jobrapido.com", "careerjet.ca", "careerjet.com", "simplyhired.com",
    "simplyhired.ca", "adzuna.com", "adzuna.ca", "jobleads.com",
    "learn4good.com", "recruit.net", "jobomas.com",
}

# A domain on the blocklist is only junk when it turns up as a *third-party*
# redirect inside someone else's results. Adzuna was blocklisted because it
# appeared that way inside JSearch output — but going direct to Adzuna's own
# API returns first-party inventory whose redirect_url is adzuna.ca by design.
# Without this, every Adzuna result would be silently discarded by is_blocked.
SOURCE_OWN_DOMAINS = {
    "LinkedIn": {"linkedin.com"},
    "Adzuna": {"adzuna.com", "adzuna.ca"},
}

# ── Adzuna ─────────────────────────────────────────────────────────────────────

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")
ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs/ca/search/1"
# The 2026-07-07→21 pilot saw ~19% of calls return a transient 503 from
# Adzuna's own infrastructure (an HTML error page, not JSON), and a single
# 2s retry did not reliably recover it. Hence three attempts with widening
# backoff, and a hard distinction between "failed" and "no results".
ADZUNA_RETRY_DELAYS = (2, 5)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ── LinkedIn fetch ─────────────────────────────────────────────────────────────

def fetch_linkedin(query: str, location: str = "") -> list[dict]:
    jobs = []
    seen_ids: set[str] = set()

    for start in (0, 10):
        params = urllib.parse.urlencode({
            "keywords": query,
            "location": location,
            "start": str(start),
        })
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/"
            f"seeMoreJobPostings/search?{params}"
        )
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                html = r.read().decode("utf-8", errors="ignore")

            cards = re.findall(
                r'data-entity-urn="urn:li:jobPosting:(\d+)".*?'
                r'base-card__full-link[^>]+href="([^"]+)".*?'
                r'base-search-card__title[^>]*>\s*(.*?)\s*</h3>.*?'
                r'hidden-nested-link[^>]*>\s*(.*?)\s*</a>.*?'
                r'job-search-card__location[^>]*>\s*(.*?)\s*</span>',
                html, re.DOTALL,
            )

            for job_id, link, title, company, loc_text in cards:
                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)
                title = re.sub(r"<[^>]+>", "", title).strip()
                company = re.sub(r"<[^>]+>", "", company).strip()
                jobs.append({
                    "id": f"linkedin_{job_id}",
                    "role": title,
                    "company": company,
                    "location": loc_text.strip(),
                    "url": link.split("?")[0],
                    "source": "LinkedIn",
                })

            if len(cards) < 10:
                break
            time.sleep(1)

        except Exception as e:
            log.warning(f"LinkedIn fetch failed for '{query}' (start={start}): {e}")
            break

    return jobs


def _adzuna_call(query: str, location: str):
    """One request. Returns parsed JSON, or None if the call failed."""
    params = urllib.parse.urlencode({
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "what": query,
        "where": location,
        "results_per_page": "20",
        # Adzuna defaults to relevance, which mixes year-old postings in with
        # today's. Confirmed during research: without this you get a 2026-07-05
        # listing sitting next to a 2025-02-16 one on the same page.
        "sort_by": "date",
        "content-type": "application/json",
    })
    req = urllib.request.Request(
        f"{ADZUNA_BASE}?{params}", headers={"User-Agent": "job-finder-portal/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.warning(f"Adzuna call failed for '{query}' @ '{location}': {e}")
        return None


def fetch_adzuna(query: str, location: str):
    """Returns a list of jobs, or **None** if every attempt failed.

    The None-vs-[] distinction is the whole point: at Adzuna's observed
    failure rate, collapsing a failed call into "no results" would silently
    tell users there were no jobs when we simply never found out.
    """
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return None  # not configured — not the same as "nothing found"

    data = _adzuna_call(query, location)
    for delay in ADZUNA_RETRY_DELAYS:
        if data is not None:
            break
        time.sleep(delay)
        data = _adzuna_call(query, location)

    if data is None:
        log.warning(f"Adzuna gave up after {len(ADZUNA_RETRY_DELAYS) + 1} attempts: "
                    f"'{query}' @ '{location}' — treating as unknown, not empty")
        return None

    jobs = []
    for r in data.get("results", []):
        job_id = r.get("id")
        url = r.get("redirect_url")
        if not job_id or not url:
            continue
        loc = r.get("location") or {}
        area = loc.get("area") or []
        jobs.append({
            "id": f"adzuna_{job_id}",
            "role": (r.get("title") or "").strip(),
            "company": ((r.get("company") or {}).get("display_name") or "").strip(),
            # display_name isn't always present; the area list is ordered
            # broad→specific, so the tail is the most useful fallback.
            "location": (loc.get("display_name") or ", ".join(area[-2:])).strip(),
            "url": url,
            "source": "Adzuna",
        })
    return jobs


# ── Filtering & cross-source dedup ─────────────────────────────────────────────

def is_blocked(job: dict) -> bool:
    url = job.get("url", "").lower()
    own = SOURCE_OWN_DOMAINS.get(job.get("source"), set())
    return any(domain in url for domain in BLOCKED_DOMAINS if domain not in own)


def dedupe_key(job: dict):
    """Normalised (role, company) — the same posting on two boards has
    different ids and different URLs, so neither of those can catch it."""
    def norm(s):
        return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return norm(job.get("role")), norm(job.get("company"))


def fetch_all_sources(query: str, location: str) -> list:
    """Both sources for one search, cross-source duplicates collapsed.

    LinkedIn runs first and wins ties, so results stay stable for users who
    are used to them. A dropped duplicate's id is carried on the survivor as
    `alias_ids`, so marking the survivor seen also suppresses the twin —
    otherwise the same job reappears as "new" the day one source drops it.
    """
    jobs, by_key = [], {}
    adzuna = fetch_adzuna(query, location)
    if adzuna is None:
        log.info(f"Adzuna unavailable for '{query}' @ '{location}' — LinkedIn only this run")

    for job in list(fetch_linkedin(query, location)) + list(adzuna or []):
        if is_blocked(job):
            continue
        key = dedupe_key(job)
        # An empty role or company makes the key useless — keep those rather
        # than collapsing unrelated postings together.
        if all(key) and key in by_key:
            by_key[key].setdefault("alias_ids", []).append(job["id"])
            continue
        job.setdefault("alias_ids", [])
        if all(key):
            by_key[key] = job
        jobs.append(job)
    return jobs


# ── Email ──────────────────────────────────────────────────────────────────────

def send_digest(user, jobs: list, is_test: bool = False) -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Gmail not configured — skipping email")
        return

    now = datetime.now().strftime("%B %d, %Y")
    count = len(jobs)
    prefix = "Test: " if is_test else ""
    subject = f"{prefix}Job Digest — {now} ({count} listing{'s' if count != 1 else ''})"
    unsub_url = f"{PORTAL_BASE_URL}/unsubscribe/{unsubscribe_token(user['id'])}"

    # Plain text
    lines = [f"Job Digest — {now}", f"{count} new listing{'s' if count != 1 else ''}", ""]
    for job in jobs:
        company = f" @ {job['company']}" if job.get("company") else ""
        loc = f" · {job['location']}" if job.get("location") else ""
        lines += [f"  {job['role']}{company}{loc}", f"  {job['url']}", ""]
    lines += [
        "—",
        "To update your searches or schedule, log in at portal-wilsmyth.pythonanywhere.com",
        f"To pause or delete your account: {unsub_url}",
    ]
    plain = "\n".join(lines)

    # HTML
    cards_html = ""
    for job in jobs:
        company = f"<span style='color:#a0aec0'> @ {job['company']}</span>" if job.get("company") else ""
        loc = f"<span style='color:#718096'> · {job['location']}</span>" if job.get("location") else ""
        cards_html += f"""
        <div style="background:#1a202c;border:1px solid #2d3748;border-radius:6px;
                    padding:1rem;margin-bottom:0.75rem;">
          <div style="font-size:1rem;font-weight:600;color:#e2e8f0;">
            {job['role']}{company}{loc}
          </div>
          <a href="{job['url']}"
             style="color:#5b6ef5;font-size:0.85rem;text-decoration:none;
                    display:inline-block;margin-top:0.4rem;">
            View on {job['source']} →
          </a>
        </div>"""

    html = f"""<!DOCTYPE html>
<html>
<body style="background:#171923;font-family:-apple-system,BlinkMacSystemFont,
             'Segoe UI',sans-serif;padding:2rem;color:#e2e8f0;max-width:600px;margin:0 auto;">
  <h2 style="color:#5b6ef5;margin-bottom:0.25rem;">Job Digest</h2>
  <p style="color:#718096;margin-top:0;margin-bottom:1.5rem;">
    {now} &nbsp;·&nbsp; {count} new listing{'s' if count != 1 else ''}
  </p>
  {cards_html}
  <p style="color:#4a5568;font-size:0.75rem;margin-top:2rem;border-top:1px solid #2d3748;padding-top:1rem;">
    You're receiving this because you set up job alerts at the
    <a href="https://portal-wilsmyth.pythonanywhere.com"
       style="color:#718096;">Job Finder Portal</a>.
    To update your searches or delivery schedule, log in anytime.
    <br>
    <a href="{unsub_url}" style="color:#718096;">Pause or delete your account</a>
  </p>
</body>
</html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Job Finder <{GMAIL_USER}>"
    msg["To"] = user["email"]
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            smtp.sendmail(GMAIL_USER, [user["email"]], msg.as_string())
        log.info(f"Digest sent to {user['email']} ({count} jobs)")
    except Exception as e:
        log.error(f"Email failed for {user['email']}: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    init_db()
    local_hour = datetime.now(PORTAL_TZ).hour
    today_users = get_users_for_run(local_hour)
    log.info(f"Local hour {local_hour}: {len(today_users)} user(s) due")

    for user in today_users:
        queries = get_queries_for_user(user["id"])

        if not queries:
            log.info(f"No queries for {user['email']} — skipping")
            mark_run_today(user["id"])
            continue

        log.info(f"{user['email']}: {len(queries)} queries")

        all_jobs: list[dict] = []
        seen_urls: set[str] = set()

        for q in queries[:6]:
            # Location is per-search now, not per-user. The profile location
            # is only a fallback for rows stored before the migration or saved
            # blank; it is no longer applied to every search.
            location = q["location"] or user["location"]
            for job in fetch_all_sources(q["query"], location):
                if job["url"] in seen_urls:
                    continue
                seen_urls.add(job["url"])
                all_jobs.append(job)
            time.sleep(0.5)

        new_jobs = [j for j in all_jobs if not is_seen_for_user(user["id"], j["id"])]
        by_source = Counter(j["source"] for j in all_jobs)
        log.info(f"{user['email']}: {len(all_jobs)} fetched ({dict(by_source)}), "
                 f"{len(new_jobs)} new")

        if new_jobs:
            for job in new_jobs:
                mark_seen_for_user(user["id"], job["id"])
                # Also suppress the same posting as seen from the other
                # source, so it doesn't resurface as "new" later.
                for alias in job.get("alias_ids", []):
                    mark_seen_for_user(user["id"], alias)
            send_digest(user, new_jobs)
        else:
            log.info(f"Nothing new for {user['email']}")

        mark_run_today(user["id"])
        time.sleep(2)


if __name__ == "__main__":
    main()
