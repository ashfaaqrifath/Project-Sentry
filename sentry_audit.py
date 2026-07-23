from datetime import datetime, timedelta
from pathlib import Path
import os

AUDIT_PREFIX = "SENTRY_AUDIT"


_LOGS_DIR = None
_CURRENT_LOG = None


def init_audit_log(base_dir=None):
    global _LOGS_DIR, _CURRENT_LOG
    base = Path(base_dir) if base_dir else Path(__file__).resolve().parent
    _LOGS_DIR = base / "sentry logs"
    os.makedirs(_LOGS_DIR, exist_ok=True)
    session_start = datetime.now().strftime("%Y%m%d_%H%M%S")
    _CURRENT_LOG = _LOGS_DIR / f"sentry_log_{session_start}.txt"
    # Write a short header
    try:
        with _CURRENT_LOG.open("w", encoding="utf-8") as fh:
            fh.write(f"SENTRY AUDIT LOG - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            fh.write("<< SENTRY AUDIT >>\n\n")
    except OSError:
        _CURRENT_LOG = None


def get_latest_log_path():
    global _LOGS_DIR, _CURRENT_LOG
    if _CURRENT_LOG and _CURRENT_LOG.exists():
        return str(_CURRENT_LOG)
    if _LOGS_DIR is None:
        base = Path(__file__).resolve().parent
        _LOGS_DIR = base / "sentry logs"
    try:
        if not _LOGS_DIR.exists():
            return None
        files = sorted([p for p in Path(_LOGS_DIR).iterdir() if p.suffix == ".txt"])
        if files:
            return str(files[-1])
    except Exception:
        pass
    return None


def make_audit_line(command, response, source=None, timestamp=None):
    cmd = str(command or "").strip()
    resp = str(response or "").strip()
    stamp = str(timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")).strip()
    src = str(source or "").strip()
    if src:
        return f"{AUDIT_PREFIX}|{stamp}|{src}|{cmd}|{resp}"
    return f"{AUDIT_PREFIX}|{stamp}|{cmd}|{resp}"


def append_audit_line(command, response, source=None, timestamp=None):
    line = make_audit_line(command, response, source=source, timestamp=timestamp)
    path = get_latest_log_path()
    if not path:
        return False
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except OSError:
        return False


def parse_audit_line(line):
    if not line:
        return None
    text = str(line).strip()
    if not text.startswith(AUDIT_PREFIX):
        return None
    payload = text[len(AUDIT_PREFIX):]
    if not payload.startswith("|"):
        return None
    parts = payload[1:].split("|")
    # support both formats: with source (4 parts) or without source (3 parts)
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
    source = source.strip() if 'source' in locals() else ""
    if not command_text and not response_text:
        return None
    return {"timestamp": timestamp, "command": command_text, "feedback": response_text, "source": source}


def prune_audit_entries(entries, days=7):
    cutoff = datetime.now() - timedelta(days=days)
    pruned = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        timestamp_text = str(entry.get("timestamp") or "").strip()
        timestamp = None
        if timestamp_text:
            try:
                from datetime import datetime as _dt
                timestamp = _dt.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S")
            except Exception:
                try:
                    timestamp = datetime.fromisoformat(timestamp_text)
                except Exception:
                    timestamp = None
        if timestamp is None:
            continue
        if timestamp >= cutoff:
            pruned.append(entry)
    return pruned
