import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup

SEEN_FILE = Path("seen_jobs.json")

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

DEFAULT_KEYWORDS = (
    r"Software|Engineer|AI|ML|Machine Learning|Data|Backend|Full[- ]?Stack|"
    r"SWE|Residency|University Graduate|New Grad|Early Career"
)

DEFAULT_EXCLUDE = r"Senior|Staff|Principal|Manager|Director|Lead"

KEYWORDS = os.getenv("JOB_KEYWORDS") or DEFAULT_KEYWORDS
EXCLUDE_KEYWORDS = os.getenv("EXCLUDE_KEYWORDS") or DEFAULT_EXCLUDE

BASE_URL = "https://www.google.com/about/careers/applications/jobs/results/"

SEARCH_TERMS = [
    "software engineer",
    "machine learning",
    "AI ML",
    "backend",
    "full stack",
    "university graduate",
    "new grad",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def load_seen():
    if not SEEN_FILE.exists():
        return {}
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2, sort_keys=True))


def make_job_id(title):
    normalized = re.sub(r"\s+", " ", title.lower().strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def send_notification(message, title="Google Job Alert"):
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers = {
        "Title": title,
        "Priority": "high",
        "Tags": "briefcase,rotating_light",
    }
    response = requests.post(
        url,
        data=message.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()


def google_careers_url(query):
    params = {
        "company": "Google",
        "employment_type": "FULL_TIME",
        "hl": "en_US",
        "jlo": "en_US",
        "location": "United States",
        "q": query,
        "sort_by": "date",
    }
    return BASE_URL + "?" + urlencode(params)


def clean_line(line):
    line = re.sub(r"\s+", " ", line).strip()
    line = line.strip("•").strip()
    return line


def looks_like_job_title(line):
    if len(line) < 8 or len(line) > 180:
        return False

    bad_phrases = [
        "jobs search results",
        "jobs matched",
        "showing",
        "follow life at google",
        "about us",
        "related information",
        "equal opportunity",
        "privacy",
        "terms",
        "google apps",
        "main menu",
        "search jobs",
        "back to jobs",
        "job not found",
        "apply",
        "help",
    ]

    lower = line.lower()

    if any(phrase in lower for phrase in bad_phrases):
        return False

    if re.search(EXCLUDE_KEYWORDS, line, re.IGNORECASE):
        return False

    return bool(re.search(KEYWORDS, line, re.IGNORECASE))


def extract_jobs_from_google_careers(query):
    url = google_careers_url(query)
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text("\n")

    if "Jobs search results" in text:
        text = text.split("Jobs search results", 1)[1]

    if "Showing 1 to" in text:
        text = text.split("Showing 1 to", 1)[0]

    jobs = []

    for raw_line in text.splitlines():
        line = clean_line(raw_line)

        if looks_like_job_title(line):
            jobs.append(
                {
                    "title": line,
                    "source_url": url,
                    "query": query,
                }
            )

    return jobs


def extract_all_jobs():
    all_jobs = []
    seen_titles = set()

    for query in SEARCH_TERMS:
        try:
            jobs = extract_jobs_from_google_careers(query)
            print(f"{query}: found {len(jobs)} possible matches")

            for job in jobs:
                key = job["title"].lower()
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_jobs.append(job)

        except Exception as e:
            print(f"Failed query '{query}': {e}")

    return all_jobs


def main():
    seen = load_seen()
    first_run = not bool(seen)

    jobs = extract_all_jobs()

    # Always create seen_jobs.json so GitHub commit step does not fail.
    if not jobs:
        save_seen(seen)
        send_notification(
            "Watcher ran but found 0 Google Careers matches. Check workflow logs.",
            title="Job Watcher Warning",
        )
        return

    new_jobs = []

    for job in jobs:
        job_id = make_job_id(job["title"])

        if job_id not in seen:
            seen[job_id] = job

            if not first_run:
                new_jobs.append(job)

    save_seen(seen)

    print(f"Total matching jobs found: {len(jobs)}")
    print(f"New jobs found: {len(new_jobs)}")

    if first_run:
        send_notification(
            f"Google job watcher started.\n\n"
            f"Initialized with {len(jobs)} current matching jobs.\n\n"
            f"No new-job alerts until something new appears.",
            title="Job Watcher Started",
        )
        return

    if new_jobs:
        message = "New Google job match found:\n\n"

        for job in new_jobs[:10]:
            message += f"• {job['title']}\n"

        message += f"\nSearch/apply:\n{BASE_URL}"
        send_notification(message)
    else:
        print("No new matching jobs.")


if __name__ == "__main__":
    main()
