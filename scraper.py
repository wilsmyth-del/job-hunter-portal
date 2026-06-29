#!/usr/bin/env python3
"""
Portal scraper — runs daily via PythonAnywhere scheduled task.
Fetches LinkedIn jobs for each user scheduled today, filters against their
personal seen-job history, and emails new listings.
"""

import logging
import os
import re
import smtplib
import time
import urllib.parse
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

from db import (
    init_db,
    get_queries_for_user,
    get_users_for_today,
    is_seen_for_user,
    mark_seen_for_user,
)

load_dotenv(Path(__file__).parent / ".env")

GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")

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


# ── Filtering ──────────────────────────────────────────────────────────────────

def is_blocked(job: dict) -> bool:
    url = job.get("url", "").lower()
    return any(domain in url for domain in BLOCKED_DOMAINS)


# ── Email ──────────────────────────────────────────────────────────────────────

def send_digest(user, jobs: list, is_test: bool = False) -> None:
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        log.warning("Gmail not configured — skipping email")
        return

    now = datetime.now().strftime("%B %d, %Y")
    count = len(jobs)
    prefix = "Test: " if is_test else ""
    subject = f"{prefix}Job Digest — {now} ({count} listing{'s' if count != 1 else ''})"

    # Plain text
    lines = [f"Job Digest — {now}", f"{count} new listing{'s' if count != 1 else ''}", ""]
    for job in jobs:
        company = f" @ {job['company']}" if job.get("company") else ""
        loc = f" · {job['location']}" if job.get("location") else ""
        lines += [f"  {job['role']}{company}{loc}", f"  {job['url']}", ""]
    lines += [
        "—",
        "To update your searches or schedule, log in at portal-wilsmyth.pythonanywhere.com",
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
    today_users = get_users_for_today()
    log.info(f"Scheduled today: {len(today_users)} user(s)")

    for user in today_users:
        queries = get_queries_for_user(user["id"])
        location = user["location"]

        if not queries:
            log.info(f"No queries for {user['email']} — skipping")
            continue

        log.info(f"{user['email']}: {len(queries)} queries, location={location!r}")

        all_jobs: list[dict] = []
        seen_urls: set[str] = set()

        for query in queries[:6]:
            for job in fetch_linkedin(query, location):
                if job["url"] in seen_urls or is_blocked(job):
                    continue
                seen_urls.add(job["url"])
                all_jobs.append(job)
            time.sleep(0.5)

        new_jobs = [j for j in all_jobs if not is_seen_for_user(user["id"], j["id"])]
        log.info(f"{user['email']}: {len(all_jobs)} fetched, {len(new_jobs)} new")

        if new_jobs:
            for job in new_jobs:
                mark_seen_for_user(user["id"], job["id"])
            send_digest(user, new_jobs)
        else:
            log.info(f"Nothing new for {user['email']}")

        time.sleep(2)


if __name__ == "__main__":
    main()
