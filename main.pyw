import csv
import ctypes
import getpass
import hashlib
import html
import hmac
import json
import mimetypes
import os
import secrets
import shutil
import socket
import sys
import subprocess
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from http.cookies import SimpleCookie
from threading import Lock, Thread
from dotenv import load_dotenv
from telegram_alert import send_telegram_alert
from generate_report import build_anomaly_report_pdf

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
REPORTS_DIR = BASE_DIR / "reports"
DETECTION_ENGINE_DIR = BASE_DIR / "detection engine"

BOT_CONTROLLER_DIR = BASE_DIR / "command_engine.py"
if str(BOT_CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_CONTROLLER_DIR))

from activity_logger import parse_activity_line, prune_activity_entries, init_activity_log, get_latest_activity_log_path, append_activity_line, make_activity_line

try:
    import psutil
except ImportError:
    psutil = None

load_dotenv()

try:
    init_activity_log(BASE_DIR)
except Exception:
    pass

HOST = "0.0.0.0"
PORT = 8765
SETTINGS_FILE = BASE_DIR / "settings.json"
DASHBOARD_HTML_FILE = BASE_DIR / "dashboard.html"

AUTO_START_COMPONENTS = False

FILES = {
    "keystroke_training": DETECTION_ENGINE_DIR / "keystroke dynamics" / "keystroke_dynamics_training.csv",
    "keystroke_detection": DETECTION_ENGINE_DIR / "keystroke dynamics" / "keystroke_detections.csv",
    "mouse_training": DETECTION_ENGINE_DIR / "mouse dynamics" / "mouse_dynamics_training.csv",
    "mouse_detection": DETECTION_ENGINE_DIR / "mouse dynamics" / "mouse_detections.csv",
    "network_training": DETECTION_ENGINE_DIR / "network usage" / "network_usage_training.csv",
    "network_detection": DETECTION_ENGINE_DIR / "network usage" / "network_detections.csv",
    "drive_training": DETECTION_ENGINE_DIR / "drive health monitor" / "drive_health_training.csv",
    "drive_detection": DETECTION_ENGINE_DIR / "drive health monitor" / "drive_detections_live.csv",
}
DRIVE_DEMO_DETECTION_FILE = DETECTION_ENGINE_DIR / "drive health monitor" / "drive_detections_demo.csv"
RETRAIN_ARTIFACTS = {
    "keystroke": [
        FILES["keystroke_training"],
        DETECTION_ENGINE_DIR / "keystroke dynamics" / "keystroke_dynamics_model.pkl",
        DETECTION_ENGINE_DIR / "keystroke dynamics" / "ocsvm_scaler.pkl",
        DETECTION_ENGINE_DIR / "keystroke dynamics" / "ocsvm_threshold.pkl",
    ],
    "mouse": [
        FILES["mouse_training"],
        DETECTION_ENGINE_DIR / "mouse dynamics" / "mouse_dynamics_model.pkl",
    ],
    "network": [
        FILES["network_training"],
        DETECTION_ENGINE_DIR / "network usage" / "network_usage_model.pkl",
    ],
    "drive": [
        FILES["drive_training"],
        DETECTION_ENGINE_DIR / "drive health monitor" / "live_model.pkl",
        DETECTION_ENGINE_DIR / "drive health monitor" / "demo_model.pkl",
    ],
}
USER_LOGS_DIR = BASE_DIR / "user logs"

TRAINING_TARGETS = {
    "keystroke": 1000,
    "mouse": 1000,
    "network": 1000,
    "drive": 100,
}


def get_training_target(name):
    return TRAINING_TARGETS.get(name, 0)


def parse_training_target(data):
    raw_target = data.get("target_rows")
    if isinstance(raw_target, bool):
        raise ValueError("target_rows must be a whole number")
    try:
        target_rows = int(str(raw_target).strip())
    except (TypeError, ValueError):
        raise ValueError("target_rows must be a whole number")
    if target_rows < 100 or target_rows > 1000000:
        raise ValueError("target_rows must be higher")
    return target_rows


COMPONENTS = {
    "keystroke": DETECTION_ENGINE_DIR / "keystroke dynamics" / "keystroke_dynamics_monitor.py",
    "mouse": DETECTION_ENGINE_DIR / "mouse dynamics" / "mouse_dynamics_monitor.py",
    "network": DETECTION_ENGINE_DIR / "network usage" / "network_usage_monitor.py",
    "drive": DETECTION_ENGINE_DIR / "drive health monitor" / "drive_health_monitor.py",
    "activity": BASE_DIR / "activity_logger.py",
    "remote": BASE_DIR / "command_engine.py",
}

running_processes = {}
process_lock = Lock()
component_logs = {name: [] for name in COMPONENTS}
training_jobs = {}
ACTIVITY_HISTORY_FILE = None
activity_history = []
SERVER_STARTED_AT = time.time()
shutdown_requested = False
expected_stops = set()
sessions = {}
failed_login_attempts = {}
drive_smart_cache = {"timestamp": 0.0, "data": {}}

last_behavior_alert_at = 0.0
last_solo_alert_at = {"keystroke": 0.0, "mouse": 0.0}

DEFAULT_BEHAVIOR_ALERTS = {
    "window_seconds": 45,
    "combined_threshold": 0.12,
    "min_single": 0.04,
    "cooldown_seconds": 300,
    "solo_window_seconds": 60,
    "solo_high_threshold": 0.10,
    "solo_sustained_min_hits": 5,
    "solo_streak_min": 5,
    "solo_cooldown_seconds": 420,
    "solo_other_peak_max": 0.035,
}


def get_token():
    token = os.getenv("SENTRY_DASHBOARD_TOKEN", "")
    if token:
        return token

    token = secrets.token_urlsafe(24)
    return token


TOKEN = get_token()


def username():
    username = os.getlogin()
    return f"{username}"


def local_ip_address():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def read_settings():
    if not SETTINGS_FILE.exists():
        return {"components": {}, "telegram_alerts_enabled": False}

    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"components": {}, "telegram_alerts_enabled": False}


def initialize_dashboard_auth():
    settings = read_settings()
    if "dashboard_auth" in settings:
        return

    password = os.getenv("DASHBOARD_PASSWORD")
    if password is None:
        password = getpass.getpass("Set dashboard password: ")
    if not password:
        raise RuntimeError("Dashboard password cannot be empty.")

    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 100000
    )
    settings["dashboard_auth"] = {
        "salt": salt.hex(),
        "hash": password_hash.hex(),
    }
    write_settings(settings)


def verify_dashboard_password(password):
    auth = read_settings().get("dashboard_auth", {})
    try:
        salt = bytes.fromhex(auth["salt"])
        expected_hash = bytes.fromhex(auth["hash"])
    except (KeyError, TypeError, ValueError):
        return False

    actual_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 100000
    )
    return hmac.compare_digest(actual_hash, expected_hash)


def load_activity_history():
                                                                       
    try:
        path = get_latest_activity_log_path()
        if not path:
            return []
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []

    entries = []
    for line in lines:
        parsed = parse_activity_line(line)
        if not parsed:
            continue
        entries.append({
            "timestamp": str(parsed.get("timestamp") or "").strip(),
            "command": str(parsed.get("command") or "").strip(),
            "feedback": str(parsed.get("feedback") or "").strip(),
            "source": str(parsed.get("source") or "").strip(),
        })

    return prune_activity_entries(entries, days=7)


def save_activity_history(entries):
    return


def write_settings(settings):
    for path in [SETTINGS_FILE]:
        try:
            path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"Could not update {path.name}: {exc}", flush=True)


def _dpapi_encrypt_bytes(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("only available on Windows.")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_void_p)]

    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    out_blob = DATA_BLOB()

    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise OSError("CryptProtectData failed.")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        local_free(ctypes.c_void_p(out_blob.pbData))


