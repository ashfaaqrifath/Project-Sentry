import json
import os
import subprocess
import sys
import tempfile
import time

import joblib
import pandas as pd
from sklearn.ensemble import IsolationForest


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.append(PROJECT_DIR)

from telegram_alert import send_drive_alert as send_drive_telegram_alert


HISTORY_FILE = os.path.join(BASE_DIR, "drive_health_history.csv")
TRAINING_DATA_FILE = os.path.join(BASE_DIR, "drive_training_features.csv")
ALERT_FILE = os.path.join(BASE_DIR, "drive_health_alerts.csv")
MODEL_FILE = os.path.join(BASE_DIR, "drive_health_model.pkl")
SIMULATION_FILE = os.path.join(tempfile.gettempdir(), "sentry_drive_simulation.json")

CHECK_INTERVAL_SECONDS = 5
MIN_BASELINE_SNAPSHOTS = 200
MODEL_CONTAMINATION = 0.05
ALERT_COOLDOWN_SECONDS = 300

SMART_ATTRIBUTE_IDS = {
    5: "reallocated_sector_count",
    9: "power_on_hours",
    12: "power_cycle_count",
    187: "reported_uncorrectable_errors",
    188: "command_timeout",
    194: "temperature_celsius",
    197: "current_pending_sector_count",
    198: "offline_uncorrectable_sector_count",
    202: "percent_lifetime_used",
}

FEATURE_COLUMNS = [
    "health_status_score",
    "operational_status_score",
    "predict_failure",
    "temperature_celsius",
    "wear",
    "read_errors_total",
    "write_errors_total",
    "reallocated_sector_count",
    "reported_uncorrectable_errors",
    "command_timeout",
    "current_pending_sector_count",
    "offline_uncorrectable_sector_count",
    "percent_lifetime_used",
    "critical_warning",
    "available_spare",
    "available_spare_threshold",
    "unsafe_shutdowns",
    "media_errors",
    "error_log_entries",
    "warning_temp_time",
    "critical_temp_time",
]

TRAINING_COLUMNS = [
    "timestamp",
    "drive_id",
    "source",
    *FEATURE_COLUMNS,
]

SNAPSHOT_COLUMNS = [
    "timestamp",
    "drive_id",
    "model",
    "serial",
    "media_type",
    "bus_type",
    "size_gb",
    "source",
    "health_status",
    "operational_status",
    "health_status_score",
    "operational_status_score",
    "predict_failure",
    "temperature_celsius",
    "wear",
    "read_errors_total",
    "write_errors_total",
    "power_on_hours",
    "reallocated_sector_count",
    "power_cycle_count",
    "reported_uncorrectable_errors",
    "command_timeout",
    "current_pending_sector_count",
    "offline_uncorrectable_sector_count",
    "percent_lifetime_used",
    "critical_warning",
    "available_spare",
    "available_spare_threshold",
    "data_units_read",
    "data_units_written",
    "host_reads",
    "host_writes",
    "controller_busy_time",
    "unsafe_shutdowns",
    "media_errors",
    "error_log_entries",
    "warning_temp_time",
    "critical_temp_time",
    "risk_level",
    "risk_score",
    "reasons",
    "baseline_anomaly_score",
]

last_alert_times = {}


def run_powershell_json(command):
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if completed.returncode != 0:
        return None, completed.stderr.strip()

    output = completed.stdout.strip()
    if not output:
        return [], None

    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        return None, f"Could not parse PowerShell JSON: {exc}"

    if isinstance(data, dict):
        return [data], None

    return data, None


def run_command_json(command):
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=60,
    )

    if completed.returncode not in (0, 2, 4):
        return None, completed.stderr.strip() or completed.stdout.strip()

    output = completed.stdout.strip()
    if not output:
        return None, "command returned no output"

    try:
        return json.loads(output), None
    except json.JSONDecodeError as exc:
        return None, f"Could not parse command JSON: {exc}"


def normalize_status(value):
    if value is None:
        return "unknown"

    if isinstance(value, list):
        return ", ".join(str(item) for item in value).lower()

    return str(value).lower()


