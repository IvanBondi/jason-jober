import feedparser
import requests
import sqlite3
import sys
import time
import os
import re
from datetime import datetime

# ============================================================
# КОНФІГУРАЦІЯ JASON JOBER
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "YOUR_CHAT_ID")
CHECK_INTERVAL = 300  # перевіряти кожні 5 хвилин
BATCH_SIZE = 5
CONFIRM_TIMEOUT = 1800  # 30 min to press "Next" before skipping

# Specific phrases matched against job TITLE only (avoids noise from descriptions)
KEYWORDS = [
    # Writing
    "content writer", "content writing",
    "blog writer", "blog writing", "blog post",
    "article writer", "article writing",
    "copywriter", "copywriting",
    "freelance writer", "freelance writing",
    "technical writer", "technical writing",
    "seo writer", "seo writing", "seo content",
    "ghostwriter", "ghost writer",
    "proofreader", "proofreading",
    "rewriting", "rewrite",
    # Data work
    "data entry", "data collection",
    "web research", "web scraping",
    # Spreadsheets
    "excel specialist", "google sheets specialist", "spreadsheet",
    # Python / automation
    "python automation", "python script", "automation script",
    # Translation
    "translator", "translation specialist", "localization specialist",
    "localization manager",
]

MIN_BUDGET = 20

# RSS фіди — Upwork removed RSS (410), Freelancer RSS returns HTML
RSS_FEEDS = {
    "RemoteOK": [
        "https://remoteok.com/remote-jobs.rss",
        "https://remoteok.com/remote-content-jobs.rss",
        "https://remoteok.com/remote-python-jobs.rss",
    ],
    "WeWorkRemotely": [
        "https://weworkremotely.com/remote-jobs.rss",
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    ],
    "Jobicy": [
        "https://jobicy.com/?feed=job_feed",
    ],
    "Remotive": [
        "https://remotive.com/remote-jobs/feed",
    ],
}

# ============================================================
# БАЗА ДАНИХ
# ============================================================
def init_db():
    conn = sqlite3.connect("jason_jobs.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            id TEXT PRIMARY KEY,
            seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def is_seen(job_id):
    conn = sqlite3.connect("jason_jobs.db")
    c = conn.cursor()
    c.execute("SELECT id FROM seen_jobs WHERE id = ?", (job_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_seen(job_id):
    conn = sqlite3.connect("jason_jobs.db")
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO seen_jobs (id) VALUES (?)", (job_id,))
    conn.commit()
    conn.close()

def mark_seen_bulk(job_ids):
    conn = sqlite3.connect("jason_jobs.db")
    c = conn.cursor()
    c.executemany("INSERT OR IGNORE INTO seen_jobs (id) VALUES (?)", [(jid,) for jid in job_ids])
    conn.commit()
    conn.close()

# ============================================================
# ФІЛЬТРАЦІЯ — title only
# ============================================================
def extract_budget(text):
    patterns = [
        r'\$(\d+(?:,\d+)?(?:\.\d+)?)',
        r'(\d+(?:,\d+)?)\s*(?:USD|usd|dollars?)',
        r'Budget[:\s]+\$?(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return float(match.group(1).replace(',', ''))
    return None

def matches_keywords(title):
    title_lower = (title or "").lower()
    for keyword in KEYWORDS:
        if keyword.lower() in title_lower:
            return keyword
    return None

def is_relevant(entry, source):
    title = entry.get("title", "")
    matched_keyword = matches_keywords(title)  # title only — avoids false positives
    if not matched_keyword:
        return False, None, None

    budget = extract_budget(entry.get("summary", ""))
    if budget and budget < MIN_BUDGET:
        return False, None, None

    return True, matched_keyword, budget

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def send_telegram_with_button(message, button_label, callback_data):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[{"text": button_label, "callback_data": callback_data}]]
        },
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def answer_callback(callback_id):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": "Loading next batch..."},
            timeout=5,
        )
    except Exception:
        pass

def wait_for_next(callback_data, timeout_seconds=CONFIRM_TIMEOUT):
    """Long-poll for the inline button press. Returns True if user pressed Next."""
    deadline = time.time() + timeout_seconds
    offset = None

    while time.time() < deadline:
        poll_secs = min(60, int(deadline - time.time()))
        if poll_secs <= 0:
            break
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"timeout": poll_secs, "allowed_updates": ["callback_query"], "offset": offset},
                timeout=poll_secs + 5,
            )
            if not r.ok:
                continue
            for update in r.json().get("result", []):
                offset = update["update_id"] + 1
                cb = update.get("callback_query", {})
                if cb.get("data") == callback_data:
                    answer_callback(cb["id"])
                    return True
        except Exception as e:
            print(f"getUpdates error: {e}")
            time.sleep(5)

    return False

