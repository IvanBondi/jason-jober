import feedparser
import requests
import sqlite3
import sys
import time
import os
import re
import json
from datetime import datetime

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as gapi_build
    SHEETS_AVAILABLE = True
except ImportError:
    SHEETS_AVAILABLE = False

# ============================================================
# CONFIGURATION
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID", "YOUR_CHAT_ID")
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "1HO22ANItDntqzK4oM2bQAQHsAk0qUITxP-ND1wJqhic")
_CREDENTIALS_FILE = os.path.join(os.path.dirname(__file__), "lucky-wonder-494514-b0-a80366374894.json")
CHECK_INTERVAL = 300  # 5 minutes
BATCH_SIZE = 5
CONFIRM_TIMEOUT = 1800  # 30 min to press "Next" before skipping

# Specific phrases matched against job TITLE only (avoids noise from descriptions)
KEYWORDS = [
    "writer", "writing", "content",
    "data entry",
    "research",
    "translation",
    "excel",
    "python",
    "scraping",
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
# GLOBAL TELEGRAM STATE
# ============================================================
tg_offset = None

# message_id -> {title, source, budget, link, keyword}
# Populated when a job is sent; consumed when user replies "take"
pending_takes = {}

# ============================================================
# DATABASE
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
# GOOGLE SHEETS
# ============================================================
SHEET_HEADERS = ["Date", "Platform", "Job Title", "Budget", "Status", "Link"]

def _load_credentials():
    """Load service account credentials from file if present, else from env var."""
    if os.path.exists(_CREDENTIALS_FILE):
        with open(_CREDENTIALS_FILE) as f:
            return json.load(f)
    if GOOGLE_CREDENTIALS_JSON:
        return json.loads(GOOGLE_CREDENTIALS_JSON)
    raise RuntimeError("No Google credentials found (set GOOGLE_CREDENTIALS_JSON or place the service account file in the project directory)")

def _sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        _load_credentials(),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return gapi_build("sheets", "v4", credentials=creds)

def _sheets_configured():
    return SHEETS_AVAILABLE and (os.path.exists(_CREDENTIALS_FILE) or GOOGLE_CREDENTIALS_JSON) and GOOGLE_SHEET_ID

def ensure_sheet_headers():
    if not _sheets_configured():
        return
    try:
        svc = _sheets_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Аркуш1!A1:F1",
        ).execute()
        if not result.get("values"):
            svc.spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range="Аркуш1!A1:F1",
                valueInputOption="USER_ENTERED",
                body={"values": [SHEET_HEADERS]},
            ).execute()
            print("✅ Sheet headers created")
    except Exception as e:
        print(f"Sheets header error: {e}")

def log_to_sheets(job_data):
    if not _sheets_configured():
        print("Google Sheets not configured")
        return False
    try:
        svc = _sheets_service()
        row = [
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            job_data["source"],
            job_data["title"],
            f"${job_data['budget']:.0f}" if job_data.get("budget") else "",
            "Applied",
            job_data["link"],
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Аркуш1!A:F",
            valueInputOption="USER_ENTERED",
            body={"values": [row]},
        ).execute()
        print(f"✅ Sheets logged: {job_data['title'][:60]}")
        return True
    except Exception as e:
        print(f"Sheets error: {e}")
        send_telegram(f"⚠️ Failed to log to Google Sheets: {e}")
        return False