def status_score(value, healthy_words):
    status = normalize_status(value)
    if any(word in status for word in healthy_words):
        return 0
    if "unknown" in status or not status:
        return 10
    if "warning" in status:
        return 45
    return 75


def raw_int(value):
    if value is None or value == "":
        return 0

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_smart_vendor_bytes(vendor_bytes):
    attributes = {}
    if not vendor_bytes:
        return attributes

    values = [raw_int(value) for value in vendor_bytes]

    # SMART data is commonly packed into 12-byte attribute records after two header bytes.
    for offset in range(2, len(values) - 12, 12):
        attribute_id = values[offset]
        if attribute_id == 0:
            continue

        raw_value = 0
        for byte_index in range(5, 11):
            raw_value += values[offset + byte_index] << (8 * (byte_index - 5))

        name = SMART_ATTRIBUTE_IDS.get(attribute_id)
        if name:
            attributes[name] = raw_value

    return attributes


def collect_physical_disks():
    command = (
        "Get-PhysicalDisk | "
        "Select-Object FriendlyName,SerialNumber,MediaType,HealthStatus,"
        "OperationalStatus,Size,BusType | ConvertTo-Json -Depth 4"
    )
    disks, error = run_powershell_json(command)
    if error:
        print(f"Physical disk collection warning: {error}")
        return []

    return disks or []


def collect_reliability_counters():
    command = (
        "$rows = @(); "
        "Get-PhysicalDisk | ForEach-Object { "
        "$disk = $_; $counter = $null; "
        "try { $counter = Get-StorageReliabilityCounter -PhysicalDisk $disk } catch {} "
        "$rows += [pscustomobject]@{"
        "SerialNumber=$disk.SerialNumber;"
        "Temperature=$counter.Temperature;"
        "Wear=$counter.Wear;"
        "ReadErrorsTotal=$counter.ReadErrorsTotal;"
        "WriteErrorsTotal=$counter.WriteErrorsTotal;"
        "PowerOnHours=$counter.PowerOnHours"
        "} "
        "}; "
        "$rows | ConvertTo-Json -Depth 4"
    )
    rows, error = run_powershell_json(command)
    if error:
        print(f"Reliability counter warning: {error}")
        return {}

    counters = {}
    for row in rows or []:
        serial = str(row.get("SerialNumber", "")).strip()
        if serial:
            counters[serial] = row

    return counters


def collect_legacy_smart_attributes():
    command = (
        "try { "
        "Get-CimInstance -Namespace root\\wmi -ClassName MSStorageDriver_FailurePredictData | "
        "Select-Object InstanceName,VendorSpecific | ConvertTo-Json -Depth 5 "
        "} catch { '' }"
    )
    rows, error = run_powershell_json(command)
    if error:
        return {}

    smart_by_instance = {}
    for row in rows or []:
        instance = str(row.get("InstanceName", "")).strip()
        smart_by_instance[instance] = parse_smart_vendor_bytes(row.get("VendorSpecific"))

    return smart_by_instance


