/*
 * ============================================================
 *  MINE GUARD — Wearable Miner Safety Device (ESP32 Firmware)
 *  Hardware target: ESP32 DevKit v1
 *
 *  Sensors (all 13):
 *    1. MAX30102           - Heart Rate + SpO2     (I2C, 0x57)
 *    2. MPU6050            - Motion + Fall         (I2C, 0x68)
 *    3. BME280             - Air Temp/Humidity/P   (I2C, 0x76)
 *    4. BMP280             - Pressure for depth    (I2C, 0x77)
 *    5. DHT22              - Body Temp             (GPIO 4)
 *    6. MQ-7   (CO)        - Carbon monoxide       (GPIO 34, ADC1)
 *    7. MQ-4   (CH4)       - Methane               (GPIO 35, ADC1)
 *    8. MQ-136 (H2S)       - Hydrogen sulphide     (GPIO 32, ADC1)
 *    9. MQ-2   (Smoke/LPG) - Smoke                 (GPIO 33, ADC1)
 *   10. O2 Electrochem.    - Oxygen %              (GPIO 36, ADC1)
 *   11. IR Flame sensor    - Flame digital         (GPIO 27)
 *   12. SOS push button    - Emergency             (GPIO 14, INPUT_PULLUP)
 *   13. Vibration buzzer   - Alert feedback        (GPIO 26, OUTPUT)
 *
 *   Output: POSTs JSON to Flask backend /update endpoint every 3 s.
 *   Endpoint payload matches backend/app.py expectation exactly.
 * ============================================================
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <ArduinoJson.h>      // by Benoit Blanchon
#include <DHT.h>              // Adafruit DHT
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>  // Adafruit BME280
#include <Adafruit_BMP280.h>  // Adafruit BMP280
#include <MAX30105.h>         // SparkFun MAX3010x
#include "spo2_algorithm.h"   // ships with SparkFun MAX3010x
#include <MPU6050_light.h>    // rfetick MPU6050_light
#include <math.h>

// ==================== USER CONFIG ====================
const char* WIFI_SSID     = "YOUR_WIFI";
const char* WIFI_PASS     = "YOUR_PASS";
const char* BACKEND_URL   = "http://192.168.1.100:5000/update";  // Flask host
const char* DEVICE_ID     = "EMP-047";
const char* DEVICE_NAME   = "Muthu Vel";
const char* DEVICE_ZONE   = "Sector B2";
const char* DEVICE_LEVEL  = "L3";
const uint32_t SEND_PERIOD_MS = 3000;

// ==================== PIN MAP ========================
#define PIN_DHT22     4
#define PIN_FLAME    27
#define PIN_SOS      14
#define PIN_BUZZER   26
#define PIN_MQ7_CO   34
#define PIN_MQ4_CH4  35
#define PIN_MQ136_H2S 32
#define PIN_MQ2_SMOKE 33
#define PIN_O2       36

#define I2C_SDA      21
#define I2C_SCL      22

// ==================== SENSOR OBJECTS =================
DHT dht(PIN_DHT22, DHT22);
Adafruit_BME280 bme;
Adafruit_BMP280 bmp;
MAX30105       max30102;
MPU6050        mpu(Wire);

bool has_bme = false, has_bmp = false, has_max = false, has_mpu = false;

// ==================== MQ SENSOR CALIBRATION ==========
// MQ sensors output a voltage proportional to log(ppm). Each sensor needs an
// R0 (clean-air resistance). Datasheets give Rs/R0 vs ppm curves; we approximate
// each curve as ppm = a * (Rs/R0)^b in the relevant range.
//
// RL is the load resistor on the breakout board (commonly 10k or 1k — check yours).
// Vc is sensor supply (5V on the module, but ESP32 ADC reads 0-3.3V, so the
// modules already include voltage divider on the AOUT — we read directly).
//
// Calibration step (ONE TIME): run the sketch in clean air for 24h burn-in,
// then measure Rs in clean air and divide by datasheet ratio to get R0.
//   Clean air ratios (Rs_air / R0):
//     MQ-7   : 27.0   (CO)
//     MQ-4   :  4.4   (CH4)
//     MQ-136 :  3.6   (H2S)
//     MQ-2   :  9.83  (Smoke/LPG)

const float VCC          = 5.0;
const float ADC_MAX      = 4095.0;
const float ADC_VREF     = 3.3;
const float RL_MQ        = 10000.0;  // 10k load resistor on most modules

// Clean-air R0 placeholders (replace after burn-in calibration!)
float R0_MQ7   = 10000.0;
float R0_MQ4   = 10000.0;
float R0_MQ136 = 10000.0;
float R0_MQ2   = 10000.0;

// Read raw ADC -> sensor resistance Rs
float readRs(int pin) {
  int raw = analogRead(pin);
  if (raw <= 0) return 1e9;  // open
  float v_out = (raw / ADC_MAX) * ADC_VREF;
  if (v_out >= ADC_VREF - 0.01) return 0.0;
  // module powers the sensor at VCC=5V; AOUT = Vcc * RL / (Rs + RL)
  // -> Rs = RL * (Vcc/Vout - 1)
  return RL_MQ * ((VCC / v_out) - 1.0);
}

// Datasheet-fit power curves: ppm = a * (Rs/R0)^b
float ppm_MQ7_CO   (float ratio) { return 99.042f * pow(ratio, -1.518f); }   // 20-2000 ppm
float ppm_MQ4_CH4  (float ratio) { return 1012.7f * pow(ratio, -2.786f); }   // 200-10000 ppm; we report % LEL (LEL CH4 = 50000 ppm = 5%)
float ppm_MQ136_H2S(float ratio) { return 36.737f * pow(ratio, -3.435f); }   // 1-200 ppm
float ppm_MQ2_LPG  (float ratio) { return 574.25f * pow(ratio, -2.222f); }   // 200-10000 ppm smoke/LPG

// O2 electrochemical sensor (e.g. ME2-O2, Grove O2): linear 0..25% O2 -> 0..0.6V or 0..3V depending module.
// Module here outputs ~0..3V over 0..25% O2. Adjust slope after calibration.
float read_O2_percent() {
  int raw = analogRead(PIN_O2);
  float v = (raw / ADC_MAX) * ADC_VREF;
  // Two-point cal: at 20.9% O2 (open air) we expect v_air. Adjust below.
  const float V_FOR_20_9 = 2.0;   // <- measure in clean air, replace
  return (v / V_FOR_20_9) * 20.9f;
}

// ==================== FALL DETECTION =================
//
// Robust 3-stage algorithm (Bourke et al., widely cited):
//   1) FREE-FALL: |a| drops below ~0.5 g for >100 ms
//   2) IMPACT:   |a| spikes above ~2.5 g within 1 s of free-fall
//   3) STILLNESS:|a-1g| stays below 0.3 g for >2 s post-impact (person not moving)
// All three must hit in sequence to flag a fall. This rejects normal jumps,
// sit-downs, and sudden accelerations from running.
//
const float FF_THRESHOLD   = 0.5f;   // g
const float IMPACT_THRESH  = 2.5f;   // g
const float STILL_THRESH   = 0.3f;   // g (deviation from 1g)
const uint32_t FF_MIN_MS   = 100;
const uint32_t IMPACT_WINDOW_MS = 1000;
const uint32_t STILL_MIN_MS = 2000;

enum FallState { FS_NORMAL, FS_FREEFALL, FS_IMPACT, FS_FALL_CONFIRMED };
FallState fallState = FS_NORMAL;
uint32_t  freefallStart = 0;
uint32_t  impactTime    = 0;
uint32_t  stillStart    = 0;
bool      fallFlag      = false;     // sticky for one transmit cycle
bool      impactFlag    = false;
bool      rotationFlag  = false;

void updateFallDetection(float aMag, float gMag) {
  uint32_t now = millis();

  // rotation event: independent
  if (gMag > 250.0f) rotationFlag = true;

  switch (fallState) {
    case FS_NORMAL:
      if (aMag < FF_THRESHOLD) {
        freefallStart = now;
        fallState = FS_FREEFALL;
      }
      break;

    case FS_FREEFALL:
      if (aMag >= FF_THRESHOLD && (now - freefallStart) < FF_MIN_MS) {
        fallState = FS_NORMAL;  // too short, false alarm
      } else if (aMag > IMPACT_THRESH) {
        if ((now - freefallStart) >= FF_MIN_MS) {
          impactTime = now;
          impactFlag = true;
          stillStart = now;
          fallState = FS_IMPACT;
        } else {
          fallState = FS_NORMAL;
        }
      }
      // give up if no impact within window
      if (now - freefallStart > IMPACT_WINDOW_MS + FF_MIN_MS) {
        fallState = FS_NORMAL;
      }
      break;

    case FS_IMPACT:
      if (fabsf(aMag - 1.0f) > STILL_THRESH) {
        // moved -> reset stillness timer
        stillStart = now;
      }
      if ((now - stillStart) >= STILL_MIN_MS) {
        fallFlag  = true;
        fallState = FS_FALL_CONFIRMED;
      }
      // also bail if too long without confirming
      if (now - impactTime > 8000) fallState = FS_NORMAL;
      break;

    case FS_FALL_CONFIRMED:
      // stays confirmed until cleared after transmit
      break;
  }
}

void clearFallFlagsAfterSend() {
  fallFlag = false; impactFlag = false; rotationFlag = false;
  if (fallState == FS_FALL_CONFIRMED) fallState = FS_NORMAL;
}

// ==================== MAX30102 HR + SpO2 =============
// Compute on a 100-sample rolling buffer (~4 s at 25 Hz)
#define MAX_BUF 100
uint32_t irBuf[MAX_BUF];
uint32_t redBuf[MAX_BUF];
int32_t  spo2 = 98;
int8_t   validSpo2 = 0;
int32_t  heartRate = 75;
int8_t   validHR = 0;

void readMAX30102() {
  if (!has_max) return;
  for (int i = 0; i < MAX_BUF; i++) {
    while (!max30102.available()) max30102.check();
    redBuf[i] = max30102.getRed();
    irBuf[i]  = max30102.getIR();
    max30102.nextSample();
  }
  maxim_heart_rate_and_oxygen_saturation(
    irBuf, MAX_BUF, redBuf,
    &spo2, &validSpo2, &heartRate, &validHR
  );
}

// ==================== SOS BUTTON =====================
volatile bool sosPressed = false;
volatile uint32_t sosPressStart = 0;

void IRAM_ATTR onSosChange() {
  if (digitalRead(PIN_SOS) == LOW) {
    sosPressStart = millis();
  } else {
    if (sosPressStart && (millis() - sosPressStart) > 3000) {
      sosPressed = true;
    }
    sosPressStart = 0;
  }
}

void buzzerPattern(int pattern) {
  // 0=off, 1=warn (short), 2=critical (long)
  if (pattern == 1) {
    digitalWrite(PIN_BUZZER, HIGH); delay(150);
    digitalWrite(PIN_BUZZER, LOW);
  } else if (pattern == 2) {
    for (int i = 0; i < 3; i++) {
      digitalWrite(PIN_BUZZER, HIGH); delay(300);
      digitalWrite(PIN_BUZZER, LOW);  delay(150);
    }
  }
}

// ==================== SETUP ==========================
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n=== MINE GUARD ESP32 boot ===");

  // pins
  pinMode(PIN_FLAME, INPUT);
  pinMode(PIN_SOS,   INPUT_PULLUP);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  attachInterrupt(digitalPinToInterrupt(PIN_SOS), onSosChange, CHANGE);

  // ADC
  analogReadResolution(12);          // 0..4095
  analogSetAttenuation(ADC_11db);    // full 0..3.3 V

  // I2C
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);

  // DHT22
  dht.begin();

  // BME280 @ 0x76
  has_bme = bme.begin(0x76);
  Serial.println(has_bme ? "BME280 ok" : "BME280 NOT FOUND");

  // BMP280 @ 0x77
  has_bmp = bmp.begin(0x77);
  Serial.println(has_bmp ? "BMP280 ok" : "BMP280 NOT FOUND");
  if (has_bmp) {
    bmp.setSampling(Adafruit_BMP280::MODE_NORMAL,
                    Adafruit_BMP280::SAMPLING_X2,
                    Adafruit_BMP280::SAMPLING_X16,
                    Adafruit_BMP280::FILTER_X16,
                    Adafruit_BMP280::STANDBY_MS_500);
  }

  // MAX30102
  has_max = max30102.begin(Wire, I2C_SPEED_FAST);
  if (has_max) {
    // power-of-2 LED amplitude, 411us pulse width, 100Hz sample rate
    max30102.setup(0x1F, 4, 2, 100, 411, 4096);
    Serial.println("MAX30102 ok");
  } else {
    Serial.println("MAX30102 NOT FOUND");
  }

  // MPU6050
  byte mpuStatus = mpu.begin();
  has_mpu = (mpuStatus == 0);
  if (has_mpu) {
    Serial.println("Calibrating MPU6050 (keep still)...");
    delay(1000);
    mpu.calcOffsets(true, true);
    Serial.println("MPU6050 ok");
  } else {
    Serial.printf("MPU6050 init err %d\n", mpuStatus);
  }

  // WiFi
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.print("WiFi");
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 20000) {
    Serial.print(".");
    delay(500);
  }
  Serial.println(WiFi.status() == WL_CONNECTED ? " connected" : " FAILED");
}

// ==================== LOOP ===========================
uint32_t lastSend = 0;

void loop() {
  // continuously update MPU at high rate for fall detection
  if (has_mpu) {
    mpu.update();
    float ax = mpu.getAccX(), ay = mpu.getAccY(), az = mpu.getAccZ();
    float gx = mpu.getGyroX(), gy = mpu.getGyroY(), gz = mpu.getGyroZ();
    float aMag = sqrtf(ax*ax + ay*ay + az*az);
    float gMag = sqrtf(gx*gx + gy*gy + gz*gz);
    updateFallDetection(aMag, gMag);
  }

  // throttle network sends
  if (millis() - lastSend < SEND_PERIOD_MS) {
    delay(20);
    return;
  }
  lastSend = millis();

  // ---- read all sensors ----
  float bodyTemp = dht.readTemperature();
  float bodyHum  = dht.readHumidity();   // unused but read to flush
  if (isnan(bodyTemp)) bodyTemp = 36.5f;

  float airTemp = NAN, airHum = NAN, pressure_hpa = NAN;
  if (has_bme) {
    airTemp      = bme.readTemperature();
    airHum       = bme.readHumidity();
    pressure_hpa = bme.readPressure() / 100.0f;
  }

  // depth from BMP280: ΔP = ρ·g·h, but underground mines we use pressure relative
  // to surface baseline. Backend will compute from raw pressure; we send it raw.
  float bmp_pressure_hpa = NAN;
  if (has_bmp) bmp_pressure_hpa = bmp.readPressure() / 100.0f;

  // MAX30102 (heavy: ~4 s blocking — only every 2 cycles)
  static uint32_t maxCount = 0;
  if (has_max && (maxCount++ % 2 == 0)) readMAX30102();

  // gas sensors
  float rs_co  = readRs(PIN_MQ7_CO);
  float rs_ch4 = readRs(PIN_MQ4_CH4);
  float rs_h2s = readRs(PIN_MQ136_H2S);
  float rs_smk = readRs(PIN_MQ2_SMOKE);

  float co_ppm   = ppm_MQ7_CO  (rs_co  / R0_MQ7);
  float ch4_ppm  = ppm_MQ4_CH4 (rs_ch4 / R0_MQ4);
  float h2s_ppm  = ppm_MQ136_H2S(rs_h2s / R0_MQ136);
  float smoke_ppm= ppm_MQ2_LPG (rs_smk / R0_MQ2);

  // CH4 frontend wants % LEL.  Lower Explosive Limit of methane = 5% v/v in air = 50000 ppm.
  // So % LEL = ppm / 500.
  float ch4_lel  = ch4_ppm / 500.0f;

  float o2_pct = read_O2_percent();
  bool flame = (digitalRead(PIN_FLAME) == LOW);   // most IR flame modules: LOW = flame detected
  bool sos   = sosPressed;

  // ---- buzzer feedback locally on critical ----
  bool any_critical = (co_ppm > 50) || (o2_pct < 19.5) || (h2s_ppm > 10) ||
                      (ch4_lel > 5) || flame || fallFlag || sos ||
                      (validSpo2 && spo2 < 92);
  bool any_warn = !any_critical && (
                      (co_ppm > 35) || (h2s_ppm > 5) || (ch4_lel > 1) ||
                      (smoke_ppm > 50) || (validHR && (heartRate > 100 || heartRate < 50))
                  );
  if (any_critical) buzzerPattern(2);
  else if (any_warn) buzzerPattern(1);

  // ---- build JSON ----
  StaticJsonDocument<1024> doc;
  doc["id"]    = DEVICE_ID;
  doc["name"]  = DEVICE_NAME;
  doc["zone"]  = DEVICE_ZONE;
  doc["level"] = DEVICE_LEVEL;

  JsonObject d = doc.createNestedObject("data");
  d["hr"]       = (validHR  ? heartRate : -1);
  d["spo2"]     = (validSpo2 ? spo2     : -1);
  d["bodytemp"] = bodyTemp;
  d["co"]       = round(co_ppm);
  d["ch4"]      = round(ch4_lel * 100) / 100.0;   // 2 dp
  d["h2s"]      = round(h2s_ppm);
  d["o2"]       = round(o2_pct * 10) / 10.0;
  d["smoke"]    = round(smoke_ppm);
  d["flame"]    = flame;
  d["airtemp"]  = isnan(airTemp) ? bodyTemp : airTemp;
  d["humidity"] = isnan(airHum) ? 0 : airHum;
  d["pressure"] = isnan(bmp_pressure_hpa) ? (isnan(pressure_hpa) ? 1013.25 : pressure_hpa) : bmp_pressure_hpa;
  d["fall"]     = fallFlag;
  d["impact"]   = impactFlag;
  d["rotation"] = rotationFlag;
  d["sos"]      = sos;

  // accel/gyro snapshot for supervisor
  if (has_mpu) {
    d["ax"] = mpu.getAccX();
    d["ay"] = mpu.getAccY();
    d["az"] = mpu.getAccZ();
    d["gz"] = mpu.getGyroZ();
  }

  // ---- POST ----
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(BACKEND_URL);
    http.addHeader("Content-Type", "application/json");
    String body;
    serializeJson(doc, body);
    int code = http.POST(body);
    Serial.printf("POST %d  CO=%.0f  HR=%d  SpO2=%d  fall=%d  sos=%d\n",
                  code, co_ppm, (int)heartRate, (int)spo2, fallFlag, sos);
    http.end();
  } else {
    Serial.println("WiFi down, queuing not implemented");
  }

  // clear sticky flags only after successful frame
  clearFallFlagsAfterSend();
  if (sos) sosPressed = false;
}
