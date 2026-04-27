"""
============================================================
 MINE GUARD — Flask Backend
 Receives JSON from ESP32 (/update) or simulator (sim.py),
 evaluates per-sensor thresholds matching the mineguard.html
 frontend, maintains per-miner state + alert log, and serves
 /devices for the dashboard to poll.
============================================================
"""

from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime, timedelta
from collections import deque
from threading import Lock
import os

app = Flask(__name__, static_folder=None)

# ------------------------------------------------------------
# Per-sensor thresholds — single source of truth.
# These EXACTLY match mineguard.html so the dashboard colours
# always agree with what app.py decides.
#
# Sources / justification:
#   CO  (ppm)   : OSHA PEL 50, NIOSH IDLH 1200. >50 => evacuate.
#   SpO2 (%)    : <95 hypoxia warning, <92 critical.
#   O2  (%)     : 19.5 % is the OSHA minimum for safe atmosphere.
#   H2S (ppm)   : OSHA ceiling 20, NIOSH IDLH 100. >5 warn, >10 crit.
#   CH4 (% LEL) : 1 % LEL = ventilation alert; 5 % LEL = mandatory
#                  evacuation per most mine codes.
#   HR  (bpm)   : Resting >100 elevated; >120 critical (workload+stress).
#                  <50 brady warning.
#   Smoke (ppm) : >50 begin alert (mine fire risk).
#   Body temp   : >38.5 hyperthermia warn, >39.5 critical.
#   Flame       : digital — any TRUE = critical.
#   Fall        : digital — any TRUE = critical.
#   SOS         : digital — any TRUE = critical.
# ------------------------------------------------------------

THRESHOLDS = {
    # key      : [(warn_low, warn_high, crit_low, crit_high), inverted?]
    # use ±inf where one side doesn't apply.
    "co":       (35,  50,   "high"),
    "spo2":     (95,  92,   "low"),
    "o2":       (20.0, 19.5, "low"),
    "h2s":      (5,   10,   "high"),
    "ch4":      (1.0, 5.0,  "high"),       # % LEL
    "smoke":    (50,  150,  "high"),       # ppm
    "hr_high":  (100, 120,  "high"),
    "hr_low":   (60,  50,   "low"),
    "bodytemp": (37.8, 39.0, "high"),
    "airtemp":  (35,  40,   "high"),
}

OFFLINE_AFTER = timedelta(seconds=30)


# ------------------------------------------------------------
# Per-miner state
# ------------------------------------------------------------
_lock = Lock()

devices: dict[str, dict] = {}     # id -> {name,zone,level,data,last_seen,alerts(deque),sos}


def _ensure(device_id, payload):
    if device_id not in devices:
        devices[device_id] = {
            "id":        device_id,
            "name":      payload.get("name", device_id),
            "zone":      payload.get("zone", "Unknown"),
            "level":     payload.get("level", "L?"),
            "data":      {},
            "online":    True,
            "last_seen": datetime.utcnow(),
            "alerts":    deque(maxlen=50),
            "status":    "ok",          # ok | warn | danger | sos | offline
        }
    return devices[device_id]


def _add_alert(state, level, msg):
    """Push alert if not duplicate of the last one."""
    if state["alerts"] and state["alerts"][-1]["m"] == msg:
        return
    state["alerts"].append({
        "l":  level,                     # ok | warn | crit
        "m":  msg,
        "ts": datetime.utcnow().isoformat(timespec="seconds")
    })


