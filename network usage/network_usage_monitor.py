import os
import sys
import time
import joblib
import numpy as np
import pandas as pd
import psutil
from sklearn.ensemble import IsolationForest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.append(PROJECT_DIR)

from telegram_alert import send_telegram_alert

MODEL_FILE = os.path.join(BASE_DIR, "network_usage_model.pkl")
TRAINING_DATA_FILE = os.path.join(BASE_DIR, "network_usage_training.csv")
DETECTION_DATA_FILE = os.path.join(BASE_DIR, "anomaly_detection.csv")

MIN_TRAINING_WINDOWS = 1000
SAMPLE_INTERVAL_SECONDS = 5
MODEL_CONTAMINATION = 0.05
ALERT_COOLDOWN_SECONDS = 300

FEATURE_COLUMNS = [
    "bytes_sent_per_sec",
    "bytes_recv_per_sec",
    "total_throughput_bps",
    "outbound_ratio",
    "avg_outbound_packet_size",
    "avg_inbound_packet_size",
]

ANOMALY_INTERPRETATION = {
    "bytes_sent_per_sec": {
        "high": "Unusual amount of data being uploaded",
    },
    "bytes_recv_per_sec": {
        "high": "Unusual amount of data being downloaded",
    },
    "total_throughput_bps": {
        "high": "Network bandwidth is heavily saturated",
        "low": "Network is unusually dead"
    },
    "outbound_ratio": {
        "high": "Traffic is heavily skewed towards uploading (possible exfiltration)",
        "low": "Traffic is heavily skewed towards downloading"
    },
    "avg_outbound_packet_size": {
        "high": "Sending unusually large data packets",
    },
    "avg_inbound_packet_size": {
        "high": "Receiving unusually large data packets",
    },
}

feature_vectors = []
feature_stats = {}
baseline_ready = False
detection_rows = 0
last_alert_time = 0.0
model = IsolationForest(contamination=MODEL_CONTAMINATION, random_state=42)

def csv_has_current_schema(file_path):
    if not os.path.exists(file_path):
        return True
    try:
        columns = list(pd.read_csv(file_path, nrows=0).columns)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return False
    return columns == FEATURE_COLUMNS

def update_feature_stats(dataframe):
    global feature_stats
    feature_stats = {}
    for column in FEATURE_COLUMNS:
        feature_stats[column] = {
            "mean": dataframe[column].mean(),
            "std": dataframe[column].std(),
        }

def extract_features(previous, current, elapsed):
    elapsed = max(elapsed, 0.001)

    bytes_sent = max(0, current.bytes_sent - previous.bytes_sent)
    bytes_recv = max(0, current.bytes_recv - previous.bytes_recv)
    packets_sent = max(0, current.packets_sent - previous.packets_sent)
    packets_recv = max(0, current.packets_recv - previous.packets_recv)

    total_bytes = bytes_sent + bytes_recv

    # Bounded ratios prevent division by zero and are better for IForest
    outbound_ratio = bytes_sent / total_bytes if total_bytes > 0 else 0.0
    avg_out_pkt_size = bytes_sent / packets_sent if packets_sent > 0 else 0.0
    avg_in_pkt_size = bytes_recv / packets_recv if packets_recv > 0 else 0.0

    return {
        "bytes_sent_per_sec": bytes_sent / elapsed,
        "bytes_recv_per_sec": bytes_recv / elapsed,
        "total_throughput_bps": total_bytes / elapsed,
        "outbound_ratio": outbound_ratio,
        "avg_outbound_packet_size": avg_out_pkt_size,
        "avg_inbound_packet_size": avg_in_pkt_size,
    }

def save_training_data():
    dataframe = pd.DataFrame(feature_vectors).reindex(columns=FEATURE_COLUMNS, fill_value=0.0)
    dataframe.to_csv(TRAINING_DATA_FILE, index=False)

def save_detection_row(feature_dict, prediction, score, reasons):
    global detection_rows
    detection_rows += 1

    row = {
        "row": detection_rows,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prediction": "anomaly" if prediction == -1 else "normal",
        "score": score,
        "reasons": ", ".join(reasons),
    }
    row.update(feature_dict)

    pd.DataFrame([row]).to_csv(
        DETECTION_DATA_FILE,
        mode="a",
        header=not os.path.exists(DETECTION_DATA_FILE),
        index=False,
    )
    return detection_rows