# ============================================================
# FILTERING — title keyword match only, no exclusions
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
    matched_keyword = matches_keywords(title)
    if not matched_keyword:
        return False, None, None

    budget = extract_budget(summary)
    if budget and budget < MIN_BUDGET:
        return False, None, None

    return True, matched_keyword, budget

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    """Send a message. Returns message_id on success, None on failure."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.ok:
            return response.json().get("result", {}).get("message_id")
        return None
    except Exception as e:
        print(f"Telegram error: {e}")
        return None

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

# ============================================================
# UPDATE POLLING
# ============================================================
def get_updates(timeout=0):
    global tg_offset
    params = {
        "timeout": timeout,
        "allowed_updates": ["callback_query", "message"],
    }
    if tg_offset is not None:
        params["offset"] = tg_offset
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params,
            timeout=timeout + 5,
        )
        if not r.ok:
            return []
        updates = r.json().get("result", [])
        if updates:
            tg_offset = updates[-1]["update_id"] + 1
        return updates
    except Exception as e:
        print(f"getUpdates error: {e}")
        time.sleep(5)
        return []

def handle_take(msg):
    """Log the job a user replied to with 'take' into Google Sheets."""
    reply_to = msg.get("reply_to_message", {})
    reply_msg_id = reply_to.get("message_id")
    if not reply_msg_id or reply_msg_id not in pending_takes:
        send_telegram(
            "⚠️ Reply directly to a job message with <b>take</b> to log it to Google Sheets."
        )
        return
    job_data = pending_takes.pop(reply_msg_id)
    if log_to_sheets(job_data):
        budget_str = f"${job_data['budget']:.0f}" if job_data.get("budget") else "N/A"
        send_telegram(
            f"✅ <b>Logged to Google Sheets!</b>\n\n"
            f"📌 {job_data['title'][:80]}\n"
            f"🏢 {job_data['source']} • 💰 {budget_str}"
        )

def process_updates(updates):
    """Handle all updates. Returns list of callback_data strings seen."""
    callbacks_seen = []
    for update in updates:
        cb = update.get("callback_query", {})
        if cb:
            callbacks_seen.append(cb.get("data", ""))
            answer_callback(cb["id"])

        msg = update.get("message", {})
        if msg and msg.get("text", "").strip().lower() == "take":
            handle_take(msg)

    return callbacks_seen

def wait_for_next(callback_data, timeout_seconds=CONFIRM_TIMEOUT):
    """Long-poll for the inline button press, processing 'take' replies along the way."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        poll_secs = min(60, int(deadline - time.time()))
        if poll_secs <= 0:
            break
        updates = get_updates(timeout=poll_secs)
        if callback_data in process_updates(updates):
            return True
    return False

def sleep_with_polling(seconds):
    """Sleep for `seconds` while still processing incoming 'take' replies."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        remaining = int(deadline - time.time())
        poll_secs = min(60, remaining)
        if poll_secs <= 0:
            break
        updates = get_updates(timeout=poll_secs)
        process_updates(updates)

# ============================================================
# JOB FORMATTING & BATCHING
# ============================================================
FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; feedparser/6.0)"}

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
        f"⏰ {datetime.now().strftime('%H:%M • %d %b %Y')}\n\n"
        f"<i>Reply <b>take</b> to log this job to Google Sheets</i>"
    )

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
            msg_id = send_telegram(message)
            if msg_id:
                pending_takes[msg_id] = {
                    "title": entry.get("title", ""),
                    "source": source,
                    "budget": budget,
                    "link": entry.get("link", ""),
                    "keyword": keyword,
                }
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

# ============================================================
# MAIN
# ============================================================
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
    ensure_sheet_headers()

    sheets_status = "✅ Google Sheets connected" if _sheets_configured() else "⚠️ Google Sheets not configured"

    send_telegram(
        "🤖 <b>Jason Jober is online!</b>\n\n"
        "Monitoring jobs on RemoteOK, WeWorkRemotely, Jobicy & Remotive...\n\n"
        "🔍 Categories: Writing, Data Entry, Spreadsheets, Python, Translation\n"
        "📦 Jobs sent in batches of 5 — press Next to load more.\n"
        f"📊 {sheets_status}\n\n"
        "<i>Reply <b>take</b> to any job to log it to Google Sheets</i>"
    )
    print("✅ Jason Jober is running!")

    while True:
        print(f"\n⏰ Checking feeds... {datetime.now().strftime('%H:%M:%S')}")
        new_jobs = check_feeds()
        print(f"📊 Found {new_jobs} new relevant jobs")
        print(f"💤 Sleeping for {CHECK_INTERVAL // 60} minutes...")
        sleep_with_polling(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
