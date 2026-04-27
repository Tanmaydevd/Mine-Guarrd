"""
============================================================
 MINE GUARD — Multi-miner Simulator
 Posts realistic sensor frames for several virtual miners to
 the Flask backend, so the dashboard works without ESP32s.
 Occasionally injects warnings, criticals, falls and SOS to
 exercise every alert path.
============================================================
"""

import random
import time
import math
import threading
import requests
from datetime import datetime

BACKEND = "http://localhost:5000/update"
PERIOD  = 2.0   # seconds per frame

# Each miner has a baseline + scenario.  scenarios drive the demo.
MINERS = [
    {"id": "EMP-041", "name": "Arjun Kumar",   "zone": "Sector A1", "level": "L2", "scenario": "normal"},
    {"id": "EMP-042", "name": "Priya Devi",    "zone": "Sector A2", "level": "L2", "scenario": "warn_co"},
    {"id": "EMP-043", "name": "Ravi Singh",    "zone": "Sector B1", "level": "L3", "scenario": "normal"},
    {"id": "EMP-044", "name": "Meena Reddy",   "zone": "Sector B2", "level": "L3", "scenario": "danger"},
    {"id": "EMP-045", "name": "Suresh Naidu",  "zone": "Sector C1", "level": "L1", "scenario": "normal"},
    {"id": "EMP-046", "name": "Lakshmi Bai",   "zone": "Sector C2", "level": "L1", "scenario": "warn_co"},
    {"id": "EMP-047", "name": "Muthu Vel",     "zone": "Sector B2", "level": "L3", "scenario": "warn_co"},
    {"id": "EMP-048", "name": "Deepak Raj",    "zone": "Sector A3", "level": "L2", "scenario": "normal"},
    {"id": "EMP-051", "name": "Anbu Mani",     "zone": "Sector D2", "level": "L2", "scenario": "sos"},
]

# Surface barometric pressure baseline (hPa).
P0 = 1013.25


def pressure_for_depth(depth_m):
    """Inverse of the depth_from_pressure formula in app.py."""
    # h = 44330 * (1 - (P0/P)^(1/5.255))   with h = -depth_m
    h = -depth_m
    return P0 / ((1 - h / 44330.0) ** 5.255)


def make_frame(m, t):
    """Generate one sensor frame for miner m at iteration t."""
    sc = m["scenario"]

    # baseline depth per level
    depth = {"L0": 5, "L1": 18, "L2": 32, "L3": 47}[m["level"]] + random.uniform(-1, 1)

    # base values
    f = {
        "hr":       int(75 + math.sin(t * 0.3 + hash(m["id"]) % 7) * 6 + random.uniform(-2, 2)),
        "spo2":     round(98 + math.sin(t * 0.1) * 0.5, 0),
        "bodytemp": round(36.6 + math.sin(t * 0.05) * 0.3 + random.uniform(-0.1, 0.1), 1),
        "co":       max(0, int(8 + math.sin(t * 0.2) * 3 + random.uniform(0, 4))),
        "ch4":      round(max(0.0, 0.1 + random.uniform(-0.05, 0.1)), 2),
        "h2s":      max(0, int(1 + random.uniform(-0.5, 1.5))),
        "o2":       round(20.9 + math.sin(t * 0.07) * 0.1, 1),
        "smoke":    max(0, int(6 + random.uniform(-2, 4))),
        "flame":    False,
        "airtemp":  round(27 + math.sin(t * 0.04) * 1.5, 1),
        "humidity": round(58 + math.sin(t * 0.06) * 4, 1),
        "pressure": round(pressure_for_depth(depth), 2),
        "fall":     False,
        "impact":   False,
        "rotation": False,
        "sos":      False,
        "ax":       round(random.uniform(-0.1, 0.1), 2),
        "ay":       round(random.uniform(-0.1, 0.1), 2),
        "az":       round(0.95 + random.uniform(-0.05, 0.05), 2),
        "gz":       round(random.uniform(-2, 2), 1),
    }

    # ------ scenario overlays ------
    if sc == "warn_co":
        f["co"]   = int(40 + math.sin(t * 0.15) * 6 + random.uniform(0, 4))   # 36..50 area
        f["hr"]   = int(f["hr"] + 8)
        f["airtemp"] = round(f["airtemp"] + 2, 1)

    elif sc == "danger":
        f["co"]   = int(60 + math.sin(t * 0.15) * 10 + random.uniform(0, 8))  # 50..78
        f["spo2"] = round(93 - random.uniform(0, 1), 0)
        f["hr"]   = int(110 + random.uniform(-3, 5))
        f["h2s"]  = int(8 + random.uniform(0, 3))
        f["o2"]   = round(19.7 + random.uniform(-0.2, 0.2), 1)
        f["smoke"]= int(30 + random.uniform(0, 6))

    elif sc == "sos":
        # critical AND sos pressed
        f["co"]   = int(85 + random.uniform(0, 10))
        f["spo2"] = round(91 - random.uniform(0, 1), 0)
        f["hr"]   = int(128 + random.uniform(-4, 6))
        f["h2s"]  = int(12 + random.uniform(0, 4))
        f["o2"]   = round(19.0 + random.uniform(-0.2, 0.2), 1)
        f["smoke"]= int(55 + random.uniform(0, 12))
        f["ch4"]  = round(1.4 + random.uniform(-0.2, 0.4), 2)
        f["sos"]  = True
        f["airtemp"] = round(34 + random.uniform(0, 1), 1)

    # rare random fall on normal miners
    if sc == "normal" and random.random() < 0.003:
        f["fall"]   = True
        f["impact"] = True
        f["az"]     = round(random.uniform(-0.5, 0.5), 2)

    return f


def push_frame(m, frame):
    payload = {
        "id":    m["id"],
        "name":  m["name"],
        "zone":  m["zone"],
        "level": m["level"],
        "data":  frame,
    }
    try:
        r = requests.post(BACKEND, json=payload, timeout=2)
        return r.status_code == 200
    except Exception as e:
        print(f"[{m['id']}] post failed: {e}")
        return False


def run_miner(m):
    t = 0
    while True:
        frame = make_frame(m, t)
        push_frame(m, frame)
        t += 1
        time.sleep(PERIOD)


def main():
    print(f"sim.py -> {BACKEND}")
    print(f"simulating {len(MINERS)} miners @ {PERIOD}s/frame\n")

    threads = []
    for m in MINERS:
        # stagger starts so frames are spread out
        time.sleep(0.1)
        th = threading.Thread(target=run_miner, args=(m,), daemon=True)
        th.start()
        threads.append(th)

    # idle main thread; ctrl-c to quit
    try:
        while True:
            time.sleep(10)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] still simulating {len(MINERS)} miners")
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