def collect_smartctl_devices():
    completed = subprocess.run(
        ["smartctl", "--scan-open"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    if completed.returncode != 0:
        return []

    devices = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split()
        if parts:
            devices.append(parts[0])

    return devices


def nested_get(data, path, default=0):
    current = data
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return default
        current = current[part]
    return current


def build_smartctl_snapshot(data):
    device = data.get("device", {})
    smart_status = data.get("smart_status", {})
    nvme_log = data.get("nvme_smart_health_information_log", {})
    capacity = nested_get(data, ["user_capacity", "bytes"], data.get("nvme_total_capacity", 0))
    serial = str(data.get("serial_number", "")).strip()

    media_errors = raw_int(nvme_log.get("media_errors"))
    error_entries = raw_int(nvme_log.get("num_err_log_entries"))
    percentage_used = raw_int(nvme_log.get("percentage_used"))

    snapshot = default_snapshot()
    snapshot.update(
        {
            "drive_id": serial or device.get("name", data.get("model_name", "unknown")),
            "model": data.get("model_name", "unknown"),
            "serial": serial,
            "media_type": "SSD" if device.get("protocol") == "NVMe" else device.get("protocol", "unknown"),
            "bus_type": device.get("type", "unknown"),
            "size_gb": round(raw_int(capacity) / (1024 ** 3), 2),
            "source": "smartctl",
            "health_status": "Healthy" if smart_status.get("passed", False) else "Warning",
            "operational_status": "ok" if smart_status.get("passed", False) else "warning",
            "health_status_score": 0 if smart_status.get("passed", False) else 75,
            "operational_status_score": 0 if smart_status.get("passed", False) else 75,
            "predict_failure": 0 if smart_status.get("passed", False) else 1,
            "temperature_celsius": raw_int(nested_get(data, ["temperature", "current"], nvme_log.get("temperature"))),
            "wear": percentage_used,
            "read_errors_total": media_errors,
            "write_errors_total": 0,
            "power_on_hours": raw_int(nested_get(data, ["power_on_time", "hours"], nvme_log.get("power_on_hours"))),
            "power_cycle_count": raw_int(data.get("power_cycle_count", nvme_log.get("power_cycles"))),
            "reported_uncorrectable_errors": media_errors,
            "percent_lifetime_used": raw_int(nested_get(data, ["endurance_used", "current_percent"], percentage_used)),
            "critical_warning": raw_int(nvme_log.get("critical_warning")),
            "available_spare": raw_int(nvme_log.get("available_spare")),
            "available_spare_threshold": raw_int(nvme_log.get("available_spare_threshold")),
            "data_units_read": raw_int(nvme_log.get("data_units_read")),
            "data_units_written": raw_int(nvme_log.get("data_units_written")),
            "host_reads": raw_int(nvme_log.get("host_reads")),
            "host_writes": raw_int(nvme_log.get("host_writes")),
            "controller_busy_time": raw_int(nvme_log.get("controller_busy_time")),
            "unsafe_shutdowns": raw_int(nvme_log.get("unsafe_shutdowns")),
            "media_errors": media_errors,
            "error_log_entries": error_entries,
            "warning_temp_time": raw_int(nvme_log.get("warning_temp_time")),
            "critical_temp_time": raw_int(nvme_log.get("critical_comp_time")),
        }
    )

    return snapshot


def collect_smartctl_snapshots():
    snapshots = []
    for device in collect_smartctl_devices():
        data, error = run_command_json(["smartctl", "-a", "-j", device])
        if error:
            print(f"smartctl warning for {device}: {error}")
            continue

        snapshots.append(build_smartctl_snapshot(data))

    return snapshots


def build_drive_snapshot(disk, counters, smart_attrs):
    serial = str(disk.get("SerialNumber", "")).strip()
    counter = counters.get(serial, {})

    snapshot = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "drive_id": serial or str(disk.get("FriendlyName", "unknown")),
        "model": disk.get("FriendlyName", "unknown"),
        "serial": serial,
        "media_type": disk.get("MediaType", "unknown"),
        "bus_type": disk.get("BusType", "unknown"),
        "size_gb": round(raw_int(disk.get("Size")) / (1024 ** 3), 2),
        "health_status": disk.get("HealthStatus", "unknown"),
        "operational_status": normalize_status(disk.get("OperationalStatus")),
        "health_status_score": status_score(disk.get("HealthStatus"), ["healthy", "ok"]),
        "operational_status_score": status_score(disk.get("OperationalStatus"), ["ok", "healthy"]),
        "predict_failure": 0,
        "temperature_celsius": raw_int(counter.get("Temperature")),
        "wear": raw_int(counter.get("Wear")),
        "read_errors_total": raw_int(counter.get("ReadErrorsTotal")),
        "write_errors_total": raw_int(counter.get("WriteErrorsTotal")),
        "power_on_hours": raw_int(counter.get("PowerOnHours")),
        "source": "real",
    }

    for name in SMART_ATTRIBUTE_IDS.values():
        snapshot[name] = 0

    # Legacy SMART WMI does not reliably expose a serial match for NVMe drives, so merge only
    # when there is a single SMART record. SATA drives often expose richer values here.
    if len(smart_attrs) == 1:
        snapshot.update(next(iter(smart_attrs.values())))

    return snapshot


def default_snapshot():
    snapshot = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "drive_id": "unknown",
        "model": "unknown",
        "serial": "",
        "media_type": "unknown",
        "bus_type": "unknown",
        "size_gb": 0,
        "health_status": "Healthy",
        "operational_status": "ok",
        "health_status_score": 0,
        "operational_status_score": 0,
        "predict_failure": 0,
        "temperature_celsius": 0,
        "wear": 0,
        "read_errors_total": 0,
        "write_errors_total": 0,
        "power_on_hours": 0,
        "source": "real",
        "power_cycle_count": 0,
        "critical_warning": 0,
        "available_spare": 0,
        "available_spare_threshold": 0,
        "data_units_read": 0,
        "data_units_written": 0,
        "host_reads": 0,
        "host_writes": 0,
        "controller_busy_time": 0,
        "unsafe_shutdowns": 0,
        "media_errors": 0,
        "error_log_entries": 0,
        "warning_temp_time": 0,
        "critical_temp_time": 0,
    }

    for name in SMART_ATTRIBUTE_IDS.values():
        snapshot[name] = 0

    return snapshot


