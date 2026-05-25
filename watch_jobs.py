import json
import os
import re
import hashlib
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

PAGE_URL = "https://www.newgrad-jobs.com/entry-level-jobs/google"
SEEN_FILE = Path("seen_jobs.json")

DEFAULT_KEYWORDS = (
    r"Software|Engineer|AI|ML|Machine Learning|Data|Backend|Full-Stack|"
    r"SWE|Residency|University Graduate|New Grad"
)

KEYWORDS = os.getenv("JOB_KEYWORDS") or DEFAULT_KEYWORDS
NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")


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
    normalized = title.lower().strip()
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


def clean_title(title):
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"^\d+\s+", "", title).strip()
    title = title.replace("Apply", "").strip()
    return title


def extract_titles():
    titles = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(PAGE_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(5000)

        selectors = [
            ".dataLeftPaneInnerContent",
            ".hover-container .primary .truncate",
            ".primary .truncate",
            "[aria-label*='Position Title']",
        ]

        for frame in page.frames:
            for selector in selectors:
                try:
                    texts = frame.locator(selector).all_inner_texts()
                    for text in texts:
                        for line in text.splitlines():
                            title = clean_title(line)
                            if (
                                len(title) >= 8
                                and not title.lower().startswith("position title")
                                and not title.lower() in {"click here", "salary", "work model"}
                                and not title.isdigit()
                            ):
                                titles.append(title)
                except Exception:
                    continue

        browser.close()

    unique = []
    seen_lower = set()

    for title in titles:
        key = title.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            unique.append(title)

    return unique


def main():
    seen = load_seen()
    first_run = not bool(seen)

    titles = extract_titles()

    if not titles:
        send_notification(
            f"Watcher ran but found no job titles.\n\nCheck manually:\n{PAGE_URL}",
            title="Job Watcher Warning",
        )
        return

    keyword_pattern = re.compile(KEYWORDS, re.IGNORECASE)

    new_matches = []

    for title in titles:
        job_id = make_job_id(title)

        if job_id not in seen:
            seen[job_id] = {
                "title": title,
                "source": PAGE_URL,
            }

            if not first_run and keyword_pattern.search(title):
                new_matches.append(title)

    save_seen(seen)

    if first_run:
        send_notification(
            f"Watcher initialized with {len(titles)} current Google jobs.\n\n"
            f"No alerts will be sent until new matching jobs appear.\n\n{PAGE_URL}",
            title="Job Watcher Started",
        )
        return

    if new_matches:
        message = "New Google job match found:\n\n"
        for title in new_matches[:10]:
            message += f"• {title}\n"

        message += f"\nCheck/apply here:\n{PAGE_URL}"
        send_notification(message)
    else:
        print("No new matching jobs.")


if __name__ == "__main__":
    main()
