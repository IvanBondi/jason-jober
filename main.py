import feedparser
import requests
import sqlite3
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

# Ключові слова для пошуку (що вміє Jason)
KEYWORDS = [
    "article writing", "blog post", "content writing",
    "copywriting", "data entry", "web research",
    "excel", "google sheets", "spreadsheet",
    "python script", "automation", "translation",
    "proofreading", "rewriting", "SEO writing"
]

# Мінімальний бюджет (де вказано)
MIN_BUDGET = 20

# RSS фіди
RSS_FEEDS = {
    "Upwork": [
        "https://www.upwork.com/ab/feed/jobs/rss?q=article+writing&sort=recency",
        "https://www.upwork.com/ab/feed/jobs/rss?q=data+entry&sort=recency",
        "https://www.upwork.com/ab/feed/jobs/rss?q=python+script&sort=recency",
        "https://www.upwork.com/ab/feed/jobs/rss?q=content+writing&sort=recency",
        "https://www.upwork.com/ab/feed/jobs/rss?q=translation&sort=recency",
    ],
    "Freelancer": [
        "https://www.freelancer.com/rss/jobs.xml",
    ]
}

# ============================================================
# БАЗА ДАНИХ (щоб не дублювати сповіщення)
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

# ============================================================
# ФІЛЬТРАЦІЯ
# ============================================================
def extract_budget(text):
    """Витягує бюджет з тексту"""
    patterns = [
        r'\$(\d+(?:,\d+)?(?:\.\d+)?)',
        r'(\d+(?:,\d+)?)\s*(?:USD|usd|dollars?)',
        r'Budget[:\s]+\$?(\d+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            amount = float(match.group(1).replace(',', ''))
            return amount
    return None

def matches_keywords(text):
    """Перевіряє чи є ключові слова в тексті"""
    text_lower = (text or "").lower()
    for keyword in KEYWORDS:
        if keyword.lower() in text_lower:
            return keyword
    return None

def is_relevant(entry, source):
    """Перевіряє чи підходить джоб"""
    title = entry.get("title", "")
    summary = entry.get("summary", "")
    full_text = f"{title} {summary}"

    # Перевірка ключових слів
    matched_keyword = matches_keywords(full_text)
    if not matched_keyword:
        return False, None, None

    # Перевірка бюджету
    budget = extract_budget(full_text)
    if budget and budget < MIN_BUDGET:
        return False, None, None

    return True, matched_keyword, budget

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(message):
    """Надсилає повідомлення в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.ok
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

def format_job_message(entry, source, keyword, budget):
    """Форматує повідомлення про джоб"""
    title = entry.get("title", "No title")
    link = entry.get("link", "")
    summary = entry.get("summary", "")

    # Очищаємо HTML теги з summary
    clean_summary = re.sub(r'<[^>]+>', '', summary)
    clean_summary = clean_summary[:300] + "..." if len(clean_summary) > 300 else clean_summary

    budget_str = f"💰 <b>Budget:</b> ${budget:.0f}" if budget else "💰 <b>Budget:</b> Not specified"

    message = f"""
🔔 <b>NEW JOB — {source}</b>

📌 <b>{title}</b>

🏷 <b>Matched:</b> {keyword}
{budget_str}

📝 {clean_summary}

🔗 <a href="{link}">Apply Now</a>
⏰ {datetime.now().strftime("%H:%M • %d %b %Y")}
"""
    return message.strip()

# ============================================================
# ОСНОВНИЙ МОНІТОРИНГ
# ============================================================
def check_feeds():
    """Перевіряє всі RSS фіди"""
    new_jobs = 0

    for source, feeds in RSS_FEEDS.items():
        for feed_url in feeds:
            try:
                feed = feedparser.parse(feed_url)

                for entry in feed.entries:
                    job_id = entry.get("id") or entry.get("link", "")

                    if not job_id or is_seen(job_id):
                        continue

                    relevant, keyword, budget = is_relevant(entry, source)

                    if relevant:
                        message = format_job_message(entry, source, keyword, budget)
                        if send_telegram(message):
                            mark_seen(job_id)
                            new_jobs += 1
                            print(f"✅ Sent: {entry.get('title', '')[:50]}")
                            time.sleep(1)  # пауза між повідомленнями
                    else:
                        mark_seen(job_id)  # помічаємо як переглянутий

            except Exception as e:
                print(f"Error parsing {feed_url}: {e}")

    return new_jobs

def main():
    print("🚀 Jason Jober is starting...")
    init_db()

    # Стартове повідомлення
    send_telegram("🤖 <b>Jason Jober is online!</b>\n\nMonitoring jobs on Upwork & Freelancer...\n\n🔍 Categories: Writing, Data Entry, Excel, Python, Translation")
    print("✅ Jason Jober is running!")

    while True:
        print(f"\n⏰ Checking feeds... {datetime.now().strftime('%H:%M:%S')}")
        new_jobs = check_feeds()
        print(f"📊 Found {new_jobs} new relevant jobs")
        print(f"💤 Sleeping for {CHECK_INTERVAL // 60} minutes...")
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
