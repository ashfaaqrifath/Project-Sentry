import csv
import ctypes
import hashlib
import json
import os
import secrets
import socket
import sys
import subprocess
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from threading import Lock, Thread

from dotenv import load_dotenv
from telegram_alert import send_telegram_alert


load_dotenv()


BASE_DIR = Path(__file__).resolve().parent
HOST = "0.0.0.0"
PORT = 8765
SETTINGS_FILE = BASE_DIR / "settings.json"
LEGACY_SETTINGS_FILE = BASE_DIR / "sentry_settings.json"
DASHBOARD_HTML_FILE = BASE_DIR / "dashboard.html"

# Change this to True later if you want every monitoring component to start with the dashboard.
AUTO_START_COMPONENTS = False

FILES = {
    "keystroke_training": BASE_DIR / "keystroke dynamics" / "typing_training_features.csv",
    "keystroke_detection": BASE_DIR / "keystroke dynamics" / "typing_anomaly_detection.csv",
    "mouse_training": BASE_DIR / "mouse dynamics" / "mouse_training_features.csv",
    "mouse_detection": BASE_DIR / "mouse dynamics" / "mouse_anomaly_detection.csv",
    "drive_training": BASE_DIR / "drive health" / "drive_training_features.csv",
    "drive_history": BASE_DIR / "drive health" / "drive_health_history.csv",
    "drive_alerts": BASE_DIR / "drive health" / "drive_health_alerts.csv",
}

TRAINING_TARGETS = {
    "keystroke": 1000,
    "mouse": 1000,
    "drive": 200,
}

COMPONENTS = {
    "keystroke": BASE_DIR / "keystroke dynamics" / "keystroke_dynamics.py",
    "mouse": BASE_DIR / "mouse dynamics" / "mouse_dynamics.py",
    "drive": BASE_DIR / "drive health" / "drive_health_prediction.py",
    "remote": BASE_DIR / "remote control" / "remote_control.py",
}

running_processes = {}
process_lock = Lock()
component_logs = {name: [] for name in COMPONENTS}
SERVER_STARTED_AT = time.time()
shutdown_requested = False
expected_stops = set()


def get_token():
    token = os.getenv("SENTRY_DASHBOARD_TOKEN", "")
    if token:
        return token
    
    token = secrets.token_urlsafe(24)
    return token


TOKEN = get_token()


def device_id():
    raw = socket.gethostname().encode("utf-8", errors="ignore")
    digest = hashlib.sha256(raw).hexdigest()[:10].upper()
    return f"SENTRY-{digest}"


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


def write_settings(settings):
    for path in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
        try:
            path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"Could not update {path.name}: {exc}", flush=True)


def read_csv_rows(path):
    if not path.exists():
        return []

    try:
        with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
            return list(csv.DictReader(csv_file))
    except (OSError, csv.Error, UnicodeDecodeError):
        return []


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


