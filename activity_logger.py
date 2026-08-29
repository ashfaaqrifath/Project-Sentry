from datetime import datetime, timedelta
from pathlib import Path
import logging
import os
import sys
import time


ACTIVITY_PREFIX = "SENTRY_ACTIVITY"

_SENTRY_LOGS_DIR = None
_CURRENT_SENTRY_LOG = None
_USER_LOGGER = None
_USER_LOG_FILE = None
_LOGGED_WINDOWS = set()


def make_log_header(timestamp=None):
    timestamp_text = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"SENTRY ACTIVITY LOG - {timestamp_text}\n<< SENTRY ACTIVITY >>\n\n"


def init_activity_log(base_dir=None):
    global _SENTRY_LOGS_DIR, _CURRENT_SENTRY_LOG
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent
    _SENTRY_LOGS_DIR = base / "sentry logs"
    os.makedirs(_SENTRY_LOGS_DIR, exist_ok=True)
    session_start = datetime.now().strftime("%Y%m%d_%H%M%S")
    _CURRENT_SENTRY_LOG = _SENTRY_LOGS_DIR / f"sentry_log_{session_start}.txt"

    try:
        with _CURRENT_SENTRY_LOG.open("w", encoding="utf-8") as file_handle:
            file_handle.write(make_log_header())
    except OSError:
        _CURRENT_SENTRY_LOG = None


def get_latest_activity_log_path():
    global _SENTRY_LOGS_DIR, _CURRENT_SENTRY_LOG
    if _CURRENT_SENTRY_LOG and _CURRENT_SENTRY_LOG.exists():
        return str(_CURRENT_SENTRY_LOG)
    if _SENTRY_LOGS_DIR is None:
        _SENTRY_LOGS_DIR = Path(__file__).resolve().parent / "sentry logs"
    try:
        if not _SENTRY_LOGS_DIR.exists():
            return None
        files = sorted(path for path in _SENTRY_LOGS_DIR.iterdir() if path.suffix == ".txt")
        return str(files[-1]) if files else None
    except OSError:
        return None


def make_activity_line(command, response, source=None, timestamp=None):
    command_text = str(command or "").strip()
    response_text = str(response or "").strip()
    timestamp_text = str(timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")).strip()
    source_text = str(source or "").strip()
    if source_text:
        return f"{ACTIVITY_PREFIX}|{timestamp_text}|{source_text}|{command_text}|{response_text}"
    return f"{ACTIVITY_PREFIX}|{timestamp_text}|{command_text}|{response_text}"


def append_activity_line(command, response, source=None, timestamp=None):
    line = make_activity_line(command, response, source=source, timestamp=timestamp)
    path = get_latest_activity_log_path()
    if not path:
        return False
    try:
        with open(path, "a", encoding="utf-8") as file_handle:
            file_handle.write(line + "\n")
        return True
    except OSError:
        return False


def parse_activity_line(line):
    if not line:
        return None
    text = str(line).strip()
    if not text.startswith(ACTIVITY_PREFIX):
        return None
    payload = text[len(ACTIVITY_PREFIX):]
    if not payload.startswith("|"):
        return None
    parts = payload[1:].split("|")
    if len(parts) == 3:
        timestamp, command_text, response_text = parts
        source = ""
    elif len(parts) >= 4:
        timestamp, source, command_text, response_text = parts[0], parts[1], parts[2], "|".join(parts[3:])
    else:
        return None
    command_text = command_text.strip()
    response_text = response_text.strip()
    timestamp = timestamp.strip() if timestamp else ""
    source = source.strip()
    if not command_text and not response_text:
        return None
    return {"timestamp": timestamp, "command": command_text, "feedback": response_text, "source": source}


def prune_activity_entries(entries, days=7):
    cutoff = datetime.now() - timedelta(days=days)
    pruned = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        timestamp_text = str(entry.get("timestamp") or "").strip()
        try:
            timestamp = datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            try:
                timestamp = datetime.fromisoformat(timestamp_text)
            except (TypeError, ValueError):
                continue
        if timestamp >= cutoff:
            pruned.append(entry)
    return pruned


def _user_log_path(base_dir=None):
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent
    logs_dir = base / "user logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    session_start = time.strftime("%Y%m%d_%H%M%S")
    return logs_dir / f"user_log_{session_start}.txt"


def _user_logger(path):
    logger = logging.getLogger("project_sentry.user_activity")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    return logger


def _window_activity(logger):
    global _LOGGED_WINDOWS
    try:
        import pygetwindow as window_manager

        open_windows = window_manager.getWindowsWithTitle("")
        for window in open_windows:
            if window.title not in _LOGGED_WINDOWS:
                logger.info(f"Opened : {window.title}")
                _LOGGED_WINDOWS.add(window.title)
        for title in _LOGGED_WINDOWS.copy():
            if title not in window_manager.getAllTitles():
                logger.info(f"Closed : {title}")
                _LOGGED_WINDOWS.remove(title)
    except Exception:
        pass


def activity_logger(base_dir=None, interval=60):
    global _USER_LOGGER, _USER_LOG_FILE
    _USER_LOG_FILE = _user_log_path(base_dir)
    try:
        _USER_LOG_FILE.write_text(make_log_header(), encoding="utf-8")
    except OSError:
        return
    _USER_LOGGER = _user_logger(_USER_LOG_FILE)
    import psutil
    import socket
    hostname = socket.gethostname()
    try:
        ip_address = socket.gethostbyname(hostname)
    except OSError:
        ip_address = "Unknown"
    try:
        username = os.getlogin()
    except OSError:
        username = os.environ.get("USERNAME", "Unknown")
    cpu_usage = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    ram_used = memory.used / (1024 ** 3)
    ram_available = memory.available / (1024 ** 3)
    uptime = datetime.fromtimestamp(psutil.boot_time())
    process_id = os.getpid()

    _USER_LOGGER.info(
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "<< ACTIVITY LOG >>\n\n"
        f"> IP Address: {ip_address}\n"
        f"> Active user: {username}\n"
        f"> CPU Usage: {cpu_usage}%\n"
        f"> RAM Usage: {ram_used:.2f} GB\n"
        f"> Available RAM: {ram_available:.2f} GB\n"
        f"> System uptime: {uptime}\n"
        f"> Process ID: {process_id}\n"
    )

    try:
        while True:
            _window_activity(_USER_LOGGER)
            time.sleep(interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    activity_logger(Path(__file__).resolve().parent)