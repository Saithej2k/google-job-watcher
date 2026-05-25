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

TOP_N = int(os.getenv("TOP_N", "5"))

# Set to true so your phone gets the current top 5 every 30 minutes.
ALWAYS_SEND_TOP5 = os.getenv("ALWAYS_SEND_TOP5", "true").lower() == "true"

TARGET_REGEX = (
    r"Software Engineer\s*(I|II|III|1|2|3)\b|"
    r"Software Engineer III|Software Engineer II|Software Engineer I|"
    r"Full[- ]Stack Software Engineer|"
    r"\bSWE\b|"
    r"New Grad|New Graduate|University Graduate|Early Career|"
    r"2026.*(Graduate|Software|SWE|Residency)"
)

BAD_TEXT = {
    "position title",
    "salary",
    "work model",
    "locations",
    "company",
    "application link",
    "click here",
    "apply",
    "remote",
    "hybrid",
    "onsite",
    "on site",
    "filter",
    "sort",
    "group",
    "hide fields",
    "views",
    "share",
    "copy link",
    "download csv",
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


def send_notification(message, title="NewGradJobs Google Alert"):
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


def clean_text(text):
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^\d+\s+", "", text).strip()
    return text


def looks_like_job_title(text):
    text = clean_text(text)
    lower = text.lower()

    if not text:
        return False

    if lower in BAD_TEXT:
        return False

    if text.isdigit():
        return False

    if len(text) < 8 or len(text) > 140:
        return False

    # Keep common job-title-looking rows.
    title_words = [
        "engineer",
        "developer",
        "software",
        "full-stack",
        "full stack",
        "machine learning",
        "ai",
        "data",
        "analyst",
        "scientist",
        "designer",
        "specialist",
        "residency",
        "graduate",
        "new grad",
    ]

    return any(word in lower for word in title_words)


def is_target_role(title):
    return bool(re.search(TARGET_REGEX, title, re.IGNORECASE))


def extract_titles():
    all_candidates = []

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

        # Scroll down so the embedded NewGradJobs/Airtable table renders.
        for _ in range(10):
            page.mouse.wheel(0, 900)
            page.wait_for_timeout(2500)

        # Wait for table content.
        found_table = False
        for _ in range(18):
            combined_text = ""

            for frame in page.frames:
                try:
                    combined_text += "\n" + frame.locator("body").inner_text(timeout=3000)
                except Exception:
                    pass

            if "Position Title" in combined_text or "Software Engineer" in combined_text:
                found_table = True
                break

            page.wait_for_timeout(5000)

        print(f"Found table text: {found_table}")
        print(f"Frame count: {len(page.frames)}")

        # Method 1: get small visible elements. This works better than grabbing the whole body.
        for frame in page.frames:
            try:
                texts = frame.locator(
                    "div, span, a, p, td, th, button"
                ).evaluate_all(
                    """
                    els => els
                      .map(e => (e.innerText || e.textContent || '').trim())
                      .filter(t => t && t.length >= 8 && t.length <= 140)
                    """
                )

                for text in texts:
                    text = clean_text(text)
                    if looks_like_job_title(text):
                        all_candidates.append(text)

            except Exception:
                pass

        # Method 2: fallback using body lines.
        for frame in page.frames:
            try:
                body_text = frame.locator("body").inner_text(timeout=5000)
                for line in body_text.splitlines():
                    line = clean_text(line)
                    if looks_like_job_title(line):
                        all_candidates.append(line)
            except Exception:
                pass

        # Debug files if extraction fails.
        if not all_candidates:
            try:
                page.screenshot(path="debug-newgradjobs.png", full_page=True)
                Path("debug-newgradjobs.html").write_text(
                    page.content(),
                    encoding="utf-8",
                )
            except Exception as e:
                print(f"Could not write debug files: {e}")

        browser.close()

    unique = []
    seen_lower = set()

    for title in all_candidates:
        key = title.lower()

        # Remove obvious duplicate/truncated junk.
        if key in seen_lower:
            continue

        if "position title" in key:
            continue

        seen_lower.add(key)
        unique.append(title)

    print(f"Total job-like titles found: {len(unique)}")

    for title in unique[:20]:
        marker = "TARGET" if is_target_role(title) else "OTHER"
        print(f"- [{marker}] {title}")

    return unique


def build_top5_message(top_titles, new_target_titles):
    message = "Top 5 recent Google jobs from NewGradJobs:\n\n"

    for idx, title in enumerate(top_titles, start=1):
        marker = " 🎯" if is_target_role(title) else ""
        message += f"{idx}. {title}{marker}\n"

    if new_target_titles:
        message += "\nNew target matches:\n"
        for title in new_target_titles[:10]:
            message += f"• {title}\n"

    message += f"\nCheck page:\n{PAGE_URL}"
    return message


def main():
    seen = load_seen()
    titles = extract_titles()

    if not titles:
        save_seen(seen)
        send_notification(
            f"Watcher ran but found 0 job titles.\n\n"
            f"Download the debug artifact from GitHub Actions and inspect the screenshot/html.\n\n"
            f"{PAGE_URL}",
            title="NewGradJobs Watcher Warning",
        )
        return

    top_titles = titles[:TOP_N]
    new_target_titles = []

    for title in titles:
        job_id = make_job_id(title)

        if job_id not in seen:
            seen[job_id] = {
                "title": title,
                "source": PAGE_URL,
            }

            if is_target_role(title):
                new_target_titles.append(title)

    save_seen(seen)

    # This sends the top 5 every run because ALWAYS_SEND_TOP5=true.
    if ALWAYS_SEND_TOP5:
        send_notification(
            build_top5_message(top_titles, new_target_titles),
            title="NewGradJobs Top 5 Google Jobs",
        )
        return

    # If you later set ALWAYS_SEND_TOP5=false, it only alerts on new target roles.
    if new_target_titles:
        message = "New target Google role on NewGradJobs:\n\n"

        for title in new_target_titles[:10]:
            message += f"• {title}\n"

        message += f"\nCheck page:\n{PAGE_URL}"

        send_notification(message)
    else:
        print("No new target roles.")


if __name__ == "__main__":
    main()