def format_job_message(entry, source, keyword, budget):
    title = entry.get("title", "No title")
    link = entry.get("link", "")
    summary = entry.get("summary", "")

    clean_summary = re.sub(r'<[^>]+>', '', summary)
    clean_summary = clean_summary[:300] + "..." if len(clean_summary) > 300 else clean_summary

    budget_str = f"💰 <b>Budget:</b> ${budget:.0f}" if budget else "💰 <b>Budget:</b> Not specified"

    return (
        f"🔔 <b>NEW JOB — {source}</b>\n\n"
        f"📌 <b>{title}</b>\n\n"
        f"🏷 <b>Matched:</b> {keyword}\n"
        f"{budget_str}\n\n"
        f"📝 {clean_summary}\n\n"
        f"🔗 <a href=\"{link}\">Apply Now</a>\n"
        f"⏰ {datetime.now().strftime('%H:%M • %d %b %Y')}"
    )

# ============================================================
# ОСНОВНИЙ МОНІТОРИНГ
# ============================================================
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; feedparser/6.0)"}

def collect_relevant_jobs():
    """Fetch all feeds and return list of (entry, source, keyword, budget, job_id)."""
    pending = []
    for source, feeds in RSS_FEEDS.items():
        for feed_url in feeds:
            try:
                response = requests.get(feed_url, headers=FETCH_HEADERS, timeout=20, allow_redirects=True)
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                for entry in feed.entries:
                    job_id = entry.get("id") or entry.get("link", "")
                    if not job_id or is_seen(job_id):
                        continue
                    relevant, keyword, budget = is_relevant(entry, source)
                    if relevant:
                        pending.append((entry, source, keyword, budget, job_id))
                    else:
                        mark_seen(job_id)
            except Exception as e:
                print(f"Error parsing {feed_url}: {e}")
    return pending

def send_in_batches(pending):
    """Send jobs 5 at a time, ask for confirmation between batches."""
    sent = 0
    total = len(pending)

    for batch_start in range(0, total, BATCH_SIZE):
        batch = pending[batch_start:batch_start + BATCH_SIZE]

        for entry, source, keyword, budget, job_id in batch:
            message = format_job_message(entry, source, keyword, budget)
            if send_telegram(message):
                mark_seen(job_id)
                sent += 1
                print(f"✅ Sent: {entry.get('title', '')[:50]}")
                time.sleep(0.5)

        remaining = total - (batch_start + BATCH_SIZE)
        if remaining <= 0:
            break

        batch_num = batch_start // BATCH_SIZE + 1
        callback_data = f"next_{batch_num}_{int(time.time())}"
        send_telegram_with_button(
            f"📦 <b>Batch {batch_num} done</b> — {len(batch)} jobs sent.\n"
            f"🔜 <b>{min(remaining, BATCH_SIZE)} more</b> ready (of {remaining} remaining).\n\n"
            f"Press the button to load next batch, or ignore to skip.",
            f"Next {min(remaining, BATCH_SIZE)} jobs →",
            callback_data,
        )
        print(f"Waiting for confirmation (batch {batch_num}, {remaining} remaining)...")
        if not wait_for_next(callback_data):
            print("No confirmation — skipping remaining jobs")
            mark_seen_bulk([job_id for _, _, _, _, job_id in pending[batch_start + BATCH_SIZE:]])
            break

    return sent

def check_feeds():
    pending = collect_relevant_jobs()
    if not pending:
        return 0
    print(f"Found {len(pending)} relevant jobs, sending in batches of {BATCH_SIZE}")
    return send_in_batches(pending)

def main():
    if "--test" in sys.argv:
        print("Sending test Telegram message...")
        ok = send_telegram(
            "✅ <b>Jason Jober — test message</b>\n\n"
            "Bot is configured correctly and can send messages.\n"
            f"⏰ {datetime.now().strftime('%H:%M • %d %b %Y')}"
        )
        print("Sent OK" if ok else "FAILED — check TELEGRAM_TOKEN and CHAT_ID")
        return

    print("🚀 Jason Jober is starting...")
    init_db()

    send_telegram(
        "🤖 <b>Jason Jober is online!</b>\n\n"
        "Monitoring jobs on RemoteOK, WeWorkRemotely, Jobicy & Remotive...\n\n"
        "🔍 Categories: Writing, Data Entry, Spreadsheets, Python, Translation\n"
        "📦 Jobs sent in batches of 5 — press Next to load more."
    )
    print("✅ Jason Jober is running!")

    while True:
        print(f"\n⏰ Checking feeds... {datetime.now().strftime('%H:%M:%S')}")
        new_jobs = check_feeds()
        print(f"📊 Found {new_jobs} new relevant jobs")
        print(f"💤 Sleeping for {CHECK_INTERVAL // 60} minutes...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
