import csv
import json
import logging
import os
import subprocess
import sys
import time
import warnings
import joblib
import numpy as np
from sklearn.svm import OneClassSVM
from sklearn.preprocessing import StandardScaler

warnings.simplefilter('ignore', getattr(np, 'RankWarning', getattr(getattr(np, 'exceptions', None), 'RankWarning', Warning)))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("drive_health_monitor")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DRIVE_PATH = "/dev/sda"

REAL_TRAINING_CSV = os.path.join(SCRIPT_DIR, "drive_health_training.csv")
SYNTHATIC_TRAINING_CSV = os.path.join(SCRIPT_DIR, "synthatic_drive_training.csv")
ATTRIB_SPIKE_FILE = os.path.join(SCRIPT_DIR, "attrib_spike_payload.json")
DEMO_MODEL_FILE = os.path.join(SCRIPT_DIR, "demo_model.pkl")
LIVE_MODEL_FILE = os.path.join(SCRIPT_DIR, "live_model.pkl")

# --- CONFIG ---
DEMO_MODE = True
INITIAL_TRAINING_ROWS = 100
RETRAINING_ROWS = 200
DETECTION_INTERVAL = 1
STRIKE_NUMS = 3
OCSVM_NU = 0.05

DRIVE_PROFILES = {
    "ssd": {
        "attrs": ["Timestamp", "Percentage_Used", "Available_Spare",
                  "Reallocated_Block_Count", "Uncorrectable_Error_Count",
                  "Temperature", "Power_On_Hours"],
        "hard_rule_cols": ["Reallocated_Block_Count", "Uncorrectable_Error_Count"],
        "rul_tracking_cols": ["Percentage_Used", "Reallocated_Block_Count", "Uncorrectable_Error_Count"],
        "failure_limit": 150.0  
    },
    "hdd": {
        "attrs": ["Timestamp", "Reallocated_Sector_Count", "Current_Pending_Sector",
                  "Uncorrectable_Sector_Count", "Seek_Error_Rate",
                  "Temperature", "Power_On_Hours"],
        "hard_rule_cols": ["Current_Pending_Sector", "Uncorrectable_Sector_Count"],
        "rul_tracking_cols": ["Reallocated_Sector_Count", "Current_Pending_Sector", "Uncorrectable_Sector_Count"],
        "failure_limit": 120.0
    },
}

# ---------------------------------------------------------------------------
# Drive detection

def detect_drive_type():
    result = subprocess.run(["smartctl", "-j", "-a", DRIVE_PATH], capture_output=True, text=True)
    try:
        data = json.loads(result.stdout)
    except Exception:
        raise RuntimeError("Couldn't parse smartctl output.")
    if "nvme_smart_health_information_log" in data:
        return "ssd"
    return "ssd" if data.get("rotation_rate", 0) == 0 else "hdd"