# ------------------------------------------------------------
# Algorithm — evaluate one frame of sensor data, return status
# ------------------------------------------------------------
def evaluate(state, d):
    """
    Apply every per-sensor rule to dict d.  Returns ("ok"|"warn"|"danger"|"sos",
    list_of_short_alert_strings_for_this_frame).
    """
    status = "ok"
    frame_alerts = []

    def bump(s):
        nonlocal status
        order = {"ok": 0, "warn": 1, "danger": 2, "sos": 3}
        if order[s] > order[status]:
            status = s

    # ---- SOS (highest priority) ----
    if d.get("sos"):
        bump("sos")
        frame_alerts.append(("crit", "🚨 SOS TRIGGERED — MANUAL EMERGENCY ALERT"))

    # ---- Flame ----
    if d.get("flame"):
        bump("danger")
        frame_alerts.append(("crit", "🔥 FLAME DETECTED — fire suppression required"))

    # ---- Fall ----
    if d.get("fall"):
        bump("danger")
        frame_alerts.append(("crit", "⚠ FALL DETECTED — miner may be incapacitated"))

    # ---- CO ----
    co = d.get("co")
    if isinstance(co, (int, float)) and co >= 0:
        if co > THRESHOLDS["co"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"CO CRITICAL: {co:g} ppm — EVACUATE"))
        elif co > THRESHOLDS["co"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"CO elevated: {co:g} ppm"))

    # ---- SpO2 ----
    spo2 = d.get("spo2")
    if isinstance(spo2, (int, float)) and spo2 > 0:
        if spo2 < THRESHOLDS["spo2"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"SpO₂ CRITICAL: {spo2:g}%"))
        elif spo2 < THRESHOLDS["spo2"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"SpO₂ low: {spo2:g}%"))

    # ---- O2 atmosphere ----
    o2 = d.get("o2")
    if isinstance(o2, (int, float)) and o2 > 0:
        if o2 < THRESHOLDS["o2"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"O₂ CRITICAL LOW: {o2:g}%"))
        elif o2 < THRESHOLDS["o2"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"O₂ low: {o2:g}%"))

    # ---- H2S ----
    h2s = d.get("h2s")
    if isinstance(h2s, (int, float)) and h2s >= 0:
        if h2s > THRESHOLDS["h2s"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"H₂S DANGEROUS: {h2s:g} ppm"))
        elif h2s > THRESHOLDS["h2s"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"H₂S elevated: {h2s:g} ppm"))

    # ---- CH4 (% LEL) ----
    ch4 = d.get("ch4")
    if isinstance(ch4, (int, float)) and ch4 >= 0:
        if ch4 > THRESHOLDS["ch4"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"CH₄ EXPLOSIVE: {ch4:g}% LEL"))
        elif ch4 > THRESHOLDS["ch4"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"CH₄ elevated: {ch4:g}% LEL"))

    # ---- Smoke ----
    smoke = d.get("smoke")
    if isinstance(smoke, (int, float)) and smoke >= 0:
        if smoke > THRESHOLDS["smoke"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"Smoke critical: {smoke:g} ppm"))
        elif smoke > THRESHOLDS["smoke"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"Smoke elevated: {smoke:g} ppm"))

    # ---- Heart rate (two-sided) ----
    hr = d.get("hr")
    if isinstance(hr, (int, float)) and hr > 0:
        if hr >= THRESHOLDS["hr_high"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"Heart rate critical: {hr:g} bpm"))
        elif hr >= THRESHOLDS["hr_high"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"Heart rate elevated: {hr:g} bpm"))
        elif hr <= THRESHOLDS["hr_low"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"Heart rate critically low: {hr:g} bpm"))
        elif hr <= THRESHOLDS["hr_low"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"Bradycardia: {hr:g} bpm"))

    # ---- Body temperature ----
    bt = d.get("bodytemp")
    if isinstance(bt, (int, float)) and bt > 30:
        if bt >= THRESHOLDS["bodytemp"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"Hyperthermia: {bt:g}°C"))
        elif bt >= THRESHOLDS["bodytemp"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"Body temp elevated: {bt:g}°C"))

    # ---- Air temperature (heat stress) ----
    at = d.get("airtemp")
    if isinstance(at, (int, float)):
        if at >= THRESHOLDS["airtemp"][1]:
            bump("danger")
            frame_alerts.append(("crit", f"Ambient too hot: {at:g}°C"))
        elif at >= THRESHOLDS["airtemp"][0]:
            bump("warn")
            frame_alerts.append(("warn", f"Ambient warm: {at:g}°C"))

    # persist alerts
    for lvl, msg in frame_alerts:
        _add_alert(state, lvl, msg)

    return status


