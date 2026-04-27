# Mine Guard v2 — Wearable Miner Safety System

Complete end-to-end implementation matching the `mineguard.html` frontend.

```
v2/
  firmware/
    esp32.ino       Arduino sketch — 13 sensors, fall detection, SOS, POSTs JSON
  backend/
    app.py          Flask server — thresholds, alert log, /devices, /update, /sos
    sim.py          Multi-miner simulator (use when no ESP32 available)
  frontend/
    mineguard.html  Dashboard — polls /devices, falls back to JS sim if backend down
```

## Running the demo (no hardware)

```
cd v2/backend
pip install flask requests
python app.py             # terminal 1   -> http://localhost:5000
python sim.py             # terminal 2   (simulates 9 miners)
# open http://localhost:5000  in a browser
```

The dashboard's `LIVE` pill (top bar) confirms it's reading from Flask. Without
the backend it shows `SIM MODE` and uses an in-page JS sim so the UI still works.

## Running with real ESP32

1. Open `firmware/esp32.ino` in Arduino IDE.
2. Install libraries:
   `ArduinoJson`, `DHT sensor library`, `Adafruit BME280`, `Adafruit BMP280`,
   `SparkFun MAX3010x`, `MPU6050_light`.
3. Edit at the top of the sketch:
   - `WIFI_SSID`, `WIFI_PASS`
   - `BACKEND_URL` to `http://<your-laptop-ip>:5000/update`
   - `DEVICE_ID`, `DEVICE_NAME`, `DEVICE_ZONE`, `DEVICE_LEVEL` per miner
4. **One-time MQ calibration**: power the board for 24 hr in clean air, then
   read the Rs values from the serial console and update the `R0_*` constants.
5. Flash. The board POSTs every 3 s to `/update`.

## Sensor algorithms — what each one does

| Sensor | Reads | Algorithm | Threshold (warn / crit) |
|---|---|---|---|
| MAX30102 | HR, SpO₂ | SparkFun `maxim_heart_rate_and_oxygen_saturation` over 100-sample IR/Red buffer | HR: 100/120 bpm or 60/50 low · SpO₂: 95/92 % |
| MPU6050 | accel, gyro | 3-stage fall: free-fall (<0.5 g, ≥100 ms) → impact (>2.5 g) → stillness (<0.3 g dev for 2 s) | fall=true ⇒ critical |
| MQ-7 | CO ppm | Rs from voltage divider, ppm = 99.04·(Rs/R0)^-1.518 | 35 / 50 ppm |
| MQ-4 | CH₄ % LEL | ppm = 1012.7·(Rs/R0)^-2.786, then ÷ 500 (LEL=50000 ppm) | 1 / 5 % LEL |
| MQ-136 | H₂S ppm | ppm = 36.74·(Rs/R0)^-3.435 | 5 / 10 ppm |
| MQ-2 | Smoke ppm | ppm = 574.25·(Rs/R0)^-2.222 | 50 / 150 ppm |
| O₂ EC | O₂ % | linear: O₂% = (V / V_at_20.9%) · 20.9 | 20.0 / 19.5 % |
| IR Flame | flame? | digital LOW = flame | true ⇒ critical |
| DHT22 | body °C | direct read | 37.8 / 39.0 °C |
| BME280 | air °C, RH, P | direct read | airtemp 35 / 40 °C |
| BMP280 | pressure → depth | depth = -44330·(1 - (P0/P)^(1/5.255)) m relative to surface P₀ (1013.25 hPa, configurable) | informational |
| SOS button | digital | 3-second long-press interrupt sets sticky flag | true ⇒ SOS |
| Vibration buzzer | output | local feedback: 1 short pulse on warn, 3 long on critical | — |

## What changed from v1 (`iotlabelthing/`)

- **esp32.ino** — original used DHT11+MQ135+MQ9+analog pulse and a broken fall test
  (`1.5 < totalAccel`); rewritten for all 13 sensors with calibrated MQ curves and
  a real Bourke 3-stage fall algorithm. Compile bug `HTTPClient server;` shadowing
  `const char* server` is gone.
- **app.py** — original only checked temperature/humidity/gas with simple bounds and
  had `info["last_seen"].format()` which crashed on a datetime; rewritten with
  per-sensor thresholds, alert log, SOS endpoint, depth from pressure, and
  proper offline detection.
- **sim.py** — original sent only temp/humidity/gas; rewritten to fan out 9 miners
  in threads with realistic scenarios (`normal`, `warn_co`, `danger`, `sos`).
- **mineguard.html** — was pure JS demo; now polls `/devices` for live data and
  falls back to JS sim only when backend is unreachable.