def newest_timestamp(*row_groups):
    latest = None
    for rows in row_groups:
        for row in rows:
            timestamp = parse_time(row.get("timestamp"))
            if timestamp is not None and (latest is None or timestamp > latest):
                latest = timestamp
    return latest


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

    try:
        with open("/proc/uptime", "r", encoding="utf-8") as uptime_file:
            return format_duration(float(uptime_file.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return "unknown"


def numeric_values(rows, column):
    values = []
    for row in rows:
        value = row.get(column)
        if value not in (None, ""):
            values.append(safe_float(value))
    return values


def average(values):
    return sum(values) / len(values) if values else 0.0


def baseline_shift(latest, baseline_rows, feature_columns):
    if not latest or not baseline_rows:
        return None

    shifts = []
    for column in feature_columns:
        baseline = numeric_values(baseline_rows, column)
        if len(baseline) < 5:
            continue
        baseline_mean = average(baseline)
        latest_value = safe_float(latest.get(column), None)
        if latest_value is None:
            continue
        scale = abs(baseline_mean) if abs(baseline_mean) > 0.001 else 1.0
        shifts.append(abs(latest_value - baseline_mean) / scale)

    if not shifts:
        return None
    return min(100, round(average(shifts) * 100, 1))


def typing_speed_trend(rows):
    today = datetime.now().date()
    trend = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        speeds = []
        for row in rows:
            timestamp = parse_time(row.get("timestamp"))
            if timestamp and timestamp.date() == day:
                speeds.append(safe_float(row.get("key_press_rate"), 0.0))
        trend.append(
            {
                "date": day.strftime("%a"),
                "speed": round(average(speeds), 2) if speeds else 0,
                "samples": len(speeds),
            }
        )
    return trend


def mouse_smoothness(latest_mouse):
    if not latest_mouse:
        return {"score": 0, "label": "No live data"}

    straightness = max(0, min(1, safe_float(latest_mouse.get("straightness"), 0)))
    direction_change = max(0, safe_float(latest_mouse.get("direction_change_mean"), 0))
    direction_score = max(0, 1 - min(direction_change / 90, 1))
    speed_variation = safe_float(latest_mouse.get("speed_std"), 0) / max(safe_float(latest_mouse.get("speed_mean"), 1), 1)
    speed_score = max(0, 1 - min(speed_variation, 1))
    score = round(((straightness * 0.45) + (direction_score * 0.35) + (speed_score * 0.20)) * 100)

    if score >= 70:
        label = "Smooth"
    elif score >= 40:
        label = "Moderate"
    else:
        label = "Erratic"
    return {"score": score, "label": label}


def behavior_insights():
    key_detection = read_csv_rows(FILES["keystroke_detection"])
    mouse_detection = read_csv_rows(FILES["mouse_detection"])
    drive_history = read_csv_rows(FILES["drive_history"])
    key_training = read_csv_rows(FILES["keystroke_training"])
    mouse_training = read_csv_rows(FILES["mouse_training"])

    latest_key = newest_row(key_detection)
    latest_mouse = newest_row(mouse_detection)
    latest_activity = newest_timestamp(key_detection, mouse_detection, drive_history)

    key_shift = baseline_shift(
        latest_key,
        key_training,
        ["key_press_rate", "dwell_mean", "flight_mean", "pause_count", "pause_mean", "correction_key_ratio"],
    )
    mouse_shift = baseline_shift(
        latest_mouse,
        mouse_training,
        ["straightness", "speed_mean", "speed_std", "acceleration_mean", "direction_change_mean"],
    )
    behavior_shifts = [value for value in [key_shift, mouse_shift] if value is not None]
    behavior_shift = round(average(behavior_shifts), 1) if behavior_shifts else 0

    fatigue_score = min(100, round((behavior_shift * 0.7) + (count_recent_anomalies(key_detection + mouse_detection, 24) * 6), 1))
    if fatigue_score >= 65:
        fatigue_label = "High"
    elif fatigue_score >= 35:
        fatigue_label = "Elevated"
    else:
        fatigue_label = "Low"

    return {
        "typing_speed_trend": typing_speed_trend(key_detection),
        "mouse_smoothness": mouse_smoothness(latest_mouse),
        "fatigue_stress": {"score": fatigue_score, "label": fatigue_label},
        "latest_activity": latest_activity.strftime("%Y-%m-%d %H:%M:%S") if latest_activity else "",
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


def behavior_summary(name, detection_path, training_path):
    rows = read_csv_rows(detection_path)
    latest = newest_row(rows)
    prediction = str(latest.get("prediction", "training" if not rows else "unknown")).lower()
    score = safe_float(latest.get("score"), None)
    training_rows = row_count(training_path)
    target = TRAINING_TARGETS[name]

    if score is None:
        score_text = "N/A"
    else:
        score_text = f"{score:.3f}"

    return {
        "status": prediction,
        "score": score,
        "score_text": score_text,
        "latest_timestamp": latest.get("timestamp", ""),
        "detections": len(rows),
        "anomalies_24h": count_recent_anomalies(rows),
        "training_rows": training_rows,
        "training_target": target,
        "training_percent": min(100, round((training_rows / target) * 100, 1)),
    }


def drive_summary():
    history = read_csv_rows(FILES["drive_history"])
    latest = newest_row(history)
    training_rows = row_count(FILES["drive_training"])
    target = TRAINING_TARGETS["drive"]

    return {
        "status": str(latest.get("risk_level", "training" if not history else "unknown")).lower(),
        "risk_score": safe_int(latest.get("risk_score"), 0),
        "temperature_celsius": safe_int(latest.get("temperature_celsius"), 0),
        "latest_timestamp": latest.get("timestamp", ""),
        "checks": len(history),
        "alerts": row_count(FILES["drive_alerts"]),
        "training_rows": training_rows,
        "training_target": target,
        "training_percent": min(100, round((training_rows / target) * 100, 1)),
    }


def component_summary():
    global running_processes
    result = {}
    with process_lock:
        for name in COMPONENTS:
            if name in running_processes and running_processes[name].poll() is None:
                result[name] = "enabled"
            else:
                if name in running_processes:
                    del running_processes[name]
                result[name] = "disabled"
    return result


def recent_alerts(limit=12):
    alerts = []

    for source, path in [
        ("keystroke", FILES["keystroke_detection"]),
        ("mouse", FILES["mouse_detection"]),
    ]:
        rows = read_csv_rows(path)
        for row in rows:
            if str(row.get("prediction", "")).lower() == "anomaly":
                alerts.append(
                    {
                        "timestamp": row.get("timestamp", ""),
                        "source": source,
                        "level": "warning",
                        "summary": f"{source.title()} anomaly score {safe_float(row.get('score')):.3f}",
                    }
                )

    for row in read_csv_rows(FILES["drive_alerts"]):
        alerts.append(
            {
                "timestamp": row.get("timestamp", ""),
                "source": "drive",
                "level": str(row.get("risk_level", "warning")).lower(),
                "summary": f"Drive risk {safe_int(row.get('risk_score'))}/100",
            }
        )

    alerts.sort(key=lambda item: parse_time(item["timestamp"]) or datetime.min, reverse=True)
    return alerts[:limit]


def build_summary():
    settings = read_settings()
    keystroke = behavior_summary("keystroke", FILES["keystroke_detection"], FILES["keystroke_training"])
    mouse = behavior_summary("mouse", FILES["mouse_detection"], FILES["mouse_training"])
    drive = drive_summary()
    components = component_summary()
    alerts = recent_alerts()
    insights = behavior_insights()

    risk_points = 0
    risk_points += 20 if keystroke["status"] == "anomaly" else 0
    risk_points += 20 if mouse["status"] == "anomaly" else 0
    risk_points += min(40, drive["risk_score"])
    risk_points += min(20, len(alerts) * 3)
    risk_score = min(100, risk_points)

    if risk_score >= 60:
        overall = "high"
    elif risk_score >= 25:
        overall = "elevated"
    else:
        overall = "low"

    return {
        "device_id": device_id(),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall": {
            "risk": overall,
            "risk_score": risk_score,
            "telegram_alerts": bool(settings.get("telegram_alerts_enabled", False)),
        },
        "components": components,
        "user_status": {
            "online": any(state == "enabled" for state in components.values()),
            "last_activity_time": insights["latest_activity"],
            "system_uptime": insights["system_uptime"],
            "dashboard_uptime": insights["dashboard_uptime"],
        },
        "behavioral_insights": insights,
        "keystroke": keystroke,
        "mouse": mouse,
        "drive": drive,
        "alerts": alerts,
        "privacy": {
            "typed_characters_sent": False,
            "mouse_coordinates_sent": False,
            "screenshots_sent": False,
            "window_titles_sent": False,
            "raw_drive_serial_sent": False,
        },
    }


def authorized(handler):
    parsed = urlparse(handler.path)
    query_token = parse_qs(parsed.query).get("token", [""])[0]
    header_token = handler.headers.get("X-Sentry-Token", "")
    return query_token == TOKEN or header_token == TOKEN


def load_dashboard_html():
    try:
        return DASHBOARD_HTML_FILE.read_text(encoding="utf-8")
    except OSError:
        return """<!doctype html>
<html><body><h1>Dashboard file missing</h1><p>dashboard.html could not be loaded.</p></body></html>
"""


def read_json_body(handler):
    try:
        content_length = int(handler.headers.get("Content-Length", 0))
    except ValueError:
        content_length = 0

    body = handler.rfile.read(max(0, content_length))
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


class OverseerHandler(BaseHTTPRequestHandler):
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

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self.send_json({"ok": True})
            return

        if not authorized(self):
            self.send_html("<h1>Sentry Monitoring Dashboard</h1><p>Unauthorized. Add the dashboard token to the URL.</p>", 401)
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

        if not authorized(self):
            self.send_json({"error": "unauthorized"}, 401)
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
                else:
                    self.send_json({"error": "no supported settings provided"}, 400)
            except json.JSONDecodeError:
                self.send_json({"error": "invalid json"}, 400)
            return

        if parsed.path == "/api/shutdown":
            self.send_json({"status": "shutting_down"})
            Thread(target=self.shutdown_server, daemon=True).start()
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


def start_component(component):
    script_path = COMPONENTS[component]
    if not script_path.exists():
        message = f"Skipping {component}: missing file {script_path}"
        print(message, flush=True)
        append_component_log(component, message)
        return None

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

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
            labelled = f"[{component}] {line.rstrip()}"
            append_component_log(component, labelled)
    except Exception as exc:
        append_component_log(component, f"Log stream stopped: {exc}")
    finally:
        return_code = process.wait()
        with process_lock:
            expected = shutdown_requested or component in expected_stops
            if running_processes.get(component) is process:
                del running_processes[component]
            expected_stops.discard(component)
        if not expected:
            message = f"{component} stopped unexpectedly with exit code {return_code}."
            print(message, flush=True)
            append_component_log(component, message)


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
            message = f"Stopping {component}..."
            print(message, flush=True)
            append_component_log(component, message)
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
            message = f"Force stopping {component}..."
            print(message, flush=True)
            append_component_log(component, message)
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


def launch_system_tray(local_url):
    env = os.environ.copy()
    env["SENTRY_DASHBOARD_URL"] = local_url
    env["SENTRY_DASHBOARD_TOKEN"] = TOKEN
    try:
        subprocess.Popen(
            [sys.executable, str(BASE_DIR / "system_tray.pyw")],
            cwd=str(BASE_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
    except Exception as exc:
        print(f"Could not start system tray: {exc}")


# def send_dashboard_url(network_url):
#     message = (
#         "Overseer dashboard is online\n"
#         f"URL: {network_url}\n\n"
#         "Use this on the same Wi-Fi network."
#     )
#     send_telegram_alert("dashboard", message, force=True)


def main():
    port = PORT
    if len(sys.argv) > 1:
        port = safe_int(sys.argv[1], PORT)

    server = ThreadingHTTPServer((HOST, port), OverseerHandler)
    local_base_url = f"http://127.0.0.1:{port}"
    local_url = f"{local_base_url}/?token={TOKEN}"
    network_url = f"http://{local_ip_address()}:{port}/?token={TOKEN}"

    if AUTO_START_COMPONENTS == True:
        start_all_components()
    else:
        print("Components are inactive. Start them from the dashboard when needed.", flush=True)
    launch_system_tray(local_base_url)
    #send_dashboard_url(network_url)

    print("Sentry is active.")
    print(f"Local dashboard: {local_url}")
    print(f"Local network dashboard: {network_url}")
    print("")
    print("System tray started.")
    
    try:
        server.serve_forever()
    finally:
        stop_all_components()


if __name__ == "__main__":
    main()