def get_smart_data(drive_type):
    try:
        result = subprocess.run(["smartctl", "-j", "-a", DRIVE_PATH], capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except Exception:
        return None

    attrs = DRIVE_PROFILES[drive_type]["attrs"]
    vals = {}

    if drive_type == "ssd" and "nvme_smart_health_information_log" in data:
        n = data["nvme_smart_health_information_log"]
        vals = {
            "Percentage_Used": n.get("percentage_used", 0),
            "Available_Spare": n.get("available_spare", 0),
            "Reallocated_Block_Count": 100 - n.get("available_spare", 100),
            "Uncorrectable_Error_Count": n.get("media_errors", 0),
            "Temperature": n.get("temperature", 0),
            "Power_On_Hours": n.get("power_on_hours", 0),
        }
    elif "ata_smart_attributes" in data:
        table = {a["name"]: a["raw"]["value"] for a in data["ata_smart_attributes"]["table"]}
        if drive_type == "ssd":
            vals = {
                "Percentage_Used": table.get("Media_Wearout_Indicator", table.get("Percentage_Used", 0)),
                "Available_Spare": table.get("Available_Reserved_Space", 0),
                "Reallocated_Block_Count": table.get("Reallocated_Sector_Ct", 0),
                "Uncorrectable_Error_Count": table.get("Uncorrectable_Error_Cnt", table.get("Reported_Uncorrect", 0)),
                "Temperature": table.get("Temperature_Celsius", 0),
                "Power_On_Hours": table.get("Power_On_Hours", 0),
            }
        else:
            vals = {
                "Reallocated_Sector_Count": table.get("Reallocated_Sector_Ct", 0),
                "Current_Pending_Sector": table.get("Current_Pending_Sector", 0),
                "Uncorrectable_Sector_Count": table.get("Offline_Uncorrectable", table.get("Reported_Uncorrectable", 0)),
                "Seek_Error_Rate": table.get("Seek_Error_Rate", 0),
                "Temperature": table.get("Temperature_Celsius", 0),
                "Power_On_Hours": table.get("Power_On_Hours", 0),
            }
    else:
        return None

    features = [time.time()] + [vals.get(a, 0) for a in attrs[1:]]

    if os.path.exists(ATTRIB_SPIKE_FILE):
        try:
            with open(ATTRIB_SPIKE_FILE) as f:
                fake = json.load(f)
            for k, v in fake.items():
                idx = int(k) + 1
                if idx < len(features):
                    features[idx] = v
        except Exception:
            pass

    return features

def save_real_data(drive_type, data):
    is_new = not os.path.isfile(REAL_TRAINING_CSV)
    with open(REAL_TRAINING_CSV, "a", newline="") as f:
        if is_new:
            f.write(f"# drive_type: {drive_type}\n")
        writer = csv.writer(f)
        if is_new:
            writer.writerow(DRIVE_PROFILES[drive_type]["attrs"])
        writer.writerow(data)

# ---------------------------------------------------------------------------
# ML + State Engine
# ---------------------------------------------------------------------------

def read_drive_type(csv_path):
    with open(csv_path) as f:
        return f.readline().strip().split(":", 1)[1].strip()

def load_real_data(path):
    with open(path) as f:
        f.readline()
        reader = csv.reader(f)
        next(reader)
        return np.array([[float(x) for x in row] for row in reader])

def load_synthatic_data(path):
    drive_type = read_drive_type(path)
    with open(path) as f:
        f.readline()
        reader = csv.reader(f)
        next(reader)
        rows, labels = [], []
        for row in reader:
            *feat, label = row
            rows.append([float(x) for x in feat])
            labels.append(int(float(label)))
    return drive_type, np.array(rows), np.array(labels)

def train_ocsvm(X_features):
    scaler = StandardScaler().fit(X_features)
    ocsvm = OneClassSVM(kernel="rbf", gamma="scale", nu=OCSVM_NU).fit(scaler.transform(X_features))
    return ocsvm, scaler

class HealthEngine:
    def __init__(self, profile, ocsvm, scaler):
        self.profile = profile
        self.ocsvm = ocsvm
        self.scaler = scaler
        self.strikes = 0
        self.is_degraded = False
        self.degradation_history = [] 

    def evaluate(self, data):
        ts = data[0]
        X_test = self.scaler.transform([data[1:]])
        anomaly = self.ocsvm.predict(X_test)[0] == -1
        score = self.ocsvm.score_samples(X_test)[0]
        
        hard_idx = [self.profile["attrs"].index(c) for c in self.profile["hard_rule_cols"]]
        track_idx = [self.profile["attrs"].index(c) for c in self.profile["rul_tracking_cols"]]
        
        critical = any(data[idx] > 0 for idx in hard_idx)
        degradation_metric = sum(data[idx] for idx in track_idx)

        if self.is_degraded:
            self.degradation_history.append((ts, degradation_metric))
            self._predict_rul(ts)
            return

        if (anomaly and score < 0) or critical:
            self.strikes += 1
            reason = "Hard-Rule" if critical else "ML-Anomaly"
            log.warning(f"⚠️  ANOMALY ({reason}) strike {self.strikes}/{STRIKE_NUMS} | score={score:.3f}")
            
            if self.strikes >= STRIKE_NUMS:
                log.critical("CRITICAL: Drive degradation confirmed.")
                self.is_degraded = True
                self.degradation_history.append((ts, degradation_metric))
        else:
            self.strikes = 0
            log.info(f"Healthy | score={score:.3f}")

    def _predict_rul(self, current_ts):
        if len(self.degradation_history) < 3:
            log.warning("DEGRADED | RUL: Calculating...")
            return

        times = np.array([h[0] for h in self.degradation_history])
        metrics = np.array([h[1] for h in self.degradation_history])
        
        t_norm = times - times[0] 
        m, c = np.polyfit(t_norm, metrics, 1)
        limit = self.profile["failure_limit"]

        if metrics[-1] >= limit:
            log.critical(f"DEGRADED | Wear Index: {metrics[-1]:.1f} | Estimated RUL: IMMINENT/DEAD")
            return

        if m > 1e-5: 
            t_dead_norm = (limit - c) / m
            rul_seconds = t_dead_norm - (current_ts - times[0])
            
            if rul_seconds > 86400:
                rul = f"{rul_seconds/86400:.1f} days"
            elif rul_seconds > 0:
                rul = f"{rul_seconds/3600:.1f} hours"
            else:
                rul = "IMMINENT/DEAD"
            log.critical(f"DEGRADED | Wear Index: {metrics[-1]:.1f} | Estimated RUL: {rul}")
        else:
            log.critical(f"DEGRADED | Wear Index: {metrics[-1]:.1f} | Estimated RUL: Stable (No active error growth)")

# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def ensure_synthatic_training_data():
    if os.path.exists(SYNTHATIC_TRAINING_CSV):
        return True

    try:
        from generate_synthatic_data import main as generate_synthatic_data
        generate_synthatic_data()
        return os.path.exists(SYNTHATIC_TRAINING_CSV)
    except Exception as exc:
        log.error(f"{SYNTHATIC_TRAINING_CSV} missing and auto-generation failed: {exc}")
        return False

def run_demo_mode():
    ensure_synthatic_training_data()
    if not os.path.exists(SYNTHATIC_TRAINING_CSV):
        log.error(f"{SYNTHATIC_TRAINING_CSV} missing. Run synthatic_data.py.")
        sys.exit(1)

    drive_type, X, y = load_synthatic_data(SYNTHATIC_TRAINING_CSV)
    profile = DRIVE_PROFILES[drive_type]

    if os.path.exists(DEMO_MODEL_FILE):
        m = joblib.load(DEMO_MODEL_FILE)
        ocsvm, scaler = m["ocsvm"], m["scaler"]
    else:
        healthy_X = X[y == 0]
        ocsvm, scaler = train_ocsvm(healthy_X[:, 1:])
        joblib.dump({"ocsvm": ocsvm, "scaler": scaler}, DEMO_MODEL_FILE)

    log.info("[DEMO] Models loaded. Fast-forwarding directly to the degradation phase...")
    engine = HealthEngine(profile, ocsvm, scaler)

    # Fast-forward skip logic
    # Finds where label '1' starts, backs up by 5 rows so you can watch it catch the anomaly live
    degrad_start_idx = np.where(y == 1)[0]
    if len(degrad_start_idx) > 0:
        start_loop_at = max(0, degrad_start_idx[0] - 5)
        X_demo = X[start_loop_at:]
    else:
        X_demo = X

    for row in X_demo:
        if os.path.exists(ATTRIB_SPIKE_FILE):
            try:
                with open(ATTRIB_SPIKE_FILE) as f:
                    fake = json.load(f)
                for k, v in fake.items():
                    idx = int(k) + 1
                    if idx < len(row):
                        row[idx] = v
            except Exception:
                pass
                
        engine.evaluate(row)
        time.sleep(DETECTION_INTERVAL)

def run_live_mode():
    drive_type = read_drive_type(REAL_TRAINING_CSV) if os.path.exists(REAL_TRAINING_CSV) else detect_drive_type()
    profile = DRIVE_PROFILES[drive_type]
    total_rows = len(load_real_data(REAL_TRAINING_CSV)) if os.path.exists(REAL_TRAINING_CSV) else 0
    log.info(f"[LIVE] Drive: {drive_type.upper()} | Starting at {total_rows} rows")

    engine = None
    if os.path.exists(LIVE_MODEL_FILE):
        m = joblib.load(LIVE_MODEL_FILE)
        engine = HealthEngine(profile, m["ocsvm"], m["scaler"])

    next_refit = INITIAL_TRAINING_ROWS if total_rows < INITIAL_TRAINING_ROWS else INITIAL_TRAINING_ROWS + ((total_rows - INITIAL_TRAINING_ROWS) // RETRAINING_ROWS + 1) * RETRAINING_ROWS

    while True:
        time.sleep(DETECTION_INTERVAL)
        data = get_smart_data(drive_type)
        if not data: continue

        save_real_data(drive_type, data)
        total_rows += 1

        if engine is None and total_rows < INITIAL_TRAINING_ROWS:
            log.info(f"[*] Baseline ({total_rows}/{INITIAL_TRAINING_ROWS})...")
            continue

        if engine is None or (total_rows >= next_refit and not engine.is_degraded):
            X = load_real_data(REAL_TRAINING_CSV)
            ocsvm, scaler = train_ocsvm(X[:, 1:])
            joblib.dump({"ocsvm": ocsvm, "scaler": scaler}, LIVE_MODEL_FILE)
            
            was_degraded = engine.is_degraded if engine else False
            engine = HealthEngine(profile, ocsvm, scaler)
            engine.is_degraded = was_degraded
            
            log.info(f"[*] Model trained on {len(X)} rows.")
            next_refit += RETRAINING_ROWS

        engine.evaluate(data)

if __name__ == "__main__":
    main = run_demo_mode if DEMO_MODE else run_live_mode
    main()
