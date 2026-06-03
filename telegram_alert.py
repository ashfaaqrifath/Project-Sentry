import json
import os
import time

import requests
from dotenv import load_dotenv


load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.environ.get("SENTRY_SETTINGS_FILE", os.path.join(BASE_DIR, "sentry_settings.json"))


def telegram_alerts_enabled():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
    except (OSError, json.JSONDecodeError):
        return True

    return bool(settings.get("telegram_alerts_enabled", True))


def send_telegram_alert(source, message, force=False):
    if not force and not telegram_alerts_enabled():
        return None

    if not BOT_TOKEN or not CHAT_ID:
        return None

    source_label = source.strip().upper()
    text = f"{source_label} ALERT\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{message}"
    send_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    params = {
        "chat_id": CHAT_ID,
        "text": text,
    }

    try:
        response = requests.get(send_url, params=params, timeout=10)
        return response.json()
    except requests.RequestException as exc:
        print(f"Telegram alert failed: {exc}")
        return None


def short_reasons(reasons, limit=3):
    clean = [reason.strip() for reason in reasons if reason and reason.strip()]
    if not clean:
        return "pattern drift"

    shown = clean[:limit]
    extra_count = len(clean) - len(shown)
    text = "; ".join(shown)
    if extra_count > 0:
        text += f"; +{extra_count} more"

    return text


def send_alert(source, score, row_number, reasons):
    message = (
        "Status: Anomaly detected\n"
        f"Score: {score:.1%}\n"
        f"CSV row: {row_number}\n"
        f"Why: {short_reasons(reasons)}"
    )
    return send_telegram_alert(source, message)


def send_drive_alert(drive, level, risk_score, reasons, baseline_score=None):
    baseline_text = ""
    if baseline_score not in (None, ""):
        baseline_text = f"\nBaseline score: {baseline_score}"

    message = (
        f"Status: {level.upper()}\n"
        f"Drive: {drive}\n"
        f"Risk: {risk_score}/100"
        f"{baseline_text}\n"
        f"Why: {short_reasons(reasons)}"
    )
    return send_telegram_alert("drive", message)
