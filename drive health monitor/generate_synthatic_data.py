import csv
import logging
import os
import numpy as np

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("generate_data")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REAL_TRAINING_CSV = os.path.join(SCRIPT_DIR, "drive_health_training.csv")
SYNTHATIC_TRAINING_CSV = os.path.join(SCRIPT_DIR, "synthatic_drive_training.csv")

CHECK_INTERVAL = 60
MIN_REAL_SAMPLES = 10

HEALTHY_MULTIPLIER = 20  # Increase to extend the healthy phase sizing 
DEGRADATION_STEPS = 100  # Increase to extend the failure window size

DRIVE_PROFILES = {
    "ssd": {
        "attrs": ["Timestamp", "Percentage_Used", "Available_Spare",
                  "Reallocated_Block_Count", "Uncorrectable_Error_Count",
                  "Temperature", "Power_On_Hours"],
        "healthy_noise": {"Percentage_Used": 0.05, "Available_Spare": 0.05, "Temperature": 1.0},
        "degradation": {
            "Percentage_Used": ("to", 100),
            "Available_Spare": ("to", 0),
            "Reallocated_Block_Count": ("to", 60), 
            "Uncorrectable_Error_Count": ("to", 20), 
            "Temperature": ("drift", 8),
            "Power_On_Hours": ("rate", None),
        },
    },
    "hdd": {
        "attrs": ["Timestamp", "Reallocated_Sector_Count", "Current_Pending_Sector",
                  "Uncorrectable_Sector_Count", "Seek_Error_Rate",
                  "Temperature", "Power_On_Hours"],
        "healthy_noise": {"Seek_Error_Rate": 2.0, "Temperature": 1.0},
        "degradation": {
            "Reallocated_Sector_Count": ("to", 60), 
            "Current_Pending_Sector": ("to", 60), 
            "Uncorrectable_Sector_Count": ("to", 20), 
            "Seek_Error_Rate": ("to", 500),
            "Temperature": ("drift", 8),
            "Power_On_Hours": ("rate", None),
        },
    },
}

def read_real_data(path):
    with open(path) as f:
        first = f.readline().strip()
        drive_type = first.split(":", 1)[1].strip()
        reader = csv.reader(f)
        next(reader) 
        rows = [[float(x) for x in row] for row in reader]
    data = np.array(rows, dtype=float)
    return drive_type, data

def generate_healthy_phase(real_data, profile, multiplier):
    attrs, noise = profile["attrs"], profile["healthy_noise"]
    ts = real_data[-1][0]
    out = []
    for _ in range(multiplier):
        for row in real_data:
            ts += CHECK_INTERVAL
            new_row = list(row)
            new_row[0] = ts
            for i, name in enumerate(attrs[1:], start=1):
                if name == "Power_On_Hours":
                    new_row[i] = row[i] + CHECK_INTERVAL / 3600
                elif name in noise:
                    new_row[i] = max(0, row[i] + np.random.normal(0, noise[name]))
            out.append(new_row)
    return np.array(out)

def generate_degradation_phase(profile, start_row, steps):
    attrs, rules = profile["attrs"], profile["degradation"]
    cols = [start_row[0] + np.arange(1, steps + 1) * CHECK_INTERVAL]
    for i, name in enumerate(attrs[1:], start=1):
        kind, val = rules[name]
        cur = start_row[i]
        if kind == "to":
            cols.append(np.linspace(cur, val, steps))
        elif kind == "exp":
            seed = max(cur, 1)
            cols.append(np.minimum(seed * (val ** np.arange(steps)), 5000))
        elif kind == "drift":
            cols.append(cur + np.linspace(0, val, steps) + np.random.normal(0, 0.5, steps))
        elif kind == "rate":
            cols.append(cur + (CHECK_INTERVAL / 3600) * np.arange(1, steps + 1))
    return np.column_stack(cols)

def write_synthatic_data(path, drive_type, attrs, rows, labels):
    with open(path, "w", newline="") as f:
        f.write(f"# drive_type: {drive_type}\n")
        writer = csv.writer(f)
        writer.writerow(attrs + ["Label"])
        for row, label in zip(rows, labels):
            writer.writerow(list(row) + [int(label)])

def main():
    drive_type, real_data = read_real_data(REAL_TRAINING_CSV)
    profile = DRIVE_PROFILES[drive_type]
    healthy = generate_healthy_phase(real_data, profile, HEALTHY_MULTIPLIER)
    degraded = generate_degradation_phase(profile, healthy[-1], DEGRADATION_STEPS)
    all_rows = np.vstack([real_data, healthy, degraded])
    labels = np.concatenate([np.zeros(len(real_data) + len(healthy)), np.ones(len(degraded))])
    write_synthatic_data(SYNTHATIC_TRAINING_CSV, drive_type, profile["attrs"], all_rows, labels)
    log.info(f"Successfully generated synthatic data with {len(all_rows)} total rows.")

if __name__ == "__main__":
    main()