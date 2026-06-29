# Hệ thống điều khiển và giám sát NEMA17 Stepper Motor trên Raspberry Pi 3

## Tổng quan kiến trúc

```
Firmware (Raspberry Pi 3)
  ├─► MQTTService  ──► MQTT Broker (localhost:1883) ──► Unity (realtime)
  └─► SupabaseClient ──► Supabase REST API ──► Database (persistence)
```

Hai đường song song, cùng dữ liệu, khác đích đến:
- **MQTT**: Unity đọc realtime để hiển thị dashboard / cảnh báo
- **Supabase**: lưu trữ lịch sử, web app đọc

---

## Phần cứng (Hardware)

### GPIO Mapping

| GPIO | Chức năng | Ghi chú |
|------|-----------|---------|
| 15 | A4988 EN (active LOW) | 0=enable, 1=disable |
| 25 | A4988 STEP | Phát xung waveform DMA |
| 8 | A4988 DIR | Chiều quay |
| 21 | Encoder quang OUT | 20 slots/rev, RISING_EDGE |
| 20 | DS18B20 Data | 1-Wire, kernel quản lý |
| 23 | Relay quạt | Active HIGH |
| 16 | Nút K1 | Giảm -50 RPM (active LOW) |
| 12 | Nút K2 | Tăng +50 RPM (active LOW) |
| 7 | Nút K3 | Cycle mode |
| 26 | Nút K4 | Bật/Tắt motor |

**GPIO bị cấm** (pigpio từ chối): GPIO0, GPIO1 (I2C EEPROM), GPIO2, GPIO3 (I2C).

### Thiết bị

- **NEMA17 Stepper**: 200 steps/rev, driver A4988
- **Encoder quang**: 20 slots/rev, đo RPM bằng period-based (không đếm xung)
- **DS18B20**: nhiệt độ qua `/sys/bus/w1/devices/28-*/w1_slave`
- **Relay quạt**: bật khi nhiệt > 40°C, tắt khi < 35°C

---

## File chính

| File | Vai trò |
|------|---------|
| `motor_firmware_rpi.py` | Firmware chính — PID, encoder, cảm biến, MQTT, Supabase |
| `health_algorithm.py` | Tính Health Index, Wear, RUL, Failure Probability |
| `mqtt_service.py` | MQTTService class + topic constants |
| `supabase_service.py` | SupabaseService class (tham khảo; firmware dùng SupabaseClient inline) |
| `timing_log.csv` | Ghi thời gian ramp-up thực tế vs lý thuyết (tự tạo khi chạy) |

---

## Thông số cấu hình chính

```python
DEVICE_ID             = "stepper_01"
DEFAULT_RPM           = 60
MIN_RPM / MAX_RPM     = 1 / 700
STEPS_PER_REV         = 200

# Encoder
ENCODER_SLOTS_PER_REV = 20
ENCODER_GLITCH_US     = 2000     # lọc nhiễu phần cứng (µs)
ENCODER_VALID_MAX_RPM = 550      # ngưỡng lọc xung giả
ENCODER_PERIOD_BUF    = 8        # median buffer
ENCODER_EMA_TAU_S     = 0.35     # làm mượt EMA

# PID
PID_KP / KI / KD      = 0.5 / 0.08 / 0.0
CONTROL_DT_S          = 0.12     # chu kỳ vòng điều khiển
PID_TRIM_UP_FRAC      = 0.05     # PID chỉ đẩy tối đa +5% trên feedforward
PID_TRIM_DOWN_FRAC    = 0.30

# Soft-ramp
RAMP_STEP_RPM         = 3        # RPM tăng/giảm mỗi chu kỳ
START_RPM             = 20

# Stall detection
STALL_FRAC            = 0.40     # rpm < 40% feedforward → nghi stall
STALL_TIME_S          = 3.5

# Nhiệt độ
AUTO_FAN_ON_TEMP      = 40.0
AUTO_FAN_OFF_TEMP     = 35.0
TEMP_HIGH_WARNING_C   = 60.0
TEMP_READ_INTERVAL_S  = 3

# Auto mode
AUTO_TEMP_LOW/HIGH    = 30.0 / 55.0
AUTO_RPM_MIN/MAX      = 50 / 450    # de-rating: nóng → chậm

# Logging
HEARTBEAT_INTERVAL_S  = 12
SENSOR_LOG_INTERVAL_S = 3
```

---

## Kiến trúc PID (4 cải tiến cốt lõi)

1. **Period-based encoder**: đo µs giữa 2 xung (không đếm xung/cửa sổ) → độ phân giải mịn, trễ thấp
2. **Feedforward soft-ramp**: `_slew_target` tăng dần 3 RPM/cycle → không trượt bước khi tăng tốc đột ngột
3. **PID trim quanh feedforward**: chỉ được ±% quanh tần số nền, không đẩy lên MAX_RPM
4. **Stall detection**: rpm < 40% feedforward liên tục > 3.5s → cảnh báo log

**Thời gian ramp lý thuyết**: `(target - start_rpm) / 3 × 0.12s`
Ví dụ: 60 → 300 RPM ≈ 9.6 giây

---

## Health Algorithm (`health_algorithm.py`)

### Công thức

