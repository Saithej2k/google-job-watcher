import hashlib
import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

PAGE_URL = "https://www.newgrad-jobs.com/entry-level-jobs/google"
SEEN_FILE = Path("seen_jobs.json")

NTFY_TOPIC = os.environ["NTFY_TOPIC"]
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")

# Only alert on these kinds of NewGradJobs titles
MATCH_REGEX = os.getenv(
    "MATCH_REGEX",
    r"(Software Engineer\s*(I|II|III|1|2|3)\b|"
    r"Software Engineer III|Software Engineer II|Software Engineer I|"
    r"Full[- ]Stack Software Engineer|"
    r"\bSWE\b|"
    r"New Grad|New Graduate|University Graduate|Early Career|"
    r"2026.*(Graduate|Residency|Software))",
)

# Avoid obvious non-target senior/management roles unless they are Software Engineer III
EXCLUDE_REGEX = os.getenv(
    "EXCLUDE_REGEX",
    r"(Staff|Principal|Director|Manager|Technical Program Manager|Learning Design|AML|Data Center Technician)",
)


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


def send_notification(message, title="NewGradJobs Alert"):
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


def clean_title(text):
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^\d+\s+", "", text).strip()
    return text


def is_possible_title(text):
    if not text:
        return False

    text = clean_title(text)
    lower = text.lower()

    bad = {
        "position title",
        "click here",
        "salary",
        "work model",
        "apply",
        "on site",
        "remote",
        "hybrid",
        "hide fields",
        "filter",
        "group",
        "sort",
    }

    if lower in bad:
        return False

    if text.isdigit():
        return False

    if len(text) < 8 or len(text) > 180:
        return False

    # Must look relevant to the roles you care about
    if not re.search(MATCH_REGEX, text, re.IGNORECASE):
        return False

    # Exclude obvious non-targets, but keep Software Engineer III
    if re.search(EXCLUDE_REGEX, text, re.IGNORECASE) and not re.search(
        r"Software Engineer\s*(III|3)\b", text, re.IGNORECASE
    ):
        return False

    return True


def extract_titles():
    titles = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1600, "height": 1400},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        page = context.new_page()

        try:
            page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightTimeoutError:
            print("Page load timed out, continuing anyway...")

        # Scroll down so the embedded NewGradJobs table actually renders.
        for _ in range(8):
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(2500)

        # Wait up to 90 sec total for the embedded table text to appear in any frame.
        found_table = False
        for _ in range(18):
            all_text = ""
            for frame in page.frames:
                try:
                    all_text += "\n" + frame.locator("body").inner_text(timeout=3000)
                except Exception:
                    pass

            if "Position Title" in all_text or "Software Engineer" in all_text:
                found_table = True
                break

            page.wait_for_timeout(5000)

        print(f"Found table text: {found_table}")
        print(f"Frame count: {len(page.frames)}")

        selectors = [
            ".dataLeftPaneInnerContent",
            ".hover-container .primary .truncate",
            ".primary .truncate",
            "[aria-label*='Position Title']",
            "body",
        ]

        for frame in page.frames:
            for selector in selectors:
                try:
                    texts = frame.locator(selector).all_inner_texts(timeout=5000)
                    for block in texts:
                        for line in block.splitlines():
                            title = clean_title(line)
                            if is_possible_title(title):
                                titles.append(title)
                except Exception:
                    continue

        # Debug files if extraction fails
        if not titles:
            try:
                page.screenshot(path="debug-newgradjobs.png", full_page=True)
                Path("debug-newgradjobs.html").write_text(page.content(), encoding="utf-8")
            except Exception as e:
                print(f"Could not write debug files: {e}")

        browser.close()

    unique = []
    seen_lower = set()

    for title in titles:
        key = title.lower()
        if key not in seen_lower:
            seen_lower.add(key)
            unique.append(title)

    print(f"Relevant NewGradJobs titles found: {len(unique)}")
    for title in unique:
        print(f"- {title}")

    return unique


def main():
    seen = load_seen()
    first_run = not bool(seen)

    titles = extract_titles()

    # Always save file so git commit step does not fail
    if not titles:
        save_seen(seen)
        send_notification(
            f"Watcher ran but found 0 matching NewGradJobs titles.\n\n"
            f"Check GitHub Actions debug artifact/screenshot.\n\n{PAGE_URL}",
            title="NewGradJobs Watcher Warning",
        )
        return

    new_titles = []

    for title in titles:
        job_id = make_job_id(title)

        if job_id not in seen:
            seen[job_id] = {
                "title": title,
                "source": PAGE_URL,
            }

            if not first_run:
                new_titles.append(title)

    save_seen(seen)

    if first_run:
        send_notification(
            f"NewGradJobs watcher started.\n\n"
            f"Initialized with {len(titles)} matching Google software/new-grad titles.\n\n"
            f"No alerts until a new matching title appears.\n\n{PAGE_URL}",
            title="NewGradJobs Watcher Started",
        )
        return

    if new_titles:
        message = "New Google role on NewGradJobs:\n\n"

        for title in new_titles[:10]:
            message += f"• {title}\n"

        message += f"\nCheck/apply here:\n{PAGE_URL}"
        send_notification(message)
    else:
        print("No new matching NewGradJobs roles.")


if __name__ == "__main__":
    main()