def _dpapi_decrypt_bytes(data: bytes) -> bytes:
    if os.name != "nt":
        raise OSError("only available on Windows.")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_void_p)]

    local_free = ctypes.windll.kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    in_blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    out_blob = DATA_BLOB()

    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise OSError("CryptUnprotectData failed.")

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        local_free(ctypes.c_void_p(out_blob.pbData))


def iter_sensitive_files():
    candidates = []

    for directory in (USER_LOGS_DIR, BASE_DIR / "sentry logs", BASE_DIR / "reports"):
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir(), key=lambda item: str(item.name)):
            if not path.is_file() or path.name.endswith(".enc"):
                continue
            if path.suffix.lower() in {".pkl"}:
                continue
            candidates.append(path)

    for csv_path in sorted(BASE_DIR.rglob("*.csv"), key=lambda item: str(item)):
        if not csv_path.is_file() or csv_path.name.endswith(".enc"):
            continue
        if any(part in {".git", "__pycache__", "venv", ".venv"} for part in csv_path.parts):
            continue
        candidates.append(csv_path)

    return candidates


def encrypt_sensitive_files():
    for path in iter_sensitive_files():
        encrypted_path = path.with_name(path.name + ".enc")
        if encrypted_path.exists() or path.name.endswith(".enc"):
            continue
        try:
            encrypted_path.write_bytes(_dpapi_encrypt_bytes(path.read_bytes()))
            path.unlink(missing_ok=True)
        except OSError:
            continue


def decrypt_sensitive_files():
    encrypted_paths = []
    for directory in (USER_LOGS_DIR, BASE_DIR / "sentry logs", BASE_DIR / "reports"):
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir(), key=lambda item: str(item.name)):
            if path.is_file() and path.name.endswith(".enc"):
                encrypted_paths.append(path)

    for path in sorted(BASE_DIR.rglob("*.*"), key=lambda item: str(item)):
        if not path.is_file() or not path.name.endswith(".enc"):
            continue
        if any(part in {".git", "__pycache__", "venv", ".venv"} for part in path.parts):
            continue
        if path.suffix.lower() == ".pkl":
            continue
        encrypted_paths.append(path)

    seen = set()
    for encrypted_path in sorted(encrypted_paths, key=lambda item: str(item)):
        if not encrypted_path.exists() or encrypted_path in seen:
            continue
        seen.add(encrypted_path)
        original_name = encrypted_path.name[:-4]
        if original_name.endswith(".csv") or original_name.endswith(".txt") or original_name.endswith(".pdf"):
            original_path = encrypted_path.with_name(original_name)
        else:
            original_path = encrypted_path.with_name(encrypted_path.name[:-4])
        if original_path.exists():
            continue
        try:
            original_path.write_bytes(_dpapi_decrypt_bytes(encrypted_path.read_bytes()))
            encrypted_path.unlink(missing_ok=True)
        except OSError:
            continue


def read_csv_rows(path):
    if not path.exists():
        return []

    try:
        with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
            rows = [line for line in csv_file if not line.lstrip().startswith("#")]
            return list(csv.DictReader(rows))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


def delete_all_anomaly_detections():
    for key, path in FILES.items():
        if key.endswith("_detection"):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                try:
                    path.write_text("", encoding="utf-8")
                except Exception:
                    pass


def delete_log_directories():
    with process_lock:
        activity_process = running_processes.get("activity")
    restart_activity = activity_process is not None and activity_process.poll() is None
    if activity_process is not None:
        stop_components([("activity", activity_process)])

    for directory in (USER_LOGS_DIR, REPORTS_DIR, BASE_DIR / "sentry logs"):
        try:
            if directory.exists() and directory.is_dir():
                shutil.rmtree(directory)
            elif directory.exists():
                directory.unlink()
        except OSError:
            pass
        directory.mkdir(parents=True, exist_ok=True)

    global activity_history
    activity_history.clear()
    with process_lock:
        for lines in component_logs.values():
            lines.clear()
    init_activity_log(BASE_DIR)
    if restart_activity:
        start_component("activity")
        activity_history.clear()
        with process_lock:
            component_logs["activity"].clear()


def latest_user_log_path():
    if not USER_LOGS_DIR.exists():
        return None
    try:
        files = [
            path for path in USER_LOGS_DIR.iterdir()
            if path.is_file() and path.suffix.lower() == ".txt"
        ]
        if not files:
            return None
        return max(files, key=lambda path: path.stat().st_mtime)
    except Exception:
        return None


def delete_retrain_artifacts(component):
    artifacts = RETRAIN_ARTIFACTS.get(component, [])
    deleted = []
    for artifact in artifacts:
        try:
            if artifact.exists():
                artifact.unlink()
                deleted.append(str(artifact))
        except Exception as exc:
            print(f"Could not delete {artifact}: {exc}", flush=True)
    return deleted


def delete_model_artifacts(component):
    artifacts = RETRAIN_ARTIFACTS.get(component, [])
    deleted = []
    for artifact in artifacts:
        if artifact == FILES.get(f"{component}_training"):
            continue
        try:
            if artifact.exists():
                artifact.unlink()
                deleted.append(str(artifact))
        except Exception as exc:
            print(f"Could not delete {artifact}: {exc}", flush=True)
    return deleted


