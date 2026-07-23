import json
import time
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SPIKE_FILE = os.path.join(SCRIPT_DIR, "attrib_spike_payload.json")


spike_payload = {
    "0": 100,     # Index 1: Percentage_Used / Reallocated_Sector
    "1": 0,       # Index 2: Available_Spare / Current_Pending
    "2": 9999,    # Index 3: Reallocated_Block / Uncorrectable_Sector
    "3": 9999,    # Index 4: Uncorrectable_Error / Seek_Error
    "4": 150,     # Index 5: Temperature
    "5": 9999     # Index 6: Power_On_Hours
}

print(f"Injecting fake SMART attribute spike into:\n{SPIKE_FILE}")
with open(SPIKE_FILE, "w") as f:
    json.dump(spike_payload, f)

print("Attribute spike payload active.")
time.sleep(20)

if os.path.exists(SPIKE_FILE):
    os.remove(SPIKE_FILE)
    print("Payload file removed.")