# ------------------------------------------------------------
# Depth from pressure (BMP280)
# Hypsometric formula relative to surface baseline:
#   h = 44330 * (1 - (P/P0)^(1/5.255))   meters above P0
# Underground, h is negative -> depth = -h.
# ------------------------------------------------------------
SURFACE_P_HPA = 1013.25   # adjust to actual surface reading at the mine

def depth_from_pressure(p_hpa):
    if p_hpa is None or p_hpa <= 0:
        return None
    h = 44330.0 * (1.0 - (SURFACE_P_HPA / p_hpa) ** (1.0 / 5.255))
    # below surface gives positive depth
    return round(-h, 1)


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------

@app.route("/update", methods=["POST"])
def receive_update():
    """ESP32 / simulator pushes a frame here."""
    payload = request.get_json(silent=True) or {}
    device_id = payload.get("id")
    if not device_id:
        return jsonify(error="missing id"), 400

    d = payload.get("data", {})

    with _lock:
        state = _ensure(device_id, payload)
        state["data"]      = d
        state["last_seen"] = datetime.utcnow()
        state["online"]    = True

        # compute depth if pressure present
        if "pressure" in d:
            depth = depth_from_pressure(d["pressure"])
            if depth is not None:
                state["data"]["depth"] = depth

        state["status"] = evaluate(state, d)

    return jsonify(ok=True, status=state["status"]), 200


@app.route("/devices")
def list_devices():
    """Snapshot of all miners for the dashboard to poll."""
    now = datetime.utcnow()
    out = []
    counts = {"ok": 0, "warn": 0, "danger": 0, "sos": 0, "offline": 0}

    with _lock:
        for state in devices.values():
            online = (now - state["last_seen"]) < OFFLINE_AFTER
            state["online"] = online
            if not online:
                state["status"] = "offline"

            counts[state["status"]] = counts.get(state["status"], 0) + 1

            out.append({
                "id":     state["id"],
                "name":   state["name"],
                "zone":   state["zone"],
                "level":  state["level"],
                "online": online,
                "status": state["status"],
                "data":   state["data"],
                "alerts": list(state["alerts"])[-5:],  # last 5
                "last_seen": state["last_seen"].isoformat(timespec="seconds"),
            })

    return jsonify({
        "miners":  out,
        "counts":  counts,
        "ts":      now.isoformat(timespec="seconds"),
    })


@app.route("/sos", methods=["POST"])
def manual_sos():
    """Frontend Employee SOS button posts here."""
    body = request.get_json(silent=True) or {}
    device_id = body.get("id")
    if not device_id:
        return jsonify(error="missing id"), 400
    with _lock:
        if device_id not in devices:
            return jsonify(error="unknown miner"), 404
        state = devices[device_id]
        state["status"] = "sos"
        state["data"]["sos"] = True
        _add_alert(state, "crit", "🚨 SOS TRIGGERED — MANUAL EMERGENCY ALERT")
    return jsonify(ok=True)


@app.route("/clear_sos", methods=["POST"])
def clear_sos():
    body = request.get_json(silent=True) or {}
    device_id = body.get("id")
    with _lock:
        if device_id in devices:
            devices[device_id]["data"]["sos"] = False
    return jsonify(ok=True)


# ------------------------------------------------------------
# Static frontend hosting (serve mineguard.html from ../frontend)
# ------------------------------------------------------------
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

@app.route("/")
def index():
    return send_from_directory(FRONTEND_DIR, "mineguard.html")

@app.route("/<path:fn>")
def static_files(fn):
    return send_from_directory(FRONTEND_DIR, fn)


# ------------------------------------------------------------
if __name__ == "__main__":
    print("Mine Guard backend on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)