def start_process(component, extra_env=None):
    script_path = COMPONENTS[component]
    if not script_path.exists():
        message = f"Skipping {component}: missing file {script_path}"
        print(message, flush=True)
        append_component_log(component, message)
        return None

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["SENTRY_DASHBOARD_TOKEN"] = TOKEN
    if extra_env:
        for env_key, env_value in extra_env.items():
            env[str(env_key)] = str(env_value)

    process = subprocess.Popen(
        [sys.executable, "-u", str(script_path)],
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    with process_lock:
        running_processes[component] = process
        if component not in component_logs:
            component_logs[component] = []
        expected_stops.discard(component)

    Thread(target=stream_process_output, args=(component, process), daemon=True).start()
    message = f"Started {component} (PID {process.pid})"
    print(message, flush=True)
    append_component_log(component, message)
    try:
        append_activity_line(f"start {component}", message, source="system")
        line = make_activity_line(f"start {component}", message, source="system")
        parsed = parse_activity_line(line)
        if parsed:
            activity_history.append({
                "timestamp": str(parsed.get("timestamp") or "").strip(),
                "command": str(parsed.get("command") or "").strip(),
                "feedback": str(parsed.get("feedback") or "").strip(),
                "source": str(parsed.get("source") or "").strip(),
            })
            activity_history[:] = prune_activity_entries(activity_history, days=7)
    except Exception:
        pass
    return process


def train_component(component, target=None):
    if component not in RETRAIN_ARTIFACTS or component == "drive":
        return

    with process_lock:
        process = running_processes.get(component)
    if process is not None and process.poll() is None:
        stop_components([(component, process)])

    delete_model_artifacts(component)
    training_path = FILES[f"{component}_training"]
    target_rows = target if target is not None else row_count(training_path) + 500
    with process_lock:
        training_jobs[component] = {"action": "training", "target": target_rows}
    start_process(component, extra_env={
        "SENTRY_TRAINING_TARGET_ROWS": target_rows,
        "SENTRY_FORCE_LIVE_TRAINING": "1",
    })


def retrain_component(component, target=None):
    if component not in RETRAIN_ARTIFACTS:
        return

    with process_lock:
        process = running_processes.get(component)
    if process is not None and process.poll() is None:
        stop_components([(component, process)])

    delete_retrain_artifacts(component)
    target_rows = target if target is not None else TRAINING_TARGETS[component]
    with process_lock:
        training_jobs[component] = {"action": "retraining", "target": target_rows}
    if component == "drive":
        start_process(component, extra_env={
            "SENTRY_TRAINING_TARGET_ROWS": target_rows,
        })
        return

    start_process(component, extra_env={
        "SENTRY_TRAINING_TARGET_ROWS": target_rows,
        "SENTRY_FORCE_LIVE_TRAINING": "1",
    })


def load_user_logs(limit=80):
    path = latest_user_log_path()
    if not path:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []

    entries = []
    in_header = True
    for raw_line in lines[-limit:]:
        line = raw_line.strip()
        if not line:
            continue

        if in_header:
            if line.startswith("> "):
                continue
            if line.startswith("Opened :") or line.startswith("Closed :"):
                in_header = False
            elif " - " in line:
                maybe_ts, maybe_message = line.split(" - ", 1)
                if maybe_message.startswith("Opened :") or maybe_message.startswith("Closed :"):
                    in_header = False
                    timestamp, message = maybe_ts.strip(), maybe_message.strip()
                    entries.append({"timestamp": timestamp, "message": message})
                    continue
            if not line.startswith("> "):
                                                                                              
                if line.startswith("Opened :") or line.startswith("Closed :"):
                    in_header = False
                else:
                    continue

        timestamp = ""
        message = line
        if " - " in message:
            parts = message.split(" - ", 1)
            timestamp = parts[0].strip()
            message = parts[1].strip()

        if message.startswith("> "):
            continue
        if not message.startswith("Opened :") and not message.startswith("Closed :"):
            continue

        entries.append({"timestamp": timestamp, "message": message})
    return entries


def safe_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_time(value):
    if not value:
        return None

    try:
        timestamp = float(value)
        if timestamp > 0:
            return datetime.fromtimestamp(timestamp)
    except (TypeError, ValueError, OSError, OverflowError):
        pass

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%m/%d/%y:%b:%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue

    return None


def row_count(path):
    return len(read_csv_rows(path))


def newest_row(rows):
    if not rows:
        return {}
    return rows[-1]


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def system_uptime():
    if sys.platform == "win32":
        try:
            milliseconds = ctypes.windll.kernel32.GetTickCount64()
            return format_duration(milliseconds / 1000)
        except Exception:
            return "unknown"


def battery_percent():
    if psutil is None:
        return None
    try:
        battery = psutil.sensors_battery()
        if battery is None:
            return None
        return max(0, min(100, int(battery.percent)))
    except Exception:
        return None


def is_user_present():
    
    if sys.platform != "win32":
        return True
    try:

        if ctypes.windll.user32.GetForegroundWindow() == 0:
            return False

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            idle_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            if idle_ms > 60000:                                           
                return False
        return True
    except Exception:
        return True

def numeric_values(rows, column):
    values = []
    for row in rows:
        value = row.get(column)
        if value not in (None, ""):
            values.append(safe_float(value))
    return values


def average(values):
    return sum(values) / len(values) if values else 0.0


def format_hour_label(timestamp):
    return timestamp.strftime("%I %p").lstrip("0").replace(" 0", " 0")


def build_hourly_trend(rows, now=None):
    if now is None:
        now = datetime.now()

    trend = []
    for offset in range(23, -1, -1):
        bucket_start = now - timedelta(hours=offset)
        bucket_end = bucket_start + timedelta(hours=1)
        anomalies = 0
        count = 0
        for row in rows:
            timestamp = parse_time(row.get("timestamp"))
            if not timestamp:
                continue
            if bucket_start <= timestamp < bucket_end:
                count += 1
                if str(row.get("prediction", "")).lower() == "anomaly":
                    anomalies += 1
        trend.append(
            {
                "date": format_hour_label(bucket_start),
                "speed": anomalies,
                "samples": count,
            }
        )
    return trend


def typing_speed_trend(rows):
    today = datetime.now().date()
    trend = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        anomalies = 0
        count = 0
        for row in rows:
            timestamp = parse_time(row.get("timestamp"))
            if timestamp and timestamp.date() == day:
                count += 1
                if str(row.get("prediction", "")).lower() == "anomaly":
                    anomalies += 1
        trend.append(
            {
                "date": day.strftime("%a"),
                "speed": anomalies,
                "samples": count,
            }
        )
    return trend


def typing_speed_trend_24h(rows):
    return build_hourly_trend(rows)


def mouse_dynamics_trend(rows):
    today = datetime.now().date()
    trend = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        anomalies = 0
        count = 0
        for row in rows:
            timestamp = parse_time(row.get("timestamp"))
            if timestamp and timestamp.date() == day:
                count += 1
                if str(row.get("prediction", "")).lower() == "anomaly":
                    anomalies += 1
        trend.append(
            {
                "date": day.strftime("%a"),
                "speed": anomalies,
                "samples": count,
            }
        )
    return trend


def mouse_dynamics_trend_24h(rows):
    return build_hourly_trend(rows)


def network_usage_trend(rows):
    today = datetime.now().date()
    trend = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        anomalies = 0
        count = 0
        for row in rows:
            timestamp = parse_time(row.get("timestamp"))
            if timestamp and timestamp.date() == day:
                count += 1
                if str(row.get("prediction", "")).lower() == "anomaly":
                    anomalies += 1
        trend.append(
            {
                "date": day.strftime("%a"),
                "speed": anomalies,
                "samples": count,
            }
        )
    return trend


def network_usage_trend_24h(rows):
    return build_hourly_trend(rows)


def drive_health_trend(rows):
                                                                                      
    today = datetime.now().date()
    trend = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        anomalies = 0
        count = 0
        for row in rows:
            timestamp = parse_time(row.get("timestamp"))
            if timestamp and timestamp.date() == day:
                count += 1
                if str(row.get("prediction", "")).lower() == "anomaly":
                    anomalies += 1
        trend.append(
            {
                "date": day.strftime("%a"),
                "speed": anomalies,
                "samples": count,
            }
        )
    return trend


def drive_health_trend_24h(rows):
    return build_hourly_trend(rows)


def behavior_insights():
    key_detection = read_csv_rows(FILES["keystroke_detection"])
    mouse_detection = read_csv_rows(FILES["mouse_detection"])
    network_detection = read_csv_rows(FILES.get("network_detection"))
    drive_detection = read_csv_rows(FILES.get("drive_detection")) + read_csv_rows(DRIVE_DEMO_DETECTION_FILE)
    drive_trend = drive_health_trend(drive_detection)
    drive_trend_24h = drive_health_trend_24h(drive_detection)
    drive_state = drive_summary()
    if not any(point["speed"] for point in drive_trend) and drive_state["status"] in ("warning", "degraded"):
        drive_trend[-1] = {"date": datetime.now().strftime("%a"), "speed": 1, "samples": 1}
    if not any(point["speed"] for point in drive_trend_24h) and drive_state["status"] in ("warning", "degraded"):
        drive_trend_24h[-1] = {"date": format_hour_label(datetime.now()), "speed": 1, "samples": 1}

    return {
        "typing_speed_trend": typing_speed_trend(key_detection),
        "typing_speed_trend_24h": typing_speed_trend_24h(key_detection),
        "mouse_dynamics_trend": mouse_dynamics_trend(mouse_detection),
        "mouse_dynamics_trend_24h": mouse_dynamics_trend_24h(mouse_detection),
        "network_usage_trend": network_usage_trend(network_detection),
        "network_usage_trend_24h": network_usage_trend_24h(network_detection),
        "drive_health_trend": drive_trend,
        "drive_health_trend_24h": drive_trend_24h,
        "system_uptime": system_uptime(),
        "dashboard_uptime": format_duration(time.time() - SERVER_STARTED_AT),
    }


def count_recent_anomalies(rows, hours=24):
    cutoff = datetime.now() - timedelta(hours=hours)
    total = 0
    for row in rows:
        if str(row.get("prediction", "")).lower() != "anomaly":
            continue
        timestamp = parse_time(row.get("timestamp"))
        if timestamp is None or timestamp >= cutoff:
            total += 1
    return total


def behavior_alert_settings():
    settings = read_settings()
    configured = settings.get("behavior_alerts", {})
    if not isinstance(configured, dict):
        configured = {}

    merged = dict(DEFAULT_BEHAVIOR_ALERTS)
    merged["window_seconds"] = max(10, safe_int(configured.get("window_seconds"), merged["window_seconds"]))
    merged["combined_threshold"] = max(
        0.01, safe_float(configured.get("combined_threshold"), merged["combined_threshold"])
    )
    merged["min_single"] = max(0.0, safe_float(configured.get("min_single"), merged["min_single"]))
    merged["cooldown_seconds"] = max(60, safe_int(configured.get("cooldown_seconds"), merged["cooldown_seconds"]))
    merged["solo_window_seconds"] = max(
        merged["window_seconds"],
        safe_int(configured.get("solo_window_seconds"), merged["solo_window_seconds"]),
    )
    merged["solo_high_threshold"] = max(
        merged["min_single"] + 0.01,
        safe_float(configured.get("solo_high_threshold"), merged["solo_high_threshold"]),
    )
    merged["solo_sustained_min_hits"] = max(
        3, safe_int(configured.get("solo_sustained_min_hits"), merged["solo_sustained_min_hits"])
    )
    merged["solo_streak_min"] = max(3, safe_int(configured.get("solo_streak_min"), merged["solo_streak_min"]))
    merged["solo_cooldown_seconds"] = max(
        merged["cooldown_seconds"],
        safe_int(configured.get("solo_cooldown_seconds"), merged["solo_cooldown_seconds"]),
    )
    merged["solo_other_peak_max"] = max(
        0.0, safe_float(configured.get("solo_other_peak_max"), merged["solo_other_peak_max"])
    )
    return merged


def recent_behavior_anomaly_scores(rows, window_seconds):
    cutoff = datetime.now() - timedelta(seconds=window_seconds)
    scores = []
    for row in rows:
        if str(row.get("prediction", "")).lower() != "anomaly":
            continue
        timestamp = parse_time(row.get("timestamp"))
        if timestamp is None or timestamp < cutoff:
            continue
        score = safe_float(row.get("score"))
        if score < 0:
            scores.append(abs(score))
    return scores


def other_modality_blocks_solo(other_rows, cfg):
    other_scores = recent_behavior_anomaly_scores(other_rows, cfg["window_seconds"])
    if not other_scores:
        return False
    return max(other_scores) >= cfg["solo_other_peak_max"]


def consecutive_anomaly_streak(rows):
    streak = 0
    for row in reversed(rows):
        if str(row.get("prediction", "")).lower() != "anomaly":
            break
        score = safe_float(row.get("score"))
        if score >= 0:
            break
        streak += 1
    return streak


def has_consecutive_normal_streak(rows, required=3):
    if len(rows) < required:
        return False
    return all(
        str(row.get("prediction", "")).lower() == "normal"
        for row in rows[-required:]
    )


def evaluate_solo_behavior_alert(rows, source_name, cfg=None):

    if cfg is None:
        cfg = behavior_alert_settings()

    scores = recent_behavior_anomaly_scores(rows, cfg["solo_window_seconds"])
    if not scores:
        return None

    peak = max(scores)
    streak = consecutive_anomaly_streak(rows)
    trigger = None

    if peak >= cfg["solo_high_threshold"]:
        trigger = "high_score"
    elif streak >= cfg["solo_streak_min"]:
        trigger = "streak"
    elif len(scores) >= cfg["solo_sustained_min_hits"]:
        trigger = "sustained"

    if not trigger:
        return None

    return {
        "source": source_name,
        "trigger": trigger,
        "peak": round(-peak, 4),
        "peak_magnitude": round(peak, 4),
        "hits": len(scores),
        "streak": streak,
        "window_seconds": cfg["solo_window_seconds"],
    }


def evaluate_combined_behavior_alert(k_rows, m_rows, cfg=None):

    if cfg is None:
        cfg = behavior_alert_settings()

    k_scores = recent_behavior_anomaly_scores(k_rows, cfg["window_seconds"])
    m_scores = recent_behavior_anomaly_scores(m_rows, cfg["window_seconds"])
    if not k_scores or not m_scores:
        return None

    k_peak = max(k_scores)
    m_peak = max(m_scores)
    intensity = k_peak + m_peak
    if intensity < cfg["combined_threshold"]:
        return None
    if k_peak < cfg["min_single"] or m_peak < cfg["min_single"]:
        return None

    return {
        "intensity": round(intensity, 4),
        "keystroke_peak": round(-k_peak, 4),
        "mouse_peak": round(-m_peak, 4),
        "keystroke_hits": len(k_scores),
        "mouse_hits": len(m_scores),
        "window_seconds": cfg["window_seconds"],
    }


def behavior_summary(name, detection_path, training_path):
    rows = read_csv_rows(detection_path)
    latest = newest_row(rows)
    prediction = str(latest.get("prediction", "training" if not rows else "unknown")).lower()
    score = safe_float(latest.get("score"), None)
    training_rows = row_count(training_path)
    target = get_training_target(name)
    with process_lock:
        job = training_jobs.get(name)

    if score is None:
        score_text = "N/A"
    else:
        score_text = f"{score:.3f}"

    recent_rows = rows[-100:] if len(rows) >= 100 else rows
    anomaly_count = sum(1 for r in recent_rows if str(r.get("prediction", "")).lower() == "anomaly")
    anomaly_percent = round((anomaly_count / len(recent_rows)) * 100, 1) if recent_rows else 0.0
    if has_consecutive_normal_streak(rows):
        anomaly_percent = 0.0
    progress_target = job["target"] if job else target

    return {
        "status": prediction,
        "score": score,
        "score_text": score_text,
        "latest_timestamp": latest.get("timestamp", ""),
        "detections": len(rows),
        "anomalies_24h": count_recent_anomalies(rows),
        "anomaly_percent": anomaly_percent,
        "training_rows": training_rows,
        "training_target": target,
        "training_percent": min(100, round((training_rows / progress_target) * 100, 1)),
        "training_status": job["action"] if job and training_rows < job["target"] else "ready",
        "training_job_target": job["target"] if job else target,
    }


def read_drive_type():
    path = FILES["drive_training"]
    if not path.exists():
        return "unknown"

    try:
        with path.open("r", encoding="utf-8-sig") as csv_file:
            first_line = csv_file.readline().strip()
    except OSError:
        return "unknown"

    if first_line.lower().startswith("# drive_type:"):
        return first_line.split(":", 1)[1].strip().lower() or "unknown"
    return "unknown"


def latest_drive_log_state():
    with process_lock:
        logs = list(component_logs.get("drive", []))

    for line in reversed(logs):
        upper = line.upper()
        if "DEGRADED" in upper or "CRITICAL" in upper:
            return "degraded", line
        if "ANOMALY" in upper:
            return "warning", line
        if "HEALTHY" in upper:
            return "healthy", line
        if "BASELINE" in upper or "TRAINED" in upper or "MODEL" in upper:
            return "training", line
    return None, ""


def drive_log_alert_count():
    with process_lock:
        logs = list(component_logs.get("drive", []))

    return sum(
        1
        for line in logs
        if any(marker in line.upper() for marker in ("ANOMALY", "DEGRADED", "CRITICAL"))
    )


def format_drive_timestamp(value):
    timestamp = parse_time(value)
    if timestamp:
        return timestamp.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def drive_risk_from_smart_row(row, drive_type):
    temp = safe_int(row.get("Temperature"), 0)
    risk = 0
    reasons = []

    if drive_type == "hdd" or "Current_Pending_Sector" in row:
        reallocated = safe_float(row.get("Reallocated_Sector_Count"))
        pending = safe_float(row.get("Current_Pending_Sector"))
        uncorrectable = safe_float(row.get("Uncorrectable_Sector_Count"))

        if reallocated > 0:
            risk = max(risk, min(75, 35 + int(reallocated)))
            reasons.append(f"reallocated sectors {safe_int(reallocated)}")
        if pending > 0:
            risk = max(risk, 80)
            reasons.append(f"pending sectors {safe_int(pending)}")
        if uncorrectable > 0:
            risk = max(risk, 90)
            reasons.append(f"uncorrectable sectors {safe_int(uncorrectable)}")
    else:
        percentage_used = safe_float(row.get("Percentage_Used"))
        spare = safe_float(row.get("Available_Spare"), 100.0)
        reallocated = safe_float(row.get("Reallocated_Block_Count"))
        uncorrectable = safe_float(row.get("Uncorrectable_Error_Count"))

        if percentage_used > 0:
            risk = max(risk, min(70, int(percentage_used * 0.7)))
            reasons.append(f"wear {safe_int(percentage_used)}%")
        if spare <= 10:
            risk = max(risk, 80)
            reasons.append(f"spare {safe_int(spare)}%")
        elif spare <= 20:
            risk = max(risk, 55)
            reasons.append(f"spare {safe_int(spare)}%")
        if reallocated > 0:
            risk = max(risk, min(85, 65 + safe_int(reallocated)))
            reasons.append(f"reallocated blocks {safe_int(reallocated)}")
        if uncorrectable > 0:
            risk = max(risk, 90)
            reasons.append(f"uncorrectable errors {safe_int(uncorrectable)}")

    if temp >= 60:
        risk = max(risk, 85)
        reasons.append(f"temperature {temp}C")
    elif temp >= 50:
        risk = max(risk, 55)
        reasons.append(f"temperature {temp}C")
    elif temp >= 45:
        risk = max(risk, 25)

    return min(100, risk), temp, ", ".join(reasons)


def drive_smart_info():
    now = time.time()
    if now - drive_smart_cache["timestamp"] < 5:
        return drive_smart_cache["data"]

    unavailable = {
        "health_status": "Unavailable",
        "lifespan_left": None,
        "total_data_written_tb": None,
        "smart_temperature_celsius": None,
        "physical_errors": None,
        "unsafe_shutdowns": None,
    }
    try:
        result = subprocess.run(
            ["smartctl", "-j", "-a", "/dev/sda"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        data = json.loads(result.stdout)
        smart_status = data.get("smart_status") or {}
        nvme = data.get("nvme_smart_health_information_log")
        if isinstance(nvme, dict):
            percentage_used = safe_float(nvme.get("percentage_used"), 0)
            units_written = safe_float(nvme.get("data_units_written"), 0)
            unavailable = {
                "health_status": "PASSED" if smart_status.get("passed") else "FAILED",
                "lifespan_left": max(0, min(100, round(100 - percentage_used))),
                "total_data_written_tb": round((units_written * 512000) / 1_000_000_000_000, 1),
                "smart_temperature_celsius": safe_int(nvme.get("temperature"), 0),
                "physical_errors": safe_int(nvme.get("media_errors"), 0),
                "unsafe_shutdowns": safe_int(nvme.get("unsafe_shutdowns"), 0),
            }
        else:
            table = {
                item.get("name"): item.get("raw", {}).get("value", 0)
                for item in data.get("ata_smart_attributes", {}).get("table", [])
                if item.get("name")
            }
            if not table:
                raise ValueError("SMART data unavailable")
            physical_errors = safe_int(table.get("Offline_Uncorrectable", table.get("Reported_Uncorrectable", 0)))
            unavailable.update({
                "health_status": "PASSED" if smart_status.get("passed") else "FAILED",
                "smart_temperature_celsius": safe_int(table.get("Temperature_Celsius"), 0),
                "physical_errors": physical_errors,
            })
    except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError, subprocess.SubprocessError):
        pass

    drive_smart_cache["timestamp"] = now
    drive_smart_cache["data"] = unavailable
    return unavailable


def drive_rul_percent(row, drive_type, smart_info):
    rul_profiles = {
        "ssd": {
            "tracking": ("Percentage_Used", "Reallocated_Block_Count", "Uncorrectable_Error_Count"),
            "failure_limit": 150.0,
        },
        "hdd": {
            "tracking": ("Reallocated_Sector_Count", "Current_Pending_Sector", "Uncorrectable_Sector_Count"),
            "failure_limit": 120.0,
        },
    }
    profile = rul_profiles.get(drive_type, rul_profiles["ssd"])
    tracking_columns = profile["tracking"]
    if row and all(column in row for column in tracking_columns):
        if drive_type == "ssd":
            return max(0, min(100, round(100 - safe_float(row.get("Percentage_Used"), 0))))
        wear_metric = sum(safe_float(row.get(column)) for column in tracking_columns)
        return max(0, min(100, round(100 * (1 - wear_metric / profile["failure_limit"]))))
    return smart_info.get("lifespan_left")


def drive_summary():
    rows = read_csv_rows(FILES["drive_training"])
    latest = newest_row(rows)
    training_rows = row_count(FILES["drive_training"])
    target = get_training_target("drive")
    with process_lock:
        job = training_jobs.get("drive")
    drive_type = read_drive_type()
    detection_rows = read_csv_rows(FILES["drive_detection"]) + read_csv_rows(DRIVE_DEMO_DETECTION_FILE)
    if detection_rows:
        latest = max(detection_rows, key=lambda row: safe_float(row.get("Timestamp"), 0))
    log_status, log_note = latest_drive_log_state()
    risk_score, temperature_celsius, reason_text = drive_risk_from_smart_row(latest, drive_type) if latest else (0, 0, "")
    smart_info = drive_smart_info()
    smart_info["lifespan_left"] = drive_rul_percent(latest, drive_type, smart_info)

    if log_status == "degraded":
        status = "degraded"
        risk_score = max(risk_score, 90)
    elif log_status == "warning":
        status = "warning"
        risk_score = max(risk_score, 55)
    elif not rows or training_rows < target:
        status = "training"
    elif risk_score >= 80:
        status = "degraded"
    elif risk_score >= 45:
        status = "warning"
    elif log_status == "healthy":
        status = "healthy"
    else:
        status = "healthy"

    alert_count = drive_log_alert_count()
    if alert_count == 0 and status in ("warning", "degraded"):
        alert_count = 1
    progress_target = job["target"] if job else target

    return {
        "status": status,
        "risk_score": risk_score,
        "temperature_celsius": temperature_celsius,
        "smart_info": smart_info,
        "rul_percent": smart_info["lifespan_left"],
        "latest_timestamp": format_drive_timestamp(latest.get("Timestamp", "")),
        "checks": len(rows),
        "alerts": alert_count,
        "drive_type": drive_type,
        "note": log_note or reason_text,
        "training_rows": training_rows,
        "training_target": target,
        "training_percent": min(100, round((training_rows / progress_target) * 100, 1)),
        "training_status": job["action"] if job and training_rows < job["target"] else "ready",
        "training_job_target": job["target"] if job else target,
    }


def component_summary():
    global running_processes
    result = {}
    with process_lock:
        for name in COMPONENTS:
            if name in running_processes and running_processes[name].poll() is None:
                train_path = FILES.get(f"{name}_training")
                job = training_jobs.get(name)
                if job and train_path and row_count(train_path) < job["target"]:
                    result[name] = "training"
                else:
                    training_jobs.pop(name, None)
                    result[name] = "enabled"
            else:
                if name in running_processes:
                    del running_processes[name]
                training_jobs.pop(name, None)
                result[name] = "disabled"
    return result


def recent_alerts(limit=12):
    alerts = []

    for source, path in [
        ("keystroke", FILES["keystroke_detection"]),
        ("mouse", FILES["mouse_detection"]),
        ("network", FILES["network_detection"]),
    ]:
        rows = read_csv_rows(path)
        for row in rows:
            if str(row.get("prediction", "")).lower() == "anomaly":
                reasons = str(row.get("reasons", "")).strip()
                reason_str = f" - Reason: {reasons}" if reasons else ""
                alerts.append(
                    {
                        "timestamp": row.get("timestamp", ""),
                        "source": source,
                        "level": "warning",
                        "summary": f"{source.title()} anomaly score {safe_float(row.get('score')):.3f}{reason_str}",
                    }
                )

    drive = drive_summary()
    if drive["status"] in ("warning", "degraded"):
        note = str(drive.get("note", "")).strip()
        reason_str = f" - {note}" if note else ""
        alerts.append(
            {
                "timestamp": drive.get("latest_timestamp", ""),
                "source": "drive",
                "level": "critical" if drive["status"] == "degraded" else "warning",
                "summary": f"Drive risk {drive['risk_score']}/100{reason_str}",
            }
        )

    alerts.sort(key=lambda item: parse_time(item["timestamp"]) or datetime.min, reverse=True)
    return alerts[:limit]


activity_history = load_activity_history()


def build_summary():
    settings = read_settings()
    keystroke = behavior_summary("keystroke", FILES["keystroke_detection"], FILES["keystroke_training"])
    mouse = behavior_summary("mouse", FILES["mouse_detection"], FILES["mouse_training"])
    network = behavior_summary("network", FILES["network_detection"], FILES["network_training"])
    drive = drive_summary()
    components = component_summary()
    alerts = recent_alerts()
    insights = behavior_insights()

    risk_points = 0
    cfg = behavior_alert_settings()
    k_rows = read_csv_rows(FILES["keystroke_detection"])
    m_rows = read_csv_rows(FILES["mouse_detection"])

    fusion = evaluate_combined_behavior_alert(k_rows, m_rows, cfg) if k_rows and m_rows else None
    solo_keystroke = None
    solo_mouse = None
    if k_rows and not (m_rows and other_modality_blocks_solo(m_rows, cfg)):
        solo_keystroke = evaluate_solo_behavior_alert(k_rows, "keystroke", cfg)

    is_online = is_user_present()
    battery_level = battery_percent()

    if m_rows and not (k_rows and other_modality_blocks_solo(k_rows, cfg)):
        solo_mouse = evaluate_solo_behavior_alert(m_rows, "mouse", cfg)

    behavior_risk = 0
    if fusion:
        behavior_risk = 40
    elif solo_keystroke or solo_mouse:
        behavior_risk = 28
    elif keystroke["status"] == "anomaly" and mouse["status"] == "anomaly":
        behavior_risk = 15

    risk_points += behavior_risk
                                                          
    behavior_alerts_count = sum(1 for a in alerts if a["source"] in ("keystroke", "mouse"))
    risk_points += min(60, behavior_alerts_count * 5)
    risk_score = min(100, risk_points)
    monitored_components = ("keystroke", "mouse", "network", "drive", "remote")
    telegram_enabled = bool(settings.get("telegram_alerts_enabled", False))
    sentry_online = telegram_enabled or any(
        components.get(name) in ("enabled", "training", "retraining")
        for name in monitored_components
    )

    if risk_score >= 25:
        overall = "anomaly"
    else:
        overall = "normal"

    return {
        "username": username(),
        "supervisor_name": str(settings.get("supervisor_name", "SUPERVISOR")).strip() or "SUPERVISOR",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "overall": {
            "risk": overall,
            "risk_score": risk_score,
            "telegram_alerts": bool(settings.get("telegram_alerts_enabled", False)),
            "telegram_commands_allow_all": bool(settings.get("telegram_commands_allow_all", False)),
            "online": sentry_online,
        },
        "components": components,
        "user_status": {
            "active": is_online,
            "uptime": insights["system_uptime"],
            "dashboard_uptime": insights["dashboard_uptime"],
            "battery_percent": battery_level,
        },
        "behavioral_insights": insights,
        "keystroke": keystroke,
        "mouse": mouse,
        "network": network,
        "drive": drive,
        "behavior_alerts": cfg,
        "behavior_fusion": fusion,
        "behavior_solo": {"keystroke": solo_keystroke, "mouse": solo_mouse},
        "alerts": alerts,
        "activity_history": list(activity_history),
        "user_logs": load_user_logs(),
        "privacy": {
            "typed_characters_sent": False,
            "mouse_coordinates_sent": False,
            "window_titles_sent": False,
            "raw_drive_serial_sent": False,
        },
    }


def authorized(handler):
    header_token = handler.headers.get("X-Sentry-Token", "")
    if header_token == TOKEN:
        return True

    cookie = SimpleCookie()
    cookie.load(handler.headers.get("Cookie", ""))
    session_cookie = cookie.get("sentry_session")
    if session_cookie is None:
        return False

    now = datetime.now()
    expired = [session_id for session_id, expiry in sessions.items() if expiry <= now]
    for session_id in expired:
        sessions.pop(session_id, None)

    session_id = session_cookie.value
    expiry = sessions.get(session_id)
    if expiry is None or expiry <= now:
        sessions.pop(session_id, None)
        return False
    return True


def load_dashboard_html(monitored_user=None):
    try:
        html_text = DASHBOARD_HTML_FILE.read_text(encoding="utf-8")
        if monitored_user is not None:
            html_text = html_text.replace(
                "{{MONITORED_USER}}", html.escape(str(monitored_user))
            )
        return html_text
    except OSError:
        return """<!doctype html>
<html><body><h1>Dashboard file missing</h1></body></html>
"""


def save_report_file(content, filename):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / filename
    try:
        report_path.write_bytes(content)
    except OSError:
        pass
    return report_path


def read_json_body(handler):
    try:
        content_length = int(handler.headers.get("Content-Length", 0))
    except ValueError:
        content_length = 0

    body = handler.rfile.read(max(0, content_length))
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


class SupervisorHandler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def clear_session_cookie(self):
        self.send_header("Set-Cookie", "sentry_session=; Max-Age=0; HttpOnly; Path=/; SameSite=Lax")

    def send_pdf(self, content, filename):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self.send_json({"ok": True})
            return

        if parsed.path.startswith("/assets/"):
            asset_path = (BASE_DIR / parsed.path.lstrip("/")).resolve()
            try:
                if ASSETS_DIR.resolve() not in asset_path.parents and asset_path != ASSETS_DIR.resolve():
                    raise ValueError("invalid asset path")
            except Exception:
                self.send_json({"error": "not found"}, 404)
                return

            if asset_path.exists() and asset_path.is_file():
                content_type = mimetypes.guess_type(str(asset_path))[0] or "application/octet-stream"
                body = asset_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            self.send_json({"error": "not found"}, 404)
            return

        if parsed.path == "/login":
            if authorized(self):
                self.send_response(302)
                self.send_header("Location", "/")
                self.end_headers()
            else:
                self.send_html(load_dashboard_html(monitored_user=username()))
            return

        if parsed.path == "/api/report":
            if not authorized(self):
                self.send_html("<p>Unauthorized</p>", 401)
                return
            report = build_anomaly_report_pdf(build_summary())
            filename = f"Sentry-Report-{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            save_report_file(report, filename)
            self.send_pdf(report, filename)
            return

        if not authorized(self):
            self.send_html("<p>Unauthorized</p>", 401)
            return

        if parsed.path in ("/", "/index.html"):
            self.send_html(load_dashboard_html())
            return

        if parsed.path == "/api/summary":
            self.send_json(build_summary())
            return

        if parsed.path == "/api/logs":
            self.send_json(component_logs)
            return

        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/login":
            client_ip = self.client_address[0]
            now = datetime.now()
            attempt = failed_login_attempts.get(client_ip, {"count": 0, "locked_until": None})
            locked_until = attempt.get("locked_until")
            if locked_until and locked_until > now:
                self.send_response(303)
                self.send_header("Location", "/login?locked=1")
                self.end_headers()
                return

            try:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
                password = parse_qs(body).get("password", [""])[0]
            except (ValueError, UnicodeDecodeError):
                password = ""

            if verify_dashboard_password(password):
                session_id = secrets.token_urlsafe(32)
                sessions[session_id] = now + timedelta(hours=8)
                failed_login_attempts.pop(client_ip, None)
                self.send_response(302)
                self.send_header("Set-Cookie", f"sentry_session={session_id}; HttpOnly; Path=/; SameSite=Lax")
                self.send_header("Location", "/")
                self.end_headers()
            else:
                attempt["count"] += 1
                if attempt["count"] >= 3:
                    attempt["locked_until"] = now + timedelta(minutes=5)
                failed_login_attempts[client_ip] = attempt
                self.send_response(303)
                location = "/login?locked=1" if attempt["locked_until"] else "/login?error=1"
                self.send_header("Location", location)
                self.end_headers()
            return

        if not authorized(self):
            self.send_json({"error": "unauthorized"}, 401)
            return

        if parsed.path == "/api/logout":
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            session_cookie = cookie.get("sentry_session")
            if session_cookie:
                sessions.pop(session_cookie.value, None)
            self.send_response(204)
            self.clear_session_cookie()
            self.end_headers()
            return

        if parsed.path == "/api/supervisor":
            try:
                data = read_json_body(self)
                supervisor_name = str(data.get("name", "")).strip()
                new_password = str(data.get("password", ""))
                if not supervisor_name or not new_password:
                    self.send_json({"error": "name and password are required"}, 400)
                    return

                settings = read_settings()
                salt = secrets.token_bytes(16)
                password_hash = hashlib.pbkdf2_hmac(
                    "sha256", new_password.encode("utf-8"), salt, 100000
                )
                settings["supervisor_name"] = supervisor_name
                settings["dashboard_auth"] = {
                    "salt": salt.hex(),
                    "hash": password_hash.hex(),
                }
                write_settings(settings)
                self.send_json({"status": "ok", "supervisor_name": supervisor_name})
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
            return

        if parsed.path == "/api/control":
            try:
                data = read_json_body(self)
                component = data.get("component")
                action = data.get("action")

                if component in COMPONENTS and action in ("start", "stop"):
                    control_component(component, action)
                    self.send_json({"status": "ok"})
                else:
                    self.send_json({"error": "invalid component or action"}, 400)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
            return

        if parsed.path == "/api/control-all":
            try:
                data = read_json_body(self)
                action = data.get("action")

                if action in ("start", "stop"):
                    for component in COMPONENTS:
                        control_component(component, action)
                    self.send_json({"status": "ok"})
                else:
                    self.send_json({"error": "invalid action"}, 400)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
            return

        if parsed.path == "/api/settings":
            try:
                data = read_json_body(self)
                settings = read_settings()

                if "telegram_alerts_enabled" in data:
                    settings["telegram_alerts_enabled"] = bool(data["telegram_alerts_enabled"])
                    write_settings(settings)
                    self.send_json({"status": "ok", "settings": settings})
                elif "telegram_commands_allow_all" in data:
                    settings["telegram_commands_allow_all"] = bool(data["telegram_commands_allow_all"])
                    write_settings(settings)
                    self.send_json({"status": "ok", "settings": settings})
                else:
                    self.send_json({"error": "no supported settings provided"}, 400)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
            return

        if parsed.path == "/api/shutdown":
            self.send_json({"status": "shutting_down"})
            Thread(target=self.shutdown_server, daemon=True).start()
            return

        if parsed.path == "/api/delete-anomalies":
            try:
                delete_all_anomaly_detections()
                self.send_json({"status": "ok"})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        if parsed.path == "/api/delete-log-dirs":
            try:
                delete_log_directories()
                self.send_json({"status": "ok"})
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        path = parsed.path.rstrip("/")

        if path == "/api/train":
            try:
                data = read_json_body(self)
                component = data.get("component")
                target_rows = parse_training_target(data)
                if component in RETRAIN_ARTIFACTS and component != "drive":
                    train_component(component, target_rows)
                    self.send_json({"status": "ok", "component": component, "target_rows": target_rows})
                else:
                    self.send_json({"error": "invalid component"}, 400)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        if path == "/api/retrain":
            try:
                data = read_json_body(self)
                component = data.get("component")
                target_rows = parse_training_target(data)
                if component in RETRAIN_ARTIFACTS:
                    retrain_component(component, target_rows)
                    self.send_json({"status": "ok", "component": component, "target_rows": target_rows})
                else:
                    self.send_json({"error": "invalid component"}, 400)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                self.send_json({"error": str(exc)}, 500)
            return

        self.send_json({"error": "not found"}, 404)

    def log_message(self, format, *args):
        return

    def shutdown_server(self):
        time.sleep(0.2)
        stop_all_components()
        self.server.shutdown()


def append_component_log(component, line):
    line = line.strip()
    if not line:
        return

    with process_lock:
        if component not in component_logs:
            component_logs[component] = []
        if len(component_logs[component]) > 150:
            component_logs[component] = component_logs[component][-75:]
        component_logs[component].append(line)

        if component == "remote":
            parsed = parse_activity_line(line)
            if parsed:
                entry = {
                    "timestamp": str(parsed.get("timestamp") or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "command": str(parsed.get("command") or "").strip(),
                    "feedback": str(parsed.get("feedback") or "").strip(),
                }
                if entry["command"] or entry["feedback"]:
                    activity_history.append(entry)
                    activity_history[:] = prune_activity_entries(activity_history, days=7)
                    save_activity_history(activity_history)


def start_component(component, extra_env=None):
    script_path = COMPONENTS[component]
    if not script_path.exists():
        message = f"Skipping {component}: missing file {script_path}"
        print(message, flush=True)
        append_component_log(component, message)
        return None

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["SENTRY_DASHBOARD_TOKEN"] = TOKEN
    if extra_env:
        for env_key, env_value in extra_env.items():
            env[str(env_key)] = str(env_value)

    process = subprocess.Popen(
        [sys.executable, "-u", str(script_path)],
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
    )

    with process_lock:
        running_processes[component] = process
        if component not in component_logs:
            component_logs[component] = []
        expected_stops.discard(component)

    Thread(target=stream_process_output, args=(component, process), daemon=True).start()
    message = f"Started {component} (PID {process.pid})"
    print(message, flush=True)
    append_component_log(component, message)
    
    try:
        append_activity_line(f"start {component}", message, source="system")
        line = make_activity_line(f"start {component}", message, source="system")
        parsed = parse_activity_line(line)
        if parsed:
            activity_history.append({
                "timestamp": str(parsed.get("timestamp") or "").strip(),
                "command": str(parsed.get("command") or "").strip(),
                "feedback": str(parsed.get("feedback") or "").strip(),
                "source": str(parsed.get("source") or "").strip(),
            })
            activity_history[:] = prune_activity_entries(activity_history, days=7)
    except Exception:
        pass
    return process


def control_component(component, action):
    if component not in COMPONENTS:
        return

    if action == "start":
        with process_lock:
            process = running_processes.get(component)
            if process is not None and process.poll() is None:
                return
            if process is not None:
                del running_processes[component]
        start_component(component)
        return

    if action == "stop":
        with process_lock:
            process = running_processes.get(component)
        if process is not None:
            stop_components([(component, process)])


def stream_process_output(component, process):
    try:
        for line in process.stdout:
            labelled = line.rstrip()
            append_component_log(component, labelled)
    except Exception as exc:
        append_component_log(component, f"Log stopped: {exc}")
    finally:
        return_code = process.wait()
        with process_lock:
            expected = shutdown_requested or component in expected_stops
            if running_processes.get(component) is process:
                del running_processes[component]
            expected_stops.discard(component)
        if not expected:
            message = f"{component} stopped {return_code}."
            print(message, flush=True)
            append_component_log(component, message)


def send_combined_behavior_alert(fusion, cfg):
    global last_behavior_alert_at, last_solo_alert_at
    msg = (
        "Correlated behavior anomaly\n"
        f"Combined intensity: {fusion['intensity']:.3f}\n"
        f"Keystroke peak score: {fusion['keystroke_peak']:.3f} "
        f"({fusion['keystroke_hits']} hit(s) in window)\n"
        f"Mouse peak score: {fusion['mouse_peak']:.3f} "
        f"({fusion['mouse_hits']} hit(s) in window)\n"
        f"Window: {fusion['window_seconds']}s\n"
        "Both typing and mouse patterns deviated from baseline."
    )
    send_telegram_alert("behavior", msg)
    now = time.time()
    last_behavior_alert_at = now
    last_solo_alert_at["keystroke"] = now
    last_solo_alert_at["mouse"] = now
    print(f"alert sent (intensity {fusion['intensity']:.3f})", flush=True)


def send_solo_behavior_alert(solo):
    global last_solo_alert_at
    source = solo["source"]
    trigger_labels = {
        "high_score": "Very high anomaly score",
        "streak": "Continuous anomaly streak",
        "sustained": "Sustained anomalies in window",
    }
    msg = (
        f"{trigger_labels.get(solo['trigger'], 'Anomaly')}\n"
        f"Peak score: {solo['peak']:.3f}\n"
        f"Hits in window: {solo['hits']}\n"
        f"Latest streak: {solo['streak']}\n"
        f"Window: {solo['window_seconds']}s\n"
        "Only this input channel triggered."
    )
    send_telegram_alert(source, msg)
    last_solo_alert_at[source] = time.time()
    print(f"[{source}] alert sent ({solo['trigger']}, peak {solo['peak']:.3f})", flush=True)


def monitor_behavior_alerts():
    
    global last_behavior_alert_at, last_solo_alert_at
    while not shutdown_requested:
        try:
            cfg = behavior_alert_settings()
            k_rows = read_csv_rows(FILES["keystroke_detection"])
            m_rows = read_csv_rows(FILES["mouse_detection"])

            if not k_rows and not m_rows:
                time.sleep(10)
                continue

            fusion = None
            if k_rows and m_rows:
                fusion = evaluate_combined_behavior_alert(k_rows, m_rows, cfg)

            if fusion and (time.time() - last_behavior_alert_at) >= cfg["cooldown_seconds"]:
                send_combined_behavior_alert(fusion, cfg)
                time.sleep(5)
                continue

            for source, own_rows, other_rows in (
                ("keystroke", k_rows, m_rows),
                ("mouse", m_rows, k_rows),
            ):
                if not own_rows:
                    continue
                if (time.time() - last_solo_alert_at[source]) < cfg["solo_cooldown_seconds"]:
                    continue
                if other_rows and other_modality_blocks_solo(other_rows, cfg):
                    continue

                solo = evaluate_solo_behavior_alert(own_rows, source, cfg)
                if solo:
                    send_solo_behavior_alert(solo)
        except Exception as exc:
            print(f"Alert monitor error: {exc}", flush=True)
        time.sleep(5)


def start_all_components():
    for component in COMPONENTS:
        control_component(component, "start")


def stop_components(processes):
    if not processes:
        return

    with process_lock:
        for component, process in processes:
            expected_stops.add(component)

    for component, process in processes:
        if process.poll() is None:
            message = f"Stopped {component}"
            print(message, flush=True)
            append_component_log(component, message)
            try:
                append_activity_line(f"stop {component}", message, source="system")
                line = make_activity_line(f"stop {component}", message, source="system")
                parsed = parse_activity_line(line)
                if parsed:
                    activity_history.append({
                        "timestamp": str(parsed.get("timestamp") or "").strip(),
                        "command": str(parsed.get("command") or "").strip(),
                        "feedback": str(parsed.get("feedback") or "").strip(),
                        "source": str(parsed.get("source") or "").strip(),
                    })
                    activity_history[:] = prune_activity_entries(activity_history, days=7)
            except Exception:
                pass
            try:
                process.terminate()
            except Exception as exc:
                append_component_log(component, f"Could not stop {component}: {exc}")

    deadline = time.time() + 8
    for component, process in processes:
        if process.poll() is not None:
            continue

        remaining = max(0, deadline - time.time())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            message = f"Stopped {component}"
            print(message, flush=True)
            append_component_log(component, message)
            try:
                append_activity_line(f"force_stop {component}", message, source="system")
                line = make_activity_line(f"force_stop {component}", message, source="system")
                parsed = parse_activity_line(line)
                if parsed:
                    activity_history.append({
                        "timestamp": str(parsed.get("timestamp") or "").strip(),
                        "command": str(parsed.get("command") or "").strip(),
                        "feedback": str(parsed.get("feedback") or "").strip(),
                        "source": str(parsed.get("source") or "").strip(),
                    })
                    activity_history[:] = prune_activity_entries(activity_history, days=7)
            except Exception:
                pass
            process.kill()
            process.wait()

    with process_lock:
        for component, process in processes:
            if running_processes.get(component) is process:
                del running_processes[component]
            expected_stops.discard(component)


def stop_all_components():
    global shutdown_requested
    shutdown_requested = True
    with process_lock:
        processes = list(running_processes.items())
    stop_components(processes)
    encrypt_sensitive_files()


def launch_system_tray(url):
    env = os.environ.copy()

    env["SENTRY_DASHBOARD_TOKEN"] = TOKEN
    try:
        proc = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "system_tray.pyw")],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        with process_lock:
            running_processes["system_tray"] = proc
    except Exception as exc:
        print(f"system tray error: {exc}")


def send_dashboard_url(network_url):
    message = (
        "Dashboard is active\n"
        f"Local network URL: {network_url}\n\n"
    )
    send_telegram_alert("dashboard", message, force=True)


def install_shutdown_handler():
    if os.name != "nt":
        return

    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32

        def handler(dwCtrlType, hook_sig=None, hook_ctx=None, event=None):
            try:
                encrypt_sensitive_files()
            except Exception:
                pass
            return 0

        kernel32.SetConsoleCtrlHandler(handler, 1)
    except Exception:
        pass


def main():
    initialize_dashboard_auth()
    decrypt_sensitive_files()
    settings = read_settings()
    settings["telegram_alerts_enabled"] = True
    write_settings(settings)
    port = PORT
    if len(sys.argv) > 1:
        port = safe_int(sys.argv[1], PORT)

    server = ThreadingHTTPServer((HOST, port), SupervisorHandler)
    network_url = f"http://{local_ip_address()}:{port}/login"
    install_shutdown_handler()

    if AUTO_START_COMPONENTS == True:
        start_all_components()
    else:
        pass

    try:
        start_component('activity')
    except Exception:
        pass

    try:
        start_component("remote")
    except Exception:
        pass

    Thread(target=monitor_behavior_alerts, daemon=True).start()
    launch_system_tray(network_url)
    send_dashboard_url(network_url)

    print("Sentry is active.")
    print(f"Local network dashboard: {network_url}")

    try:
        server.serve_forever()
    finally:
        stop_all_components()


if __name__ == "__main__":
    main()