def normalize_snapshot(snapshot):
    normalized = default_snapshot()
    normalized.update(snapshot)

    normalized["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
    normalized["drive_id"] = str(normalized.get("drive_id") or normalized.get("serial") or normalized.get("model"))
    normalized["serial"] = str(normalized.get("serial") or "")
    normalized["operational_status"] = normalize_status(normalized.get("operational_status"))
    normalized["health_status_score"] = raw_int(
        normalized.get("health_status_score", status_score(normalized.get("health_status"), ["healthy", "ok"]))
    )
    normalized["operational_status_score"] = raw_int(
        normalized.get("operational_status_score", status_score(normalized.get("operational_status"), ["ok", "healthy"]))
    )

    for column in FEATURE_COLUMNS:
        normalized[column] = raw_int(normalized.get(column))

    return normalized


def collect_simulated_snapshots():
    if not os.path.exists(SIMULATION_FILE):
        return []

    try:
        with open(SIMULATION_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Simulation file warning: {exc}")
        return []

    try:
        os.remove(SIMULATION_FILE)
    except OSError:
        pass

    if isinstance(data, dict):
        data = [data]

    snapshots = []
    for item in data:
        if isinstance(item, dict):
            item["source"] = "simulation"
            snapshots.append(normalize_snapshot(item))

    if snapshots:
        print(f"Loaded {len(snapshots)} simulated drive snapshot(s) from {SIMULATION_FILE}")

    return snapshots


def add_reason(reasons, condition, message, score):
    if condition:
        reasons.append(message)
        return score
    return 0


def evaluate_drive(snapshot):
    reasons = []
    score = 0

    score += add_reason(
        reasons,
        snapshot["health_status_score"] >= 45,
        f"health status is {snapshot['health_status']}",
        snapshot["health_status_score"],
    )
    score += add_reason(
        reasons,
        snapshot["operational_status_score"] >= 45,
        f"operational status is {snapshot['operational_status']}",
        snapshot["operational_status_score"],
    )
    score += add_reason(reasons, snapshot["predict_failure"] == 1, "drive predicts failure", 100)
    score += add_reason(reasons, snapshot["reallocated_sector_count"] > 0, "reallocated sectors detected", 25)
    score += add_reason(reasons, snapshot["reported_uncorrectable_errors"] > 0, "reported uncorrectable errors detected", 35)
    score += add_reason(reasons, snapshot["command_timeout"] > 0, "command timeouts detected", 20)
    score += add_reason(reasons, snapshot["current_pending_sector_count"] > 0, "pending sectors detected", 35)
    score += add_reason(reasons, snapshot["offline_uncorrectable_sector_count"] > 0, "offline uncorrectable sectors detected", 40)
    score += add_reason(reasons, snapshot["read_errors_total"] > 0, "read errors detected", 20)
    score += add_reason(reasons, snapshot["write_errors_total"] > 0, "write errors detected", 25)
    score += add_reason(reasons, snapshot["temperature_celsius"] >= 50, "temperature is high", 20)
    score += add_reason(reasons, snapshot["temperature_celsius"] >= 60, "temperature is critical", 30)
    score += add_reason(reasons, snapshot["wear"] >= 80, "SSD wear is high", 30)
    score += add_reason(reasons, snapshot["wear"] >= 95, "SSD wear is critical", 40)
    score += add_reason(reasons, snapshot["percent_lifetime_used"] >= 80, "SSD lifetime used is high", 30)
    score += add_reason(reasons, snapshot["percent_lifetime_used"] >= 95, "SSD lifetime used is critical", 40)
    score += add_reason(reasons, snapshot["critical_warning"] > 0, "NVMe critical warning is active", 70)
    score += add_reason(
        reasons,
        snapshot["available_spare_threshold"] > 0 and snapshot["available_spare"] <= snapshot["available_spare_threshold"],
        "available spare is below threshold",
        50,
    )
    score += add_reason(reasons, snapshot["media_errors"] > 0, "NVMe media errors detected", 40)
    score += add_reason(reasons, snapshot["warning_temp_time"] > 0, "drive has spent time above warning temperature", 20)
    score += add_reason(reasons, snapshot["critical_temp_time"] > 0, "drive has spent time above critical temperature", 35)

    score = min(score, 100)
    if score >= 60:
        level = "critical"
    elif score >= 25:
        level = "warning"
    else:
        level = "healthy"

    return level, score, reasons


def load_history():
    if not os.path.exists(HISTORY_FILE):
        return pd.DataFrame()

    ensure_csv_schema(HISTORY_FILE, SNAPSHOT_COLUMNS)
    return pd.read_csv(HISTORY_FILE)


def seed_training_data_from_history():
    if os.path.exists(TRAINING_DATA_FILE) or not os.path.exists(HISTORY_FILE):
        return

    history = load_history()
    if history.empty:
        return

    real_healthy_history = history[
        (history.get("source", "") != "simulation")
        & (history.get("risk_level", "").astype(str).str.lower() == "healthy")
    ]
    if real_healthy_history.empty:
        return

    training_data = real_healthy_history.reindex(columns=TRAINING_COLUMNS, fill_value=0)
    training_data.to_csv(TRAINING_DATA_FILE, index=False)


def load_training_data():
    seed_training_data_from_history()

    if not os.path.exists(TRAINING_DATA_FILE):
        return pd.DataFrame()

    ensure_csv_schema(TRAINING_DATA_FILE, TRAINING_COLUMNS)
    return pd.read_csv(TRAINING_DATA_FILE)


def get_baseline_training(snapshot, training_data):
    if training_data.empty:
        return pd.DataFrame()

    drive_training = training_data[training_data["drive_id"] == snapshot["drive_id"]]
    if snapshot.get("source") != "simulation" and "source" in drive_training.columns:
        drive_training = drive_training[drive_training["source"] != "simulation"]

    return drive_training


def is_training_drive(snapshot, training_data):
    if snapshot.get("source") == "simulation":
        return False

    return len(get_baseline_training(snapshot, training_data)) < MIN_BASELINE_SNAPSHOTS


def detect_baseline_anomaly(snapshot, training_data):
    drive_training = get_baseline_training(snapshot, training_data)
    if len(drive_training) < MIN_BASELINE_SNAPSHOTS:
        return None, None

    training_features = drive_training.reindex(columns=FEATURE_COLUMNS, fill_value=0)
    current_data = pd.DataFrame([snapshot]).reindex(columns=FEATURE_COLUMNS, fill_value=0)

    model = IsolationForest(contamination=MODEL_CONTAMINATION, random_state=42)
    model.fit(training_features)
    joblib.dump(model, MODEL_FILE)

    prediction = model.predict(current_data)[0]
    score = model.decision_function(current_data)[0]
    return prediction, score


def ensure_csv_schema(file_path, columns):
    if not os.path.exists(file_path):
        return

    try:
        dataframe = pd.read_csv(file_path)
    except pd.errors.ParserError:
        dataframe = pd.read_csv(file_path, engine="python", on_bad_lines="skip")

    if list(dataframe.columns) != columns:
        dataframe = dataframe.reindex(columns=columns, fill_value="")
        dataframe.to_csv(file_path, index=False)


def append_csv(file_path, row, columns):
    ensure_csv_schema(file_path, columns)
    pd.DataFrame([row]).to_csv(
        file_path,
        columns=columns,
        mode="a",
        header=not os.path.exists(file_path),
        index=False,
    )


def append_history_csv(row):
    append_csv(HISTORY_FILE, row, SNAPSHOT_COLUMNS)


def append_alert_csv(row):
    append_csv(ALERT_FILE, row, SNAPSHOT_COLUMNS)


def append_training_csv(snapshot):
    row = {column: snapshot.get(column, 0) for column in TRAINING_COLUMNS}
    append_csv(TRAINING_DATA_FILE, row, TRAINING_COLUMNS)


def should_send_alert(drive_id, level):
    if level == "healthy":
        return False

    key = f"{drive_id}:{level}"
    now = time.time()
    previous = last_alert_times.get(key, 0)
    if now - previous < ALERT_COOLDOWN_SECONDS:
        return False

    last_alert_times[key] = now
    return True


def send_drive_alert(snapshot, reasons, baseline_score=None):
    drive_name = snapshot["model"]
    if snapshot.get("serial"):
        drive_name = f"{drive_name} ({snapshot['serial']})"

    send_drive_telegram_alert(
        drive=drive_name,
        level=snapshot["risk_level"],
        risk_score=snapshot["risk_score"],
        reasons=reasons,
        baseline_score=baseline_score,
    )


def collect_snapshots():
    smartctl_snapshots = collect_smartctl_snapshots()
    if smartctl_snapshots:
        smartctl_snapshots.extend(collect_simulated_snapshots())
        return smartctl_snapshots

    disks = collect_physical_disks()
    counters = collect_reliability_counters()
    smart_attrs = collect_legacy_smart_attributes()

    snapshots = [build_drive_snapshot(disk, counters, smart_attrs) for disk in disks]
    snapshots.extend(collect_simulated_snapshots())
    return snapshots


def process_once():
    training_data = load_training_data()
    snapshots = collect_snapshots()

    if not snapshots:
        print("No drives found. Try running this script with permission to read disk health data.")
        return

    for snapshot in snapshots:
        level, score, reasons = evaluate_drive(snapshot)
        training_mode = is_training_drive(snapshot, training_data)
        baseline_prediction, baseline_score = detect_baseline_anomaly(snapshot, training_data)

        if baseline_prediction == -1 and level == "healthy":
            level = "warning"
            score = max(score, 25)
            reasons.append("SMART pattern differs from this drive's baseline")

        snapshot["risk_level"] = level
        snapshot["risk_score"] = score
        snapshot["reasons"] = ", ".join(reasons)
        snapshot["baseline_anomaly_score"] = baseline_score if baseline_score is not None else ""

        append_history_csv(snapshot)

        if snapshot.get("source") != "simulation" and level == "healthy":
            append_training_csv(snapshot)
            training_data = pd.concat(
                [training_data, pd.DataFrame([{column: snapshot.get(column, 0) for column in TRAINING_COLUMNS}])],
                ignore_index=True,
            )

        source = snapshot.get("source", "real")
        print(f"[{time.strftime('%H:%M:%S')}] {snapshot['model']} [{source}] -> {level.upper()} ({score}/100)")

        if training_mode and level != "healthy":
            print(f"[{time.strftime('%H:%M:%S')}] Drive alert skipped: collecting baseline data.")
            continue

        if should_send_alert(snapshot["drive_id"], level):
            append_alert_csv(snapshot)
            send_drive_alert(snapshot, reasons, baseline_score)


def main():
    print("Drive health prediction running...")
    print(f"Checking every {CHECK_INTERVAL_SECONDS} seconds.")

    while True:
        process_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    if "--once" in sys.argv:
        process_once()
    else:
        main()
