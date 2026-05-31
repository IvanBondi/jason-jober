import re
import logging
import os
from datetime import datetime

# ============================================================
# WHITELIST
# ============================================================
ALLOWED_TELEGRAM_USER_IDS = {288775465}

# ============================================================
# PROMPT INJECTION PATTERNS
# ============================================================
_INJECTION_PATTERNS = [
    r"ignore\s+previous\s+instructions",
    r"forget\s+your\s+rules",
    r"\bact\s+as\b",
    r"new\s+instructions",
    r"system\s+prompt",
    r"ignore\s+all\s+prior",
    r"disregard\s+(your|all|previous)",
    r"you\s+are\s+now\s+(a|an)",
    r"pretend\s+(you\s+are|to\s+be)",
    r"roleplay\s+as",
]

_COMPILED_INJECTIONS = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]

# ============================================================
# SECURITY LOGGER
# ============================================================
_LOG_FILE = os.path.join(os.path.dirname(__file__), "security.log")

logging.basicConfig(level=logging.INFO)
_logger = logging.getLogger("jason_security")

_file_handler = logging.FileHandler(_LOG_FILE)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_logger.addHandler(_file_handler)


def _log_suspicious(context: str, detail: str) -> None:
    msg = f"SUSPICIOUS | context={context} | {detail}"
    _logger.warning(msg)
    print(f"[SECURITY] {msg}")


# ============================================================
# PUBLIC API
# ============================================================

def is_user_allowed(user_id: int) -> bool:
    """Return True if the Telegram user ID is whitelisted."""
    allowed = user_id in ALLOWED_TELEGRAM_USER_IDS
    if not allowed:
        _log_suspicious("telegram", f"unauthorized user_id={user_id}")
    return allowed


def contains_injection(text: str) -> bool:
    """Return True if text contains a prompt injection attempt."""
    for pattern in _COMPILED_INJECTIONS:
        if pattern.search(text):
            return True
    return False


def check_message(user_id: int, text: str) -> bool:
    """
    Validate an incoming Telegram message.
    Returns True if the message is safe to process, False if it should be dropped.
    """
    if not is_user_allowed(user_id):
        return False
    if contains_injection(text):
        _log_suspicious("telegram_message", f"user_id={user_id} injection in: {text[:120]!r}")
        return False
    return True


def sanitize_rss_field(value: str, max_len: int = 2000) -> str:
    """
    Clean a single RSS/email text field:
    - Strip leading/trailing whitespace
    - Collapse internal whitespace runs
    - Truncate to max_len
    - Flag and strip any embedded injection attempts
    """
    if not isinstance(value, str):
        return ""
    value = value.strip()
    value = re.sub(r"\s+", " ", value)
    if contains_injection(value):
        _log_suspicious("rss_field", f"injection stripped from field: {value[:120]!r}")
        for pattern in _COMPILED_INJECTIONS:
            value = pattern.sub("[REDACTED]", value)
    return value[:max_len]


def sanitize_entry(entry: dict) -> dict:
    """
    Sanitize an RSS/email entry dict in-place (returns same dict).
    Cleans title, summary, link, and description fields.
    """
    for field in ("title", "summary", "description", "link"):
        raw = entry.get(field, "")
        cleaned = sanitize_rss_field(raw, max_len=500 if field == "link" else 2000)
        if cleaned != raw and raw:
            _log_suspicious("rss_entry", f"field={field!r} was modified during sanitization")
        entry[field] = cleaned
    return entry