def load_existing_data():
    global feature_vectors, baseline_ready, model, detection_rows

    if os.path.exists(TRAINING_DATA_FILE):
        if csv_has_current_schema(TRAINING_DATA_FILE):
            dataframe = pd.read_csv(TRAINING_DATA_FILE).reindex(columns=FEATURE_COLUMNS, fill_value=0.0)
            feature_vectors = dataframe.to_dict("records")
            print(f"Loaded {len(feature_vectors)} past network feature windows.")
        else:
            print("Feature set mismatch. Starting fresh.")

    if os.path.exists(DETECTION_DATA_FILE):
        detection_rows = len(pd.read_csv(DETECTION_DATA_FILE))
        print(f"Loaded {detection_rows} past network detection rows.")

    if os.path.exists(MODEL_FILE) and feature_vectors:
        model = joblib.load(MODEL_FILE)
        expected_features = getattr(model, "n_features_in_", len(FEATURE_COLUMNS))
        if expected_features == len(FEATURE_COLUMNS):
            baseline_ready = True
            print("Loaded existing network baseline model.")
            update_feature_stats(pd.DataFrame(feature_vectors).reindex(columns=FEATURE_COLUMNS, fill_value=0.0))
        else:
            print("Feature set mismatch in model. Will retrain.")
    else:
        print(f"Will train network baseline after {MIN_TRAINING_WINDOWS} windows.")
        print(f"(each window = {SAMPLE_INTERVAL_SECONDS}s of aggregate network activity).")

def train_baseline():
    global baseline_ready

    if len(feature_vectors) >= MIN_TRAINING_WINDOWS and not baseline_ready:
        dataframe = pd.DataFrame(feature_vectors).reindex(columns=FEATURE_COLUMNS, fill_value=0.0)
        model.fit(dataframe)
        joblib.dump(model, MODEL_FILE)
        update_feature_stats(dataframe)

        baseline_ready = True
        print(f"[{time.strftime('%H:%M:%S')}] Baseline trained on {len(feature_vectors)} network windows.")
        print("Network usage anomaly detection active.")

def get_anomaly_reasons(feature_dict):
    reasons = []
    for feature_name, value in feature_dict.items():
        stats = feature_stats.get(feature_name)
        if not stats:
            continue
        
        std = stats["std"]
        if std > 0:
            z_score = (value - stats["mean"]) / std
            if abs(z_score) > 2.5:
                direction = "high" if z_score > 0 else "low"
                if feature_name in ANOMALY_INTERPRETATION:
                    reason = ANOMALY_INTERPRETATION[feature_name].get(direction)
                    if reason:
                        reasons.append(reason)
    return reasons

def maybe_send_alert(score, row_number, reasons):
    global last_alert_time
    now = time.time()
    if now - last_alert_time < ALERT_COOLDOWN_SECONDS:
        return

    reason_text = "; ".join(reasons[:3]) if reasons else "network usage pattern drift"
    message = (
        f"Status: Network anomaly detected\n"
        f"Score: {score:.1%}\n"
        f"Why: {reason_text}"
    )
    send_telegram_alert("network", message)
    last_alert_time = now

def detect_anomaly(feature_dict):
    if not baseline_ready:
        return

    dataframe = pd.DataFrame([feature_dict]).reindex(columns=FEATURE_COLUMNS, fill_value=0.0)
    prediction = model.predict(dataframe)[0]
    score = model.decision_function(dataframe)[0]
    reasons = get_anomaly_reasons(feature_dict)
    row_number = save_detection_row(feature_dict, prediction, score, reasons)

    if prediction == -1:
        reason_text = ", ".join(reasons) if reasons else "network usage pattern drift"
        print(
            f"[{time.strftime('%H:%M:%S')}] ANOMALY "
            f"(Score: {score:.1%}) - Reason: {reason_text}"
        )
        maybe_send_alert(score, row_number, reasons)
    else:
        print(f"[{time.strftime('%H:%M:%S')}] NORMAL (Score: {score:.1%})")

def main_loop():
    global feature_vectors

    previous = psutil.net_io_counters()
    previous_time = time.time()

    while True:
        time.sleep(SAMPLE_INTERVAL_SECONDS)
        current = psutil.net_io_counters()
        current_time = time.time()

        features = extract_features(previous, current, current_time - previous_time)
        previous = current
        previous_time = current_time

        was_training_window = not baseline_ready
        if was_training_window:
            feature_vectors.append(features)
            if len(feature_vectors) > 5000:
                feature_vectors = feature_vectors[-5000:]

            save_training_data()
            train_baseline()

        if baseline_ready and not was_training_window:
            detect_anomaly(features)

def main():
    load_existing_data()
    print("Network usage anomaly detection model running...")

    try:
        main_loop()
    except KeyboardInterrupt:
        print("\nExiting.")

if __name__ == "__main__":
    main()