```
permanent_wear = clamp(W_TEMP × temp_dmg + W_RUNTIME × runtime_dmg)
max_wear       = max(permanent_wear, prev_max_degradation)   ← ratchet 1 chiều

rpm_penalty    = W_RPM × rpm_dmg   (nếu motor đang chạy, không nạp vào ratchet)

degradation    = clamp(max_wear + rpm_penalty)
health_score   = (1 - degradation) × 100
wear_level     = max_wear × 100
remaining_life = BASE_LIFE_HOURS × (1 - max_wear)^1.5
failure_prob   = 1 - exp(-(t_eq / 20000)^2)     ← Weibull beta=2
```

### Hằng số vật lý

```python
BASE_LIFE_HOURS = 20000.0    # tuổi thọ thiết kế (giờ)
TEMP_NORMAL     = 30.0
TEMP_MAX        = 80.0
REL_ERR_MAX     = 0.50       # sai số RPM 50% = hỏng
W_TEMP          = 0.3
W_RPM           = 0.3        # chỉ phạt tức thời, không ratchet
W_RUNTIME       = 0.4
```

### status_rule (nhãn phân loại)

| Rule | Điều kiện |
|------|-----------|
| FAILURE | temp ≥ 72°C / rpm_err ≥ 20% / HI < 40 |
| DANGER | temp ≥ 58°C / rpm_err ≥ 8% / HI < 60 |
| WARNING | temp ≥ 45°C / rpm_err ≥ 2% / HI < 80 |
| NORMAL | còn lại |

---

## MQTT Topics

| Topic | Hướng | QoS | Retain | Nội dung |
|-------|-------|-----|--------|---------|
| `cps/control/cmd` | Unity→Pi | 1 | - | Lệnh điều khiển |
| `cps/raw/sensor` | Pi→Unity | 0 | No | rpm, temp, wave_rpm, is_stalled, ... |
| `cps/control/state` | Pi→Unity | 1 | Yes | motor_on, fan, mode |
| `cps/event/log` | Pi→Unity | 1 | No | event_name, severity |
| `cps/system/heartbeat` | Pi→Unity | 1 | Yes | status, uptime_s |
| `cps/cyber/result` | Pi→Unity | 1 | Yes | health_score, wear_level, RUL, ... |

### Cấu trúc lệnh MQTT từ Unity

```json
{ "motor": true, "rpm": 300, "mode": "manual", "fan": false }
```

---

## Supabase Tables

| Bảng | Dữ liệu chính |
|------|---------------|
| `raw_sensor` | device_id, rpm, temperature, target_rpm, status_rule, rpm_error, wave_rpm, is_stalled |
| `cyber_results` | device_id, health_score, wear_level, failure_probability, remaining_life_hours, avg_temperature, avg_rpm, overheat_count, fan_activation_count, total_hours, today_runtime_sec |
| `control_state` | device_id, motor, fan, mode |
| `events` | device_id, event_name, severity |
| `heartbeat` | device_id, status, uptime_s — INSERT mới mỗi lần |
| `control_commands` | device_id, source (mqtt/web), motor, fan, mode, target_rpm |

### Env vars bắt buộc

```bash
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_KEY="<anon_or_service_key>"
```

---

## Command Queue (Priority)

```
0 = Button (K1/K2/K3/K4)   ← cao nhất
1 = MQTT  (Unity)
2 = Web   (React app qua Supabase, poll mỗi 2s)
3 = Auto  (auto fan)        ← thấp nhất
```

### Payload lệnh

```python
# Button
{"action": "rpm_down" | "rpm_up" | "cycle_mode" | "toggle_power"}

# MQTT / Web (subset tùy ý)
{"motor": bool, "rpm": int, "mode": "auto"|"manual", "fan": bool}

# Auto fan (nội bộ)
{"action": "auto_fan_on" | "auto_fan_off", "temp": float}
```

---

## Thread Architecture

```
Main thread
  ├── PID loop          (StepperMotor._ramp_loop, 0.12s/cycle)
  ├── db_worker         (ghi Supabase qua queue, không block PID)
  ├── cmd_worker        (xử lý command queue tuần tự)
  ├── bg_loop           (heartbeat 12s, poll web 2s, log sensor 3s)
  ├── temp_update       (DS18B20, spawn mỗi 3s, non-blocking)
  ├── EncoderReader cb  (pigpio interrupt, mỗi xung encoder)
  ├── ButtonController  (pigpio interrupt, debounce 250ms)
  └── FanController cb  (pigpio guardian, chặn EMI từ stepper wave)
```

---

## Timing Log (`timing_log.csv`)

Ghi tự động mỗi khi motor hoàn thành ramp:

```
timestamp,metric,value_ms,detail
2026-06-25T10:30:17,ramp,14200.00,60->300 RPM theory=14.40s
```

- `value_ms`: thời gian thực tế (ms)
- `detail`: RPM start→end + lý thuyết để so sánh
- Nếu thực tế >> lý thuyết: dấu hiệu stall hoặc encoder lag

---

## Yêu cầu hệ thống

```bash
# Daemon bắt buộc
sudo systemctl enable --now pigpiod

# Python env
~/motor_health/env  (venv, Python 3.13)

# Thư mục làm việc trên Pi
~/motor_health/test/

# Chạy firmware
cd ~/motor_health/test && python3 motor_firmware_rpi.py
```

### Dependencies chính

```
pigpio, paho-mqtt, supabase, numpy, joblib
```

**Lưu ý numpy trên Pi**: nếu lỗi `libopenblas.so.0 not found`, chạy:
```bash
pip uninstall numpy -y && pip install "numpy==2.4.4" --no-cache-dir
```
