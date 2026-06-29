"""
motor_firmware_rpi.py  —  FIRMWARE DIEU KHIEN PHAN CUNG RPi  [v6]
================================================================================
KIEN TRUC v6 — HAI DUONG SONG SONG, CUNG DU LIEU:

    Firmware
      ├─► MQTTService (mqtt_service.py)
      │       └─► MQTT Broker ──► Unity (realtime)
      └─► SupabaseClient (nội bộ, _db_queue, có fallback cột tùy chọn)
              └─► Supabase REST API ──► database (persistence)

Khong con bridge. Firmware tu ghi thang len 2 noi.
Du lieu giong het nhau, chi khac dich den.

HEARTBEAT:
  - MQTT: publish_heartbeat() -> cps/system/heartbeat (Unity/dashboard)
  - Supabase: log_heartbeat() -> bang heartbeat, dung UPDATE->INSERT
    (khong can UNIQUE constraint, chi can cot last_seen)

YEU CAU SQL (chay 1 lan):
    ALTER TABLE heartbeat
      ADD COLUMN IF NOT EXISTS last_seen timestamptz DEFAULT now();

Web check online:
    SELECT * FROM heartbeat
    WHERE device_id = 'stepper_01'
      AND last_seen > now() - interval '2 minutes'

YEU CAU pigpio daemon:
    sudo apt install pigpio && sudo systemctl enable --now pigpiod

CAU HINH:
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_KEY="<key>"

CHAY:
    python motor_firmware_rpi_pid_v6.py
================================================================================
"""

import itertools
import os
import re
import glob
import json
import logging
import queue
import signal
import sys
import time
import threading

from health_algorithm import (calculate_health, auto_target_rpm,
                              initial_max_wear_from_runtime)

import paho.mqtt.client as mqtt  # giu lai de pigpio khong bi loi import chain
from supabase import create_client, Client

# [v6] Import 2 service rieng biet
from mqtt_service import (
    MQTTService,
    TOPIC_CONTROL_CMD,
    TOPIC_RAW_SENSOR,
    TOPIC_CONTROL_STATE,
    TOPIC_EVENT_LOG,
    TOPIC_HEARTBEAT,
    TOPIC_CYBER_RESULT,
)


# ── pigpio: bắt buộc chạy thật trên Raspberry Pi. Không còn chế độ mô phỏng ──
try:
    import pigpio
    _pi = pigpio.pi()
    ON_PI = bool(_pi.connected)
except Exception:
    pigpio = None
    _pi = None
    ON_PI = False


# ============================== CẤU HÌNH ==============================
DEVICE_ID = "stepper_01"

PIN_EN      = 15      # A4988 EN  (active LOW: 0=enable, 1=disable)
PIN_STEP    = 25      # A4988 STEP (phát xung waveform)
PIN_DIR     = 8       # A4988 DIR
PIN_TEMP    = 20      # DS18B20 (1-Wire, kernel quản lý)
PIN_ENCODER = 21      # Encoder quang OUT
PIN_FAN     = 23      # Relay quạt (active HIGH) — cần tắt serial console OS: xem README

# Nút bấm chức năng (active LOW — nối GND, pull-up nội)
PIN_BTN_K1  = 16   # K1: Giảm tốc -50 RPM
PIN_BTN_K2  = 12   # K2: Tăng tốc +50 RPM
PIN_BTN_K3  = 7    # K3: Cycle mode (auto → manual+fan_on → manual+fan_off → manual)
PIN_BTN_K4  = 26   # K4: Bật/Tắt motor  (đổi từ GPIO1 vì GPIO0/1 bị pigpio chặn - I2C ID EEPROM)
#
# GPIO BỊ CẤM trên RPi (pigpio từ chối quyền truy cập):
#   GPIO0, GPIO1  — I2C ID EEPROM (SCL/SDA)
#   GPIO2, GPIO3  — I2C SDA/SCL (nếu bật I2C)
# GPIO an toàn còn trống: 4, 5, 6, 17, 18, 22, 23, 24, 26, 27
BTN_DEBOUNCE_MS = 250   # chống rung phần mềm (ms)
RPM_BTN_STEP    = 50    # bước +/- RPM mỗi lần nhấn K1/K2

STEPS_PER_REV = 200
DEFAULT_RPM   = 60
MIN_RPM       = 1
MAX_RPM       = 700          # giới hạn lệnh STEP/waveform
DEFAULT_DIR   = True

# Encoder phải chấp nhận tốc độ cao hơn giới hạn lệnh một khoảng dự phòng.
# Nếu dùng đúng MAX_RPM để lọc chu kỳ, các xung hợp lệ gần 300 RPM có thể bị
# loại do jitter, làm RPM đo bị thấp giả tạo.
ENCODER_VALID_MAX_RPM = 550   # ngưỡng lọc xung; tăng _MIN_PERIOD_US lên ~5.5ms để loại xung rung ở RPM thấp

ENCODER_SLOTS_PER_REV = 20
ENCODER_GLITCH_US     = 2000   # loc nhieu phan cung (us); 2ms an toan voi 500 RPM (period ~6ms)

TEMP_HIGH_WARNING_C  = 60.0
TEMP_READ_INTERVAL_S = 3

# Tự động bật/tắt quạt theo nhiệt độ (chế độ auto)
AUTO_FAN_ON_TEMP  = 40.0   # bật quạt khi nhiệt > 40°C
AUTO_FAN_OFF_TEMP = 35.0   # tắt quạt khi nhiệt < 35°C (khoảng trễ hysteresis)

MQTT_HOST = "localhost"
MQTT_PORT = 1883

# Không hard-code key trong file. Đặt bằng biến môi trường trên Raspberry Pi:
#   export SUPABASE_URL="https://xxxx.supabase.co"
#   export SUPABASE_KEY="<anon_or_service_key>"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://mhloddjfpujjynkjsnlf.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", 
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1obG9kZGpmcHVqanlua2pzbmxmIiwicm9sZSI6ImFub24"
    "iLCJpYXQiOjE3Nzk1MDE4MjIsImV4cCI6MjA5NTA3NzgyMn0"
    ".zkXVXJadWWbC5yrJAm-OqX55Zyxn26y1VKXQrZNJMTc"
)

HEARTBEAT_INTERVAL_S  = 12
SENSOR_LOG_INTERVAL_S = 3

# Auto mode — chỉnh theo dải RPM motor này (hướng de-rating: nóng -> chậm)
AUTO_TEMP_LOW  = 30.0
AUTO_TEMP_HIGH = 55.0
AUTO_RPM_MAX   = 450
AUTO_RPM_MIN   = 50

# ── PID vòng kín tốc độ (BẢN CẢI TIẾN) ──────────────────────────────────────
# Kiến trúc: FEEDFORWARD (tần số xung = target) + PI TRIM bám sai số đo.
#   commanded_rpm = clamp( slew_target(FF) + Kp*e + Ki*∫e dt - Kd*de/dt ,  quanh FF )
#
# Bốn cải tiến cốt lõi để PID chạy tốt trên stepper + encoder 20 PPR:
#   (1) FEEDBACK NHANH: encoder đo CHU KỲ giữa xung (period-based) thay vì đếm
#       xung/cửa-sổ, lọc median + EMA NGẮN -> độ phân giải mịn, độ trễ thấp,
#       hợp với vòng điều khiển nhanh.
#   (2) KẸP TRIM QUANH FEEDFORWARD: PID chỉ được chỉnh ±% quanh tần số nền,
#       KHÔNG cho đẩy lệnh chạy tới MAX_RPM -> chặn "stall-runaway" của stepper.
#   (3) ĐÓNG BĂNG KHÂU I KHI ĐANG RAMP: trong lúc soft-ramp, đo còn trễ nên I sẽ
#       tích lũy oan -> chỉ bật I sau khi feedforward đã tới target (hết vọt lố).
#   (4) PHÁT HIỆN STALL: nếu rpm đo tụt sâu dưới feedforward kéo dài -> cảnh báo
#       (và tùy chọn tự dừng) thay vì để PID đẩy mạnh thêm làm trượt bước nặng hơn.
#
# QUY TRÌNH CHỈNH GAIN TRÊN MOTOR THẬT (làm theo thứ tự):
#   1) Ki=Kd=0. Tăng Kp tới khi rpm bắt đầu DAO ĐỘNG đều quanh target -> đó là Ku,
#      ghi lại chu kỳ dao động Tu (giây). Đặt Kp = 0.45*Ku (PI) hoặc 0.6*Ku (PID).
#   2) Đặt Ki = 0.54*Ku / Tu  (PI cổ điển Ziegler-Nichols). Tăng/giảm để hết sai
#      số thường trực mà không dao động kéo dài.
#   3) Để Kd = 0 với 20 PPR. Chỉ thêm Kd = 0.075*Ku*Tu (rất nhỏ) nếu cần bớt vọt lố,
#      và phải có PID_D_FILT_TAU đủ lớn để không khuếch đại nhiễu.
PID_KP         = 0.5    # điểm khởi đầu dịu; chỉnh theo quy trình trên
PID_KI         = 0.08   # bù sai số thường trực; tăng lên 0.15 nếu còn lệch
PID_KD         = 0.0    # giữ 0 với encoder 20 PPR
PID_D_FILT_TAU = 0.20   # hằng số lọc thông thấp cho khâu D (s)
CONTROL_DT_S   = 0.12   # chu kỳ vòng điều khiển; nhanh hơn nhờ feedback period-based

# (2) Kẹp lệnh PID quanh feedforward — chống stall-runaway của stepper.
#     Cho PID kéo XUỐNG nhiều hơn (an toàn) nhưng ĐẨY LÊN ít (trên FF = nguy cơ trượt bước).
PID_TRIM_UP_FRAC   = 0.05   # commanded <= FF * 1.05 — giam de PID khong over-drive khi gan gioi han mo-men
PID_TRIM_DOWN_FRAC = 0.30   # commanded >= FF * 0.70

# (4) Phát hiện stall: rpm đo < STALL_FRAC * feedforward liên tục > STALL_TIME_S.
STALL_FRAC      = 0.40   # đo tụt dưới 40% feedforward = nghi trượt bước
STALL_TIME_S    = 3.5    # keo dai bao lau thi coi la stall
STALL_AUTOSTOP  = False  # True = tu dung motor khi stall; False = chi canh bao + kep PID

# (1) Encoder period-based: lọc & làm mượt
ENCODER_PERIOD_BUF   = 8     # số chu kỳ gần nhất để lấy trung vị
ENCODER_EMA_TAU_S    = 0.35  # EMA NGẮN cho rpm đo (khớp vòng điều khiển ~0.12s)
ENCODER_ZERO_TIMEOUT = 3.0   # im lặng > 3× chu kỳ kỳ vọng -> rpm về 0

# [v5] TOPIC_* constants da duoc chuyen sang mqtt_service.py va import o tren.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler("motor_controller.log", encoding="utf-8")],
)
logger = logging.getLogger("main")

if pigpio is None:
    sys.exit("pigpio chua cai trong moi truong Python hien tai. Cai: pip install pigpio | sudo apt install pigpio python3-pigpio -y")
elif not ON_PI:
    sys.exit("pigpiod chua chay/khong ket noi. Chay: sudo systemctl enable --now pigpiod")
else:
    logger.info("pigpio daemon OK - dieu khien phan cung that")


# ============================== SupabaseClient ==============================
# Dùng cho cả READ (seed, poll) và WRITE (qua _db_queue background thread).
# Có fallback tự động bỏ cột tùy chọn chưa tồn tại (wave_rpm, is_stalled).
class SupabaseClient:
    TABLE_COLUMNS = {
        "raw_sensor": {"device_id", "rpm", "temperature", "target_rpm",
                       "status_rule", "rpm_error", "wave_rpm",
                       "is_stalled", "created_at"},
        "cyber_results": {"device_id", "health_score", "wear_level",
                          "failure_probability", "remaining_life_hours",
                          "avg_temperature", "avg_rpm", "overheat_count",
                          "fan_activation_count", "total_hours",
                          "today_runtime_sec",
                          "created_at"},
        "control_state": {"device_id", "motor", "fan", "mode", "created_at"},
        "events": {"device_id", "event_name", "severity", "created_at"},
        "heartbeat": {"device_id", "status", "uptime_s", "created_at"},
        "control_commands": {"device_id", "source", "motor", "fan", "mode",
                             "target_rpm", "revolutions", "created_at"},
    }

    def __init__(self, url, key):
        self._client = create_client(url, key)
        # Các cột debug PID là tùy chọn. Nếu DB cũ chưa có, firmware sẽ tự bỏ
        # cột thiếu và thử INSERT lại để dữ liệu chính vẫn tiếp tục cập nhật.
        self._optional_columns = {
            "raw_sensor": {"wave_rpm", "is_stalled"},
        }
        self._disabled_columns = {}
        logger.info("Supabase client khoi tao: %s", url)

    def _filter(self, table, payload):
        allowed = self.TABLE_COLUMNS.get(table, set())
        disabled = self._disabled_columns.get(table, set())
        return {k: v for k, v in payload.items()
                if k in allowed and k not in disabled}

    def _insert(self, table, payload):
        clean = self._filter(table, payload)
        if not clean:
            return False

        # Có thể phải bỏ lần lượt nhiều cột tùy chọn chưa tồn tại.
        for _attempt in range(3):
            try:
                self._client.table(table).insert(clean).execute()
                return True
            except Exception as e:
                message = str(e)
                match = re.search(
                    r"Could not find the '([^']+)' column of '([^']+)'",
                    message
                )
                if match:
                    missing_column, missing_table = match.groups()
                    optional = self._optional_columns.get(table, set())
                    if (missing_table == table
                            and missing_column in optional
                            and missing_column in clean):
                        self._disabled_columns.setdefault(table, set()).add(
                            missing_column
                        )
                        clean.pop(missing_column, None)
                        logger.warning(
                            "Supabase %s chua co cot tuy chon '%s'; "
                            "tam bo cot nay va thu INSERT lai. "
                            "Du lieu chinh van duoc ghi.",
                            table, missing_column
                        )
                        if not clean:
                            return False
                        continue

                logger.error("Supabase INSERT %s: %s", table, e)
                return False

        logger.error("Supabase INSERT %s that bai sau khi thu bo cot tuy chon.", table)
        return False

    def log_sensor(self, device_id, rpm, temperature=None, target_rpm=None,
                   status_rule=None, rpm_error=None,
                   wave_rpm=None, is_stalled=None):
        payload = {"device_id": device_id, "rpm": int(round(rpm))}
        if temperature is not None:
            payload["temperature"] = round(temperature, 2)
        if target_rpm is not None:
            payload["target_rpm"] = int(target_rpm)
        if status_rule is not None:
            payload["status_rule"] = status_rule
        if rpm_error is not None:
            payload["rpm_error"] = round(rpm_error, 2)
        if wave_rpm is not None:
            payload["wave_rpm"] = round(float(wave_rpm), 2)
        if is_stalled is not None:
            payload["is_stalled"] = bool(is_stalled)
        return self._insert("raw_sensor", payload)

    def log_cyber_results(self, device_id, res, avg_temperature=None, avg_rpm=None,
                          overheat_count=0, fan_activation_count=0,
                          total_hours=None, today_runtime_sec=None):
        payload = {
            "device_id": device_id,
            "health_score":         res["health_score"],
            "wear_level":           res["wear_level"],
            "failure_probability":  res["failure_probability"],
            "remaining_life_hours": res["remaining_life_hours"],
            "overheat_count":       overheat_count,
            "fan_activation_count": fan_activation_count,
        }
        if avg_temperature is not None:
            payload["avg_temperature"] = round(avg_temperature, 2)
        if avg_rpm is not None:
            payload["avg_rpm"] = int(round(avg_rpm))
        if total_hours is not None:
            payload["total_hours"] = round(total_hours, 4)
        if today_runtime_sec is not None:
            payload["today_runtime_sec"] = int(today_runtime_sec)
        return self._insert("cyber_results", payload)

    def get_latest_result(self, device_id):
        try:
            r = (self._client.table("cyber_results")
                 .select("wear_level, total_hours")
                 .eq("device_id", device_id)
                 .order("created_at", desc=True).limit(1).execute())
            if r.data:
                return r.data[0]
        except Exception as e:
            logger.warning("Khong seed duoc cyber_results: %s", e)
        return None

    def log_command(self, device_id, motor=None, target_rpm=None, mode="manual",
                source="mqtt", fan=False, revolutions=None):
        """
        Vô hiệu hóa ghi control_commands từ Pi.

        Theo kiến trúc hiện tại:
        - WEB ghi control_commands
        - Pi đọc control_commands
        - Pi thực thi
        - Pi ghi control_state

        Pi không ghi ngược vào control_commands.
        """
        logger.debug("Bo qua log_command: control_commands chi danh cho WEB.")
        return True

    def log_control_state(self, device_id, motor, mode="manual", fan=False):
        return self._insert("control_state",
                            {"device_id": device_id, "motor": motor,
                             "fan": fan, "mode": mode})

    def log_event(self, device_id, event_name, severity="info"):
        valid = {"info", "warning", "error", "critical"}
        return self._insert("events",
                            {"device_id": device_id, "event_name": event_name,
                             "severity": severity if severity in valid else "info"})

    def log_heartbeat(self, device_id, uptime_s, status="online"):
        return self._insert("heartbeat",
                            {"device_id": device_id, "status": status,
                             "uptime_s": int(uptime_s)})


# [v5] MQTTService da duoc chuyen sang mqtt_service.py.
# Import o dau file: from mqtt_service import MQTTService, ...

# ============================== EncoderReader (pigpio callback) ==============================
class EncoderReader:
    """
    Đọc RPM bằng ĐO CHU KỲ giữa hai xung (period-based) + lọc trung vị + EMA ngắn.

    TẠI SAO period-based (cải tiến #1 cho PID):
      - Đếm-xung-theo-cửa-sổ ở 20 PPR rất thô: ở 60 RPM chỉ ~20 xung/giây,
        lệch 1 xung = sai ~5-10%; ở RPM thấp còn tệ hơn.
      - Đo µs giữa hai xung cho độ phân giải mịn ở MỌI tốc độ và độ trễ thấp,
        nên PID có feedback "sạch + nhanh" để bám target mà không dao động.
      - KHÔNG phụ thuộc nhịp gọi update_rpm() (không chia cho dt-giữa-các-lần-gọi),
        nên gọi ở 0.12s (vòng PID) hay 1s (lúc motor tắt) đều đúng.

    Lọc:
      - glitch filter phần cứng + bỏ chu kỳ ngắn bất thường (< chu kỳ tại MAX_RPM).
      - trung vị trên ENCODER_PERIOD_BUF chu kỳ gần nhất (diệt outlier do rung bước).
      - EMA ngắn ENCODER_EMA_TAU_S để mượt nhẹ, vẫn bám kịp vòng điều khiển.
      - zero-timeout: im lặng quá lâu -> rpm về 0.
    """
    _MIN_PERIOD_US = int(60_000_000 / (ENCODER_VALID_MAX_RPM * ENCODER_SLOTS_PER_REV))  # có headroom chống loại nhầm xung

    def __init__(self, pin=PIN_ENCODER, slots_per_rev=ENCODER_SLOTS_PER_REV):
        self.pin = pin
        self.slots_per_rev = slots_per_rev
        self._lock = threading.Lock()
        self._rpm = 0.0
        self._period_buf = []          # các chu kỳ hợp lệ gần nhất (µs)
        self._last_tick = None         # pigpio tick của xung trước
        self._last_valid_t = time.monotonic()
        self._last_update_t = time.monotonic()
        self._cb = None

        _pi.set_mode(pin, pigpio.INPUT)
        _pi.set_pull_up_down(pin, pigpio.PUD_UP)
        _pi.set_glitch_filter(pin, ENCODER_GLITCH_US)
        self._cb = _pi.callback(pin, pigpio.RISING_EDGE, self._on_edge)
        logger.info("Encoder (period-based) GPIO%s | %s khe/vong | glitch=%dus | min_period=%dus",
                    pin, slots_per_rev, ENCODER_GLITCH_US, self._MIN_PERIOD_US)

    def _on_edge(self, gpio, level, tick):
        with self._lock:
            if self._last_tick is not None:
                dt_us = pigpio.tickDiff(self._last_tick, tick)
                if dt_us >= self._MIN_PERIOD_US:     # bỏ xung giả/rung bước đến quá sớm
                    self._period_buf.append(dt_us)
                    if len(self._period_buf) > ENCODER_PERIOD_BUF:
                        self._period_buf.pop(0)
                    self._last_valid_t = time.monotonic()
            self._last_tick = tick

    def update_rpm(self):
        now = time.monotonic()
        dt_call = now - self._last_update_t
        self._last_update_t = now

        with self._lock:
            periods = list(self._period_buf)
            last_valid = self._last_valid_t

        if len(periods) < 2:
            self._rpm *= 0.6                          # chưa đủ mẫu -> hạ dần về 0
            return round(self._rpm, 1)

        # Trung vị + cắt đuôi nhẹ để loại outlier còn sót
        s = sorted(periods)
        n = len(s)
        trim = n // 4 if n >= 4 else 0
        core = s[trim:n - trim] if trim else s
        avg_us = sum(core) / len(core)

        # Zero-timeout: không có xung mới trong > N× chu kỳ kỳ vọng -> về 0
        expected_s = avg_us / 1_000_000.0
        if now - last_valid > ENCODER_ZERO_TIMEOUT * expected_s:
            self._rpm *= 0.6
            return round(self._rpm, 1)

        rpm_measured = 60_000_000.0 / (avg_us * self.slots_per_rev)
        # EMA ngắn thích nghi theo nhịp gọi thực (dt_call) để giữ hằng số thời gian ~tau
        alpha = min(1.0, dt_call / max(1e-3, ENCODER_EMA_TAU_S))
        self._rpm = alpha * rpm_measured + (1.0 - alpha) * self._rpm
        return round(self._rpm, 1)

    def reset(self):
        """Xóa dữ liệu chu kỳ cũ khi motor khởi động lại."""
        with self._lock:
            self._rpm = 0.0
            self._period_buf.clear()
            self._last_tick = None
            self._last_valid_t = time.monotonic()
            self._last_update_t = time.monotonic()

    @property
    def rpm(self):
        """RPM đã lọc + EMA ngắn (đọc để PID dùng và để LOG)."""
        return round(self._rpm, 1)

    def cleanup(self):
        if self._cb is not None:
            self._cb.cancel()


# ============================== PIDController ==============================
def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


class PIDController:
    """PID rời rạc: anti-windup, lọc khâu D, đạo hàm theo PHÉP ĐO (tránh đá setpoint).
    update() trả về LỆNH RPM cuối (đã gồm feedforward) và đã kẹp trong [out_min, out_max]."""

    def __init__(self, kp, ki, kd, out_min, out_max, d_filter_tau=PID_D_FILT_TAU):
        self.kp, self.ki, self.kd = kp, ki, kd
        self.out_min, self.out_max = out_min, out_max
        self.d_filter_tau = d_filter_tau
        self.reset()

    def reset(self):
        self._integral = 0.0
        self._prev_meas = None
        self._d_filt = 0.0

    def update(self, setpoint, measurement, dt, feedforward=0.0,
               freeze_integral=False, out_lo=None, out_hi=None):
        # out_lo/out_hi: giới hạn ĐỘNG quanh feedforward (chống stall-runaway).
        # Mặc định dùng giới hạn tĩnh [out_min, out_max] nếu không truyền.
        lo = self.out_min if out_lo is None else out_lo
        hi = self.out_max if out_hi is None else out_hi
        if dt <= 0:
            return _clamp(feedforward, lo, hi)
        error = setpoint - measurement

        # P
        p = self.kp * error

        # D theo phép đo + lọc thông thấp (giảm khuếch đại nhiễu encoder)
        if self._prev_meas is None:
            d_meas = 0.0
        else:
            d_meas = (measurement - self._prev_meas) / dt
        self._prev_meas = measurement
        a = dt / (self.d_filter_tau + dt)
        self._d_filt += a * (d_meas - self._d_filt)
        d = -self.kd * self._d_filt

        # I — ĐÓNG BĂNG thật sự khi đang ramp. Lưu giá trị cũ để có thể
        # rollback nếu đầu ra bão hòa; tránh tích phân bị kéo âm/dương oan.
        integral_before = self._integral
        if not freeze_integral:
            self._integral += self.ki * error * dt

        out_unsat = feedforward + p + self._integral + d
        out = _clamp(out_unsat, lo, hi)

        # Conditional-integration anti-windup:
        # nếu bão hòa và sai số còn đẩy sâu hơn vào vùng bão hòa thì hủy phần
        # tích phân vừa cộng ở chu kỳ này. Khi freeze_integral=True, I không đổi.
        if not freeze_integral and out != out_unsat:
            drives_further_high = out_unsat > hi and error > 0
            drives_further_low = out_unsat < lo and error < 0
            if drives_further_high or drives_further_low:
                self._integral = integral_before

        return out


# ============================== StepperMotor (pigpio waveform) ==============================
class StepperMotor:
    """
    Phát xung STEP bằng waveform pigpio (DMA) trên GPIO25.
    Bản này KHÔNG có mock. VÒNG KÍN PID: soft-ramp feedforward chống trượt bước,
    PID trim quanh feedforward để rpm ĐO ĐƯỢC bám target.
    """
    START_RPM = 20
    RAMP_STEP_RPM = 3
    RAMP_INTERVAL_S = 0.15
    STOP_RPM = 5

    def __init__(self, encoder):
        self._target_rpm = DEFAULT_RPM
        self._wave_rpm = 0
        self._direction = DEFAULT_DIR
        self._running = False
        self._stopping = False
        self._lock = threading.RLock()
        self._encoder = encoder
        self._wave_id = None
        self._ramp_stop = threading.Event()
        self._ramp_thread = None

        # Theo dõi stall (cải tiến #4)
        self._stalled = False
        self._stall_since = None       # mốc thời gian rpm bắt đầu tụt sâu dưới FF

        # Vòng kín PID: trim quanh feedforward (slew_target), kẹp trong [MIN_RPM, MAX_RPM]
        self.pid = PIDController(PID_KP, PID_KI, PID_KD,
                                 out_min=MIN_RPM, out_max=MAX_RPM)
        self._slew_target = 0.0   # feedforward đã qua giới hạn slew (soft-ramp chống stall)

        _pi.set_mode(PIN_EN, pigpio.OUTPUT)
        _pi.set_mode(PIN_STEP, pigpio.OUTPUT)
        _pi.set_mode(PIN_DIR, pigpio.OUTPUT)
        _pi.write(PIN_EN, 1)       # A4988 EN active LOW: 1=disable
        _pi.write(PIN_DIR, 1 if DEFAULT_DIR else 0)
        _pi.write(PIN_STEP, 0)
        logger.info("StepperMotor REAL (pigpio wave) EN=%s STEP=%s DIR=%s",
                    PIN_EN, PIN_STEP, PIN_DIR)

    def _send_wave_at_rpm(self, rpm):
        rpm = max(MIN_RPM, min(MAX_RPM, int(rpm)))
        steps_per_sec = rpm * STEPS_PER_REV / 60.0
        if steps_per_sec <= 0:
            return

        half_us = max(3, int(round(1_000_000 / (2 * steps_per_sec))))
        _pi.wave_add_new()
        _pi.wave_add_generic([
            pigpio.pulse(1 << PIN_STEP, 0, half_us),
            pigpio.pulse(0, 1 << PIN_STEP, half_us),
        ])
        new_wid = _pi.wave_create()
        if new_wid < 0:
            logger.error("Khong tao duoc pigpio wave, code=%s", new_wid)
            return

        _pi.wave_send_repeat(new_wid)
        old = self._wave_id
        self._wave_id = new_wid
        self._wave_rpm = rpm
        if old is not None:
            try:
                _pi.wave_delete(old)
            except Exception:
                pass

    def _stop_wave_and_disable(self):
        _pi.wave_tx_stop()
        if self._wave_id is not None:
            try:
                _pi.wave_delete(self._wave_id)
            except Exception:
                pass
            self._wave_id = None
        self._wave_rpm = 0
        _pi.write(PIN_STEP, 0)
        _pi.write(PIN_EN, 1)

    def _ramp_loop(self):
        # VÒNG ĐIỀU KHIỂN PID (chu kỳ cố định): đọc encoder -> tính PID -> phát xung.
        # Đúng pseudocode điều khiển: đọc cảm biến -> tính -> xuất cơ cấu chấp hành -> lặp.
        self.pid.reset()
        last = time.monotonic()
        while not self._ramp_stop.is_set():
            now = time.monotonic()
            dt = now - last
            if dt < CONTROL_DT_S:                 # giữ nhịp đều
                time.sleep(CONTROL_DT_S - dt)
                now = time.monotonic()
                dt = now - last
            last = now

            with self._lock:
                if not self._running:
                    return
                stopping = self._stopping
                target = float(self._target_rpm)

                # SOFT-RAMP feedforward: chống trượt bước + chống PID nhảy sốc khi đổi target
                if stopping:
                    self._slew_target = max(0.0, self._slew_target - self.RAMP_STEP_RPM)
                else:
                    if self._slew_target < self.START_RPM and self._slew_target < target:
                        self._slew_target = float(self.START_RPM)
                    if self._slew_target < target:
                        self._slew_target = min(target, self._slew_target + self.RAMP_STEP_RPM)
                    elif self._slew_target > target:
                        self._slew_target = max(target, self._slew_target - self.RAMP_STEP_RPM)

                # Dừng hẳn khi feedforward đã giảm về ngưỡng STOP
                if stopping and self._slew_target <= self.STOP_RPM:
                    self._stop_wave_and_disable()
                    self._running = False
                    self._stopping = False
                    self._stalled = False
                    self._stall_since = None
                    logger.info("Motor TAT sau khi giam toc")
                    return

                ff = self._slew_target
                # Đang ramp = feedforward chưa tới target (hoặc đang dừng) -> đóng băng khâu I
                slewing = stopping or (int(ff) != int(target))


            # ĐỌC PHÉP ĐO (ngoài lock — update_rpm tự khóa riêng)
            measured = self._encoder.update_rpm()

            # (2) Kẹp lệnh PID quanh feedforward: chặn stall-runaway của stepper.
            #     Cho kéo xuống nhiều, đẩy lên ít (trên FF dễ trượt bước).
            out_hi = min(MAX_RPM, ff * (1.0 + PID_TRIM_UP_FRAC)) if ff > 0 else MAX_RPM
            out_lo = max(MIN_RPM, ff * (1.0 - PID_TRIM_DOWN_FRAC)) if ff > 0 else MIN_RPM

            # (3) PID trim quanh feedforward; I đóng băng khi đang ramp
            commanded = self.pid.update(ff, measured, dt,
                                        feedforward=ff,
                                        freeze_integral=slewing,
                                        out_lo=out_lo, out_hi=out_hi)

            # (4) Phát hiện stall: rpm đo tụt sâu dưới FF kéo dài khi KHÔNG ramp
            if (not slewing) and ff >= self.START_RPM and measured < STALL_FRAC * ff:
                if self._stall_since is None:
                    self._stall_since = now
                elif now - self._stall_since >= STALL_TIME_S and not self._stalled:
                    self._stalled = True
                    logger.warning("STALL nghi ngo: ff=%.0f nhung do=%.0f RPM (>%.1fs). "
                                   "PID da bi kep quanh FF de khong day them.",
                                   ff, measured, STALL_TIME_S)
                    if STALL_AUTOSTOP:
                        self._stopping = True
            else:
                self._stall_since = None
                if self._stalled and measured >= STALL_FRAC * ff:
                    self._stalled = False
                    logger.info("Het stall: rpm do da hoi lai (%.0f).", measured)

            with self._lock:
                if self._running and abs(commanded - self._wave_rpm) >= 3.0:
                    self._send_wave_at_rpm(commanded)

    def _ensure_ramp_thread(self):
        if self._ramp_thread is None or not self._ramp_thread.is_alive():
            self._ramp_stop.clear()
            self._ramp_thread = threading.Thread(target=self._ramp_loop,
                                                 daemon=True, name="motor_ramp")
            self._ramp_thread.start()

    def start(self):
        with self._lock:
            if self._running:
                self._stopping = False
                self._ensure_ramp_thread()
                return
            _pi.write(PIN_DIR, 1 if self._direction else 0)
            _pi.write(PIN_EN, 0)
            self._running = True
            self._stopping = False
            self._wave_rpm = 0
            self._slew_target = 0.0    # PID loop sẽ ramp feedforward từ 0 lên
            self._stalled = False
            self._stall_since = None
            self._encoder.reset()
            self._send_wave_at_rpm(min(self._target_rpm, self.START_RPM))
            self._ensure_ramp_thread()
        logger.info("Motor BAT - target RPM=%s", self._target_rpm)

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._stopping = True
            self._ensure_ramp_thread()
        logger.info("Motor dang giam toc de dung")

    def emergency_stop(self):
        with self._lock:
            self._ramp_stop.set()
            self._stop_wave_and_disable()
            self._running = False
            self._stopping = False
            self._stalled = False
            self._stall_since = None
        logger.info("Motor TAT khan cap")

    def set_rpm(self, rpm):
        rpm = max(MIN_RPM, min(MAX_RPM, int(rpm)))
        with self._lock:
            if rpm == self._target_rpm and not self._stopping:
                return
            old_target = self._target_rpm
            self._target_rpm = rpm
            self._stopping = False

            if rpm >= MAX_RPM:
                logger.warning(
                    "Target=%d RPM bang gioi han waveform. PID khong con "
                    "headroom de bu tang toc; neu rpm thuc thap hon target, "
                    "hay giam setpoint hoac tang gioi han phan cung sau khi "
                    "kiem tra an toan.",
                    rpm
                )

            # Đổi target lớn: xóa tích phân cũ để PID không kéo lệch từ trạng thái trước.
            if abs(rpm - old_target) >= self.RAMP_STEP_RPM:
                self.pid.reset()
                self._stalled = False
                self._stall_since = None

            if self._running:
                self._ensure_ramp_thread()
        logger.info("Set target RPM=%s", rpm)

    def cleanup(self):
        self.emergency_stop()

    @property
    def is_running(self):
        return self._running

    @property
    def is_stopping(self):
        return self._stopping

    @property
    def is_ramping(self):
        # "Đang ramp" = giai đoạn soft-ramp feedforward chưa tới target.
        # KHÔNG so _wave_rpm với target vì PID luôn trim quanh target -> sẽ kẹt mãi.
        return self._running and (self._stopping
                                  or int(self._slew_target) != int(self._target_rpm))

    @property
    def set_rpm_value(self):
        return self._target_rpm

    @property
    def wave_rpm(self):
        return self._wave_rpm

    @property
    def is_stalled(self):
        return self._stalled


# ============================== DS18B20 ==============================
class DS18B20TemperatureSensor:
    W1_DEVICE_GLOB = "/sys/bus/w1/devices/28-*/w1_slave"

    def __init__(self):
        self._device_path = self._find_device()
        self._temperature = None
        self._lock = threading.Lock()       # bảo vệ _temperature khi đọc
        self._read_lock = threading.Lock()  # chặn concurrent _read_raw()
        if self._device_path:
            logger.info("DS18B20: %s", self._device_path)
        else:
            logger.warning("Khong tim thay DS18B20. Cot temperature se ghi NULL/bo qua gia tri nhiet that.")

    def _find_device(self):
        d = glob.glob(self.W1_DEVICE_GLOB)
        return d[0] if d else None

    def _read_raw(self):
        if not self._device_path:
            return None
        try:
            with open(self._device_path) as f:
                lines = f.readlines()
            if len(lines) < 2 or not lines[0].strip().endswith("YES"):
                return None
            temp_c = int(lines[1].split("t=")[-1].strip()) / 1000.0
            if temp_c in (85.0, -127.0):
                return None
            return round(temp_c, 2)
        except Exception as e:
            logger.error("DS18B20 loi: %s", e)
            return None

    def update(self):
        # Non-blocking: nếu lần đọc trước chưa xong thì bỏ qua, giữ giá trị cũ.
        # DS18B20 cần ~750ms convert; nếu spawn thread mới mỗi 5s mà thread cũ
        # còn đang block thì hai thread cùng mở w1_slave → kernel w1 trả lỗi.
        if not self._read_lock.acquire(blocking=False):
            logger.debug("DS18B20: read dang chay, bo qua lan nay")
            return self._temperature
        try:
            temp = self._read_raw()
            if temp is not None:
                with self._lock:
                    self._temperature = temp
            return self._temperature
        finally:
            self._read_lock.release()

    @property
    def temperature(self):
        with self._lock:
            return self._temperature


# ============================== FanController (relay GPIO14) ==============================
class FanController:
    """
    Điều khiển quạt qua relay nối GPIO14 (active HIGH).

    Vấn đề: DMA waveform của stepper (GPIO25) tạo EMI coupling vào GPIO14
    qua trace PCB, kích relay mỗi ~5s khi motor chạy. Refresh 200ms không
    đủ vì relay hút sau ~10ms.

    Giải pháp — GUARDIAN CALLBACK:
      pigpio callback đăng ký trên EITHER_EDGE của GPIO14. Khi có bất kỳ
      thay đổi trái phép nào (EMI hoặc UART re-claim ALT0), callback kích
      hoạt trong ~10-50µs — nhanh hơn thời gian hút relay ~200 lần.
      Callback gọi set_mode(OUTPUT) + write() để:
        1. Re-claim GPIO khỏi UART nếu kernel đã lấy lại ALT0.
        2. Khôi phục mức điện áp đúng trước khi cuộn dây relay kịp tích từ.

    Lưu ý flag-before-write trong turn_on/turn_off: _on phải được set TRƯỚC
    khi gọi _pi.write() để callback không nhầm thay đổi hợp lệ là can thiệp.
    """

    def __init__(self, pin=PIN_FAN):
        self.pin = pin
        self._on = False
        self._lock = threading.Lock()
        self._guard_cb = None
        _pi.set_mode(pin, pigpio.OUTPUT)
        _pi.write(pin, 0)
        # Đăng ký guardian — phải sau write(0) để lần thay đổi đầu tiên
        # (0→0 không có edge) không kích callback ngay khi khởi động.
        self._guard_cb = _pi.callback(pin, pigpio.EITHER_EDGE, self._guard_edge)
        logger.info("FanController: relay GPIO%s (active HIGH) + guardian callback", pin)

    def _guard_edge(self, gpio, level, tick):
        """Pigpio gọi callback này ngay khi GPIO14 thay đổi (~10-50µs).
        Nếu level khác _on: re-claim GPIO khỏi UART/ALT0 rồi write lại."""
        expected = 1 if self._on else 0
        if level != expected:
            # set_mode trước: re-claim GPIO14 nếu kernel UART đã lấy lại ALT0
            _pi.set_mode(self.pin, pigpio.OUTPUT)
            _pi.write(self.pin, expected)
            logger.warning(
                "GPIO%d guardian: can thiep phat hien (level=%d mong=%d) — "
                "da reclaim va sua lai",
                gpio, level, expected,
            )

    def turn_on(self):
        with self._lock:
            if not self._on:
                self._on = True          # flag TRƯỚC: callback thấy expected=1
                _pi.set_mode(self.pin, pigpio.OUTPUT)
                _pi.write(self.pin, 1)
                logger.info("Quat BAT (GPIO%s = HIGH)", self.pin)

    def turn_off(self):
        with self._lock:
            if self._on:
                self._on = False         # flag TRƯỚC: callback thấy expected=0
                _pi.set_mode(self.pin, pigpio.OUTPUT)
                _pi.write(self.pin, 0)
                logger.info("Quat TAT (GPIO%s = LOW)", self.pin)

    def set(self, on: bool):
        self.turn_on() if on else self.turn_off()

    @property
    def is_on(self) -> bool:
        with self._lock:
            return self._on

    def cleanup(self):
        if self._guard_cb is not None:
            self._guard_cb.cancel()
            self._guard_cb = None
        with self._lock:
            self._on = False
        _pi.write(self.pin, 0)
        logger.info("FanController cleanup: quat da tat, guardian da huy")


# ============================== Trạng thái hiển thị ==============================
def health_status_from_score(health_score):
    """Trạng thái sức khỏe dài hạn theo cùng ngưỡng HI đang dùng trên Web."""
    if health_score >= 80.0:
        return "NORMAL"
    if health_score >= 60.0:
        return "WARNING"
    if health_score >= 40.0:
        return "DANGER"
    return "FAILURE"


# ============================== ButtonController ==============================
class ButtonController:
    """
    Xu ly 4 nut bam K1-K4 qua pigpio callback (FALLING_EDGE = nut nhan xuong GND).
    Chong rung bang thoi gian BTN_DEBOUNCE_MS gio moi lan xu ly.

    K1 (GPIO16): Giam toc -50 RPM (chi manual mode, motor dang chay)
    K2 (GPIO12): Tang toc +50 RPM (chi manual mode, motor dang chay)
    K3 (GPIO7) : Cycle mode:
                   0=auto  ->  1=manual+fan_ON  ->  2=manual+fan_OFF  ->  3=manual  -> (lap lai)
    K4 (GPIO26): Bat/Tat motor (toggle)
    """

    def __init__(self, on_k1, on_k2, on_k3, on_k4):
        self._handlers    = {}
        self._last_t      = {}
        self._callbacks   = []

        pin_map = {
            PIN_BTN_K1: ("K1", on_k1),
            PIN_BTN_K2: ("K2", on_k2),
            PIN_BTN_K3: ("K3", on_k3),
            PIN_BTN_K4: ("K4", on_k4),
        }

        for pin, (label, handler) in pin_map.items():
            try:
                _pi.set_mode(pin, pigpio.INPUT)
                _pi.set_pull_up_down(pin, pigpio.PUD_UP)
                cb = _pi.callback(pin, pigpio.FALLING_EDGE, self._on_edge)
                self._handlers[pin] = handler
                self._last_t[pin]   = 0.0
                self._callbacks.append(cb)
                logger.info("Button %s GPIO%d: OK", label, pin)
            except Exception as exc:
                logger.error("Button %s GPIO%d: KHONG KHOI TAO DUOC — %s "
                             "(kiem tra pin co bi cam khong, doi sang GPIO khac)",
                             label, pin, exc)

        logger.info("ButtonController: K1=GPIO%d K2=GPIO%d K3=GPIO%d K4=GPIO%d",
                    PIN_BTN_K1, PIN_BTN_K2, PIN_BTN_K3, PIN_BTN_K4)

    def _on_edge(self, gpio, level, tick):
        now = time.monotonic()
        if now - self._last_t.get(gpio, 0) < BTN_DEBOUNCE_MS / 1000.0:
            return                        # bo qua rung
        self._last_t[gpio] = now
        handler = self._handlers.get(gpio)
        if handler:
            try:
                handler()
            except Exception as exc:
                logger.error("Button GPIO%d handler loi: %s", gpio, exc)

    def cleanup(self):
        for cb in self._callbacks:
            cb.cancel()
        logger.info("ButtonController cleanup xong.")


# ============================== MotorController ==============================
class MotorController:
    def __init__(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            sys.exit("Thieu SUPABASE_URL / SUPABASE_KEY trong bien moi truong.")

        self.encoder = EncoderReader(PIN_ENCODER, ENCODER_SLOTS_PER_REV)
        self.temp_sensor = DS18B20TemperatureSensor()
        self.motor = StepperMotor(self.encoder)
        self.fan = FanController(PIN_FAN)
        # Supabase: dùng SupabaseClient nội bộ (có fallback cột tùy chọn) + _db_queue
        # MQTT: dùng MQTTService từ mqtt_service.py (Unity realtime)
        self.supabase = SupabaseClient(SUPABASE_URL, SUPABASE_KEY)
        self.mqtt = MQTTService(MQTT_HOST, MQTT_PORT, DEVICE_ID,
                                on_command=self._on_command)
        # Background queue cho Supabase writes (không block vòng điều khiển)
        self._db_queue = queue.Queue()
        self._db_thread = threading.Thread(target=self._db_worker, daemon=True,
                                           name="db_writer")
        self._db_thread.start()
        # Nut bam vat ly
        self._k3_state = 0   # 0=auto  1=manual+fan_on  2=manual+fan_off
        self.buttons = ButtonController(
            on_k1=self._btn_decrease_rpm,
            on_k2=self._btn_increase_rpm,
            on_k3=self._btn_cycle_mode,
            on_k4=self._btn_toggle_power,
        )

        self._start_time = time.monotonic()
        self._running = False
        self._last_temp_warn = 0.0

        self._mode = "manual"
        self._max_degradation = 0.0
        self._runtime_s = 0.0
        self._overheat_count = 0
        self._fan_activation_count = 0
        self._last_sensor_t = time.monotonic()
        self._last_target = None
        self._target_changed_at = time.monotonic()
        # Cua so "dong bang" danh gia sau cu doi target LON, dai DONG theo do lon
        # cu nhay: nen + thoi gian ramp uoc luong + bu EMA hoi tu. Tranh cham diem
        # mau luc dong co dang DUOI toc (chinh la mau lam HI tut oan o ban cu).
        self._stable_delay_base_s = 3.0      # nen toi thieu sau doi target
        self._settle_margin_s     = 1.5      # bu thoi gian EMA hoi tu
        self._significant_delta_rpm = 20
        self._settle_until = 0.0             # moc het dong bang (tinh theo cu nhay)
        self._last_good_health_result = None
        # Hội tụ theo SAI SỐ ĐO THỰC (cho PID): coi như đã ổn định khi |rpm_error|
        # nằm trong dải nhỏ liên tục đủ số mẫu. _settle_until đóng vai DEADLINE TỐI ĐA
        # (giới hạn cửa sổ NULL) -> sau deadline mà rpm vẫn lệch lớn = lỗi THẬT, không bị che.
        self._pid_settle_margin_s   = 4.0    # nới thêm cho PID vọt lố/hồi tụ
        self._settle_band_rpm       = 8      # |rpm - target| <= dải này coi là đã bám
        self._settle_consec_needed  = 2      # cần liên tục bao nhiêu mẫu trong dải
        self._converged_count       = 0

        seed = self.supabase.get_latest_result(DEVICE_ID)
        if seed:
            self._runtime_s = float(seed.get("total_hours") or 0) * 3600.0
            self._max_degradation = initial_max_wear_from_runtime(
                self._runtime_s / 3600.0)
            logger.info("Seed: runtime=%.2fh -> wear_floor=%.2f%%",
                        self._runtime_s / 3600, self._max_degradation * 100)

        # Nhan lenh Web ghi vao bang control_commands.
        self._last_web_cmd_id    = 0
        self._web_poll_interval_s = 2.0
        self._last_web_poll      = 0.0
        self._web_poll_lock      = threading.Lock()
        self._seed_last_web_cmd_id()
        # (log đã được ghi bên trong _seed_last_web_cmd_id)

        # ── Priority command queue: button=0 > mqtt=1 > web=2 > auto=3 ──────
        # Tat ca input duoc enqueue va xu ly TUAN TU boi _cmd_worker.
        # Khong con race condition tren _mode, _fan_activation_count, v.v.
        self._cmd_q   = queue.PriorityQueue()
        self._cmd_seq = itertools.count()          # so thu tu FIFO trong cung priority
        self._CMD_PRI = {"button": 0, "mqtt": 1, "web": 2, "auto": 3}
        threading.Thread(target=self._cmd_worker, daemon=True,
                         name="cmd_worker").start()

    # ══════════════════════════════════════════════════════════════════════════
    # SUPABASE BACKGROUND QUEUE  (giữ nguyên pattern v3 - không block vòng PID)
    # ══════════════════════════════════════════════════════════════════════════

    def _db_worker(self):
        while True:
            fn, args, kwargs = self._db_queue.get()
            try:
                fn(*args, **kwargs)
            except Exception as e:
                logger.error("DB write error: %s", e)
            finally:
                self._db_queue.task_done()

    def _db(self, fn, *args, **kwargs):
        """Enqueue Supabase write - non-blocking, thread-safe."""
        self._db_queue.put((fn, args, kwargs))

    def _seed_last_web_cmd_id(self):
        """Không phát lại lệnh Web cũ khi firmware vừa khởi động."""
        try:
            r = (self.supabase._client
                .table("control_commands")
                .select("id")
                .eq("device_id", DEVICE_ID)
                .eq("source", "web")
                .order("id", desc=True)
                .limit(1)
                .execute())
            if r.data:
                self._last_web_cmd_id = int(r.data[0]["id"])
                logger.info("Web cmd seed: last_id=%s", self._last_web_cmd_id)
        except Exception as e:
            logger.warning("Khong seed duoc last_web_cmd_id: %s", e)

    # ══════════════════════════════════════════════════════════════════════════
    # PRIORITY COMMAND QUEUE
    # Tat ca nguon input (button / MQTT / web / auto-fan) enqueue tai day.
    # _cmd_worker xu ly tuan tu theo thu tu: button > mqtt > web > auto.
    # Khong con race condition tren _mode, _fan_activation_count, v.v.
    # ══════════════════════════════════════════════════════════════════════════

    def _enqueue_cmd(self, source: str, payload: dict):
        """Dat lenh vao queue. Thread-safe, tra ve ngay."""
        pri = self._CMD_PRI.get(source, 9)
        seq = next(self._cmd_seq)          # FIFO tiebreaker trong cung priority
        self._cmd_q.put((pri, seq, source, payload))

    def _cmd_worker(self):
        """Worker thread duy nhat xu ly lenh - khong race condition."""
        while True:
            pri, seq, source, payload = self._cmd_q.get()
            try:
                self._execute_cmd(source, payload)
            except Exception as exc:
                logger.error("[%s] cmd loi: %s | payload=%s", source, exc, payload)
            finally:
                self._cmd_q.task_done()

    def _pub_state(self, cmd_ts=None):
        """Phat trang thai hien tai len MQTT + ghi Supabase (dung sau moi thay doi)."""
        motor_on = self.motor.is_running and not self.motor.is_stopping
        self.mqtt.publish_control_state(motor_on, self._mode, self.fan.is_on, ts=cmd_ts)
        self._db(self.supabase.log_control_state, DEVICE_ID, motor_on, self._mode, self.fan.is_on)

    def _execute_cmd(self, source: str, payload: dict):
        """
        Xu ly lenh - CHI duoc goi tu _cmd_worker (single thread).
        Khong can lock them vi moi lenh chay tuan tu.

        source: "button" | "mqtt" | "web" | "auto"
        payload:
          - Button K1: {"action": "rpm_down"}
          - Button K2: {"action": "rpm_up"}
          - Button K3: {"action": "cycle_mode"}
          - Button K4: {"action": "toggle_power"}
          - Auto fan:  {"action": "auto_fan_on"|"auto_fan_off", "temp": float}
          - MQTT/Web:  {"motor": bool, "rpm": int, "mode": str, "fan": bool}  (bat ky subset)
        """
        cmd_ts = payload.pop("_cmd_ts", None)
        action = payload.get("action")

        # ── Button K1: giam toc ───────────────────────────────────────────────
        if action == "rpm_down":
            if self._mode != "manual" or not self.motor.is_running:
                return
            new_rpm = max(MIN_RPM, self.motor.set_rpm_value - RPM_BTN_STEP)
            self.motor.set_rpm(new_rpm)
            logger.info("[button] K1: RPM -> %d", new_rpm)
            self._pub_state(cmd_ts)
            self._db(self.supabase.log_event, DEVICE_ID, f"rpm_down_{new_rpm}", "info")
            return

        # ── Button K2: tang toc ───────────────────────────────────────────────
        if action == "rpm_up":
            if self._mode != "manual" or not self.motor.is_running:
                return
            new_rpm = min(MAX_RPM, self.motor.set_rpm_value + RPM_BTN_STEP)
            self.motor.set_rpm(new_rpm)
            logger.info("[button] K2: RPM -> %d", new_rpm)
            self._pub_state(cmd_ts)
            self._db(self.supabase.log_event, DEVICE_ID, f"rpm_up_{new_rpm}", "info")
            return

        # ── Button K3: cycle mode ─────────────────────────────────────────────
        if action == "cycle_mode":
            self._k3_state = (self._k3_state + 1) % 3
            if self._k3_state == 0:
                self._mode = "auto"
                logger.info("[button] K3: -> AUTO")         
            elif self._k3_state == 1:
                self._mode = "manual"
                if not self.fan.is_on:
                    self.fan.turn_on()
                    self._fan_activation_count += 1
                    self._db(self.supabase.log_event, DEVICE_ID, "fan_on", "info")
                logger.info("[button] K3: -> MANUAL + fan ON")
            else:
                self._mode = "manual"
                if self.fan.is_on:
                    self.fan.turn_off()
                    self._db(self.supabase.log_event, DEVICE_ID, "fan_off", "info")
                logger.info("[button] K3: -> MANUAL + fan OFF")
            _K3_LABELS = {0: "btn_k3_auto", 1: "btn_k3_manual_fan_on", 2: "btn_k3_manual_fan_off"}
            k3_event = _K3_LABELS[self._k3_state]
            self._pub_state(cmd_ts)
            self.mqtt.publish_event(k3_event, "info")
            self._db(self.supabase.log_event, DEVICE_ID, k3_event, "info")
            return

        # ── Button K4: bat/tat motor ──────────────────────────────────────────
        if action == "toggle_power":
            if self.motor.is_running and not self.motor.is_stopping:
                self.motor.stop()
                logger.info("[button] K4: Motor TAT")
                self.mqtt.publish_event("btn_motor_off", "info")
                self._db(self.supabase.log_event, DEVICE_ID, "btn_motor_off", "info")
            else:
                self.motor.start()
                logger.info("[button] K4: Motor BAT")
                self.mqtt.publish_event("btn_motor_on", "info")
                self._db(self.supabase.log_event, DEVICE_ID, "btn_motor_on", "info")
            self._pub_state(cmd_ts)
            return

        # ── Auto fan (nguon "auto", priority thap nhat) ───────────────────────
        if action == "auto_fan_on":
            if self._mode != "auto" or self.fan.is_on:
                return                  # da thay doi boi lenh uu tien cao hon
            self.fan.turn_on()
            self._fan_activation_count += 1
            temp = payload.get("temp", 0)
            logger.info("[auto] fan ON: %.1f°C > %.0f°C", temp, AUTO_FAN_ON_TEMP)
            self.mqtt.publish_event("auto_fan_on", "info")
            self._db(self.supabase.log_event, DEVICE_ID, "auto_fan_on", "info")
            self._pub_state(cmd_ts)
            return

        if action == "auto_fan_off":
            if self._mode != "auto" or not self.fan.is_on:
                return
            self.fan.turn_off()
            temp = payload.get("temp", 0)
            logger.info("[auto] fan OFF: %.1f°C < %.0f°C", temp, AUTO_FAN_OFF_TEMP)
            self.mqtt.publish_event("auto_fan_off", "info")
            self._db(self.supabase.log_event, DEVICE_ID, "auto_fan_off", "info")
            self._pub_state(cmd_ts)
            return

        # ── MQTT / Web: lenh tieu chuan ───────────────────────────────────────
        logger.info("[%s] lenh: %s", source, payload)
        changed   = False
        rpm_cmd   = None
        motor_cmd = None

        if "mode" in payload and str(payload["mode"]).lower() in ("manual", "auto"):
            self._mode = str(payload["mode"]).lower()
            changed = True
            logger.info("mode -> %s", self._mode)

        if "rpm" in payload and self._mode == "manual":
            try:
                rpm = int(payload["rpm"])
                if MIN_RPM <= rpm <= MAX_RPM:
                    self.motor.set_rpm(rpm)
                    rpm_cmd = rpm
                    changed = True
                else:
                    logger.warning("rpm ngoai [%d-%d]: %s", MIN_RPM, MAX_RPM, rpm)
            except (ValueError, TypeError):
                logger.warning("rpm khong hop le: %s", payload["rpm"])

        if "fan" in payload:
            fan_cmd  = bool(payload["fan"])
            prev_fan = self.fan.is_on
            self.fan.set(fan_cmd)
            if fan_cmd and not prev_fan:
                self._fan_activation_count += 1
                self.mqtt.publish_event("fan_manual_on", "info")
                self._db(self.supabase.log_event, DEVICE_ID, "fan_manual_on", "info")
            elif not fan_cmd and prev_fan:
                self.mqtt.publish_event("fan_manual_off", "info")
                self._db(self.supabase.log_event, DEVICE_ID, "fan_manual_off", "info")
            if fan_cmd != prev_fan:
                changed = True

        if "motor" in payload:
            cmd = bool(payload["motor"])
            motor_cmd = cmd

            # Nếu lệnh motor đến từ WEB:
            # - Web đã tự ghi log sự kiện.
            # - Pi chỉ thực thi và cập nhật control_state.
            # - Pi không ghi thêm events để tránh trùng log.
            should_log_motor_event = source != "web"

            if cmd and not self.motor.is_running:
                self.motor.start()

                if should_log_motor_event:
                    self.mqtt.publish_event("motor_start", "info")
                    self._db(self.supabase.log_event, DEVICE_ID, "motor_start", "info")

                changed = True

            elif not cmd and self.motor.is_running:
                self.motor.stop()

                if should_log_motor_event:
                    self.mqtt.publish_event("motor_stop", "info")
                    self._db(self.supabase.log_event, DEVICE_ID, "motor_stop", "info")

                changed = True


        # Ghi log_command CHỈ khi MQTT/button phát lệnh MỚI chưa có trong DB.
        # Khi source="web": lệnh đã tồn tại trong control_commands (do React insert),
        # firmware KHÔNG ghi lại vì sẽ tạo row source="web" mới → bị poll lại → vòng lặp.

        if changed:
            self._pub_state(cmd_ts if source == "mqtt" else None)

    # ── Poll web commands ────────────────────────────────────────────────────

    def _poll_web_commands(self):
        """Doc lenh web moi, enqueue voi priority 'web'."""
        if not self._web_poll_lock.acquire(blocking=False):
            return
        try:
            try:
                r = (self.supabase._client
                     .table("control_commands")
                     .select("id, motor, fan, mode, target_rpm")
                     .eq("device_id", DEVICE_ID)
                     .eq("source", "web")          # CHỈ poll lệnh do React app gửi
                     .gt("id", self._last_web_cmd_id)
                     .order("id", desc=False)
                     .limit(10)
                     .execute())
            except Exception as e:
                logger.warning("Poll web cmd loi: %s", e)
                return

            for cmd in (r.data or []):
                self._last_web_cmd_id = int(cmd["id"])
                payload = {}
                if cmd.get("motor")      is not None: payload["motor"] = cmd["motor"]
                if cmd.get("mode")       is not None: payload["mode"]  = str(cmd["mode"]).lower()
                if cmd.get("target_rpm") is not None: payload["rpm"]   = cmd["target_rpm"]
                if cmd.get("fan")        is not None: payload["fan"]   = cmd["fan"]
                if payload:
                    logger.info("[web] cmd id=%s: %s", cmd["id"], payload)
                    self._enqueue_cmd("web", payload)
        finally:
            self._web_poll_lock.release()

    # ── Button handlers: chi enqueue, khong xu ly truc tiep ─────────────────

    def _btn_decrease_rpm(self):
        self._enqueue_cmd("button", {"action": "rpm_down"})

    def _btn_increase_rpm(self):
        self._enqueue_cmd("button", {"action": "rpm_up"})

    def _btn_cycle_mode(self):
        self._enqueue_cmd("button", {"action": "cycle_mode"})

    def _btn_toggle_power(self):
        self._enqueue_cmd("button", {"action": "toggle_power"})

    # ── MQTT callback: chi enqueue, khong xu ly truc tiep ───────────────────

    def _on_command(self, topic, payload):
        cmd_ts = payload.get("ts")
        if cmd_ts is not None:
            payload["_cmd_ts"] = cmd_ts
        self._enqueue_cmd("mqtt", payload)

    # ── Auto fan: enqueue voi priority thap nhat ─────────────────────────────

    def _check_auto_fan(self, temp: float | None):
        """Kiem tra nhiet do, enqueue lenh quat neu can. Chi hieu luc trong auto mode."""
        if temp is None:
            return
        if temp > AUTO_FAN_ON_TEMP and not self.fan.is_on:
            self._enqueue_cmd("auto", {"action": "auto_fan_on", "temp": temp})
        elif temp < AUTO_FAN_OFF_TEMP and self.fan.is_on:
            self._enqueue_cmd("auto", {"action": "auto_fan_off", "temp": temp})

    def _check_temp_warning(self, temp):
          if temp is None or temp < TEMP_HIGH_WARNING_C:
              return

          self._overheat_count += 1
          now = time.monotonic()

          if now - self._last_temp_warn >= 60:
              logger.warning("Nhiet cao: %.1f C", temp)

              self.mqtt.publish_event("temp_high_warning", "warning")
              self._db(self.supabase.log_event, DEVICE_ID, "temp_high_warning", "warning")

              self._last_temp_warn = now

    def _compute_and_log(self, actual_rpm, temp):
        now = time.monotonic()
        dt = now - self._last_sensor_t
        self._last_sensor_t = now

        # Auto mode chỉ được chỉnh RPM khi motor đang chạy ổn định.
        # Khi vừa bấm OFF, motor vẫn is_running=True trong lúc soft-stop,
        # nhưng is_stopping=True. Nếu vẫn gọi set_rpm(), motor sẽ bị bật lại.                               
        if (self._mode == "auto"
                and self.motor.is_running
                and not self.motor.is_stopping
                and temp is not None):
            auto_t = auto_target_rpm(temp,
                                    temp_low=AUTO_TEMP_LOW, temp_high=AUTO_TEMP_HIGH,
                                    rpm_min=AUTO_RPM_MIN, rpm_max=AUTO_RPM_MAX)
            self.motor.set_rpm(int(auto_t))

        target = self.motor.set_rpm_value if self.motor.is_running else 0

        # Khi target doi DANG KE: dat DEADLINE dong bang = nen + thoi gian ramp uoc luong
        # + bu PID. Day la GIOI HAN TREN cua cua so NULL. Auto nhich vai RPM -> khong reset.
        if self._last_target is None or abs(target - self._last_target) >= self._significant_delta_rpm:
            delta = abs(target - (self._last_target or 0))
            ramp_time = (delta / max(1, self.motor.RAMP_STEP_RPM)) * self.motor.RAMP_INTERVAL_S
            self._target_changed_at = now
            self._settle_until = (now + self._stable_delay_base_s + ramp_time
                                  + self._settle_margin_s + self._pid_settle_margin_s)
            self._converged_count = 0
        self._last_target = target

        # HOI TU THEO SAI SO DO THUC: dem so mau lien tiep co |rpm - target| trong dai.
        # Dung phep do that, KHONG dung "lenh != target" (PID luon trim quanh target).
        if (self.motor.is_running and not self.motor.is_stopping
                and target > 0 and not self.motor.is_ramping
                and abs(actual_rpm - target) <= self._settle_band_rpm):
            self._converged_count += 1
        else:
            self._converged_count = 0
        is_converged = self._converged_count >= self._settle_consec_needed

        # NULL trong giai doan CHUA THE danh gia: vua start, dang ramp/doi target, dang dung,
        # HOAC con trong deadline ma CHUA hoi tu. Mot khi da hoi tu (rpm bam target) -> danh gia
        # ngay du con trong deadline. Het deadline ma chua hoi tu -> cung danh gia lai, nen
        # rpm lech lon keo dai van bi gan WARNING/DANGER/FAILURE, khong bi che giau.
        is_transient = (
            self.motor.is_running
            and (self.motor.is_ramping or self.motor.is_stopping
                 or (now < self._settle_until and not is_converged))
        )

        if self.motor.is_running and not self.motor.is_stopping:
            self._runtime_s += dt

        res = calculate_health({
            "temperature":  temp if temp is not None else AUTO_TEMP_LOW,
            "rpm":          actual_rpm,
            "target_rpm":   target if target > 0 else None,
            "mode":         self._mode,
            "motor":        self.motor.is_running and not self.motor.is_stopping,
            "runtime_hours": self._runtime_s / 3600.0,
        }, prev_max_degradation=self._max_degradation)

        rpm_error = res.get("rpm_error", None)
        health_status_calc = health_status_from_score(res["health_score"])

        # Mẫu hợp lệ để gắn nhãn sức khỏe khi motor đang chạy, không dừng,
        # đã qua giai đoạn chuyển tiếp. KHÔNG loại bỏ mẫu chỉ vì rpm lệch lớn,
        # vì lệch lớn sau khi đã ổn định chính là dấu hiệu DANGER/FAILURE.
        is_valid_health_sample = (
            self.motor.is_running
            and not self.motor.is_stopping
            and target > 0
            and not is_transient
        )

        if is_valid_health_sample:
            # operating status: tình trạng tức thời từ nhiệt độ/sai số tốc độ.
            status = res["status_rule"] if res.get("status_rule") else "NORMAL"
            # health status: tình trạng dài hạn suy ra từ HI, cùng cách Web thường hiển thị.
            health_status = health_status_calc
        else:
            # Trong giai đoạn chuyển tiếp không phát nhãn sức khỏe/trạng thái.
            status = None
            health_status = None
        # [v3 pattern] Hai duong song song: MQTT cho Unity, Supabase qua _db_queue
        self.mqtt.publish_sensor(
            rpm=actual_rpm, temperature=temp,
            target_rpm=target if target > 0 else None,
            motor_on=self.motor.is_running and not self.motor.is_stopping,
            mode=self._mode, status_rule=status, health_status=health_status,
            rpm_error=rpm_error, wave_rpm=self.motor.wave_rpm,
            is_stalled=self.motor.is_stalled,
        )
        self._db(self.supabase.log_sensor,
                 DEVICE_ID, actual_rpm, temp,
                 target if target > 0 else None, status, rpm_error,
                 self.motor.wave_rpm, self.motor.is_stalled)

        if is_valid_health_sample:
            self._max_degradation = res["max_degradation"]
            self._last_good_health_result = res

            # Mot topic duy nhat: cps/cyber/result -> Unity va Supabase cung doc
            self.mqtt.publish_cyber_result(
                DEVICE_ID, res,
                avg_temperature=temp, avg_rpm=actual_rpm,
                overheat_count=self._overheat_count,
                fan_activation_count=self._fan_activation_count,
                total_hours=self._runtime_s / 3600.0,
                today_runtime_sec=int(self._runtime_s),
                health_status=health_status,
            )
            self._db(self.supabase.log_cyber_results,
                     DEVICE_ID, res,
                     avg_temperature=temp, avg_rpm=actual_rpm,
                     overheat_count=self._overheat_count,
                     fan_activation_count=self._fan_activation_count,
                     total_hours=self._runtime_s / 3600.0,
                     today_runtime_sec=int(self._runtime_s))
            rel_err_pct = (100.0 * rpm_error / target) if rpm_error is not None and target > 0 else 0.0
            logger.info(
                "rpm=%.0f tgt=%d wave=%d err=%.1f%% temp=%s mode=%s | "
                "op_status=%s health_status=%s HI=%.1f RUL=%.0fh stall=%s",
                actual_rpm, target, self.motor.wave_rpm, rel_err_pct,
                ("%.1f" % temp) if temp is not None else "N/A",
                self._mode, status, health_status,
                res["health_score"], res["remaining_life_hours"],
                self.motor.is_stalled
            )
        else:
            logger.info("rpm=%.0f tgt=%d temp=%s mode=%s | status=NULL | bo qua HI/RUL vi dang start/ramp/doi target/dung",
                        actual_rpm, target,
                        ("%.1f" % temp) if temp is not None else "N/A",
                        self._mode)

    def _background_loop(self):
        last_heartbeat = 0.0
        last_sensor = 0.0
        last_temp_read = 0.0
        while self._running:
            now = time.monotonic()
            uptime = int(now - self._start_time)

            if now - last_heartbeat >= HEARTBEAT_INTERVAL_S:
                self.mqtt.publish_heartbeat(uptime_s=uptime, status="online")
                self._db(self.supabase.log_heartbeat, DEVICE_ID, uptime, "online")
                last_heartbeat = now

            # Poll lệnh từ web qua Supabase mỗi 2 giây.
            if now - self._last_web_poll >= self._web_poll_interval_s:
                threading.Thread(target=self._poll_web_commands,
                                 daemon=True, name="web_poll").start()
                self._last_web_poll = now

            if now - last_temp_read >= TEMP_READ_INTERVAL_S:
                threading.Thread(target=self.temp_sensor.update,
                                 daemon=True, name="temp_read").start()
                last_temp_read = now

            # Vòng PID đã gọi update_rpm() theo CONTROL_DT khi motor chạy.
            # Ở đây chỉ cập nhật khi motor KHÔNG chạy (để kim RPM hạ về 0), tránh
            # lấy mẫu trùng làm sai dt/EMA. Log vẫn đọc giá trị đã EMA mỗi 3s.
            if not self.motor.is_running:
                self.encoder.update_rpm()
            if now - last_sensor >= SENSOR_LOG_INTERVAL_S:
                actual_rpm = self.encoder.rpm
                temp = self.temp_sensor.temperature
                self._check_auto_fan(temp)          # tu dong quat theo nhiet
                self._compute_and_log(actual_rpm, temp)
                self._check_temp_warning(temp)
                last_sensor = now

            time.sleep(1)

    def start(self):
        logger.info("Motor Controller | device=%s", DEVICE_ID)
        if not self.mqtt.connect():
            logger.error("Khong ket noi duoc MQTT %s:%s", MQTT_HOST, MQTT_PORT)
            sys.exit(1)
        threading.Thread(target=self.temp_sensor.update, daemon=True).start()
        self.mqtt.publish_event("controller_start", "info")
        self._db(self.supabase.log_event, DEVICE_ID, "controller_start", "info")
        self._running = True
        threading.Thread(target=self._background_loop, daemon=True,
                         name="bg_loop").start()
        logger.info("San sang | sub: %s", TOPIC_CONTROL_CMD)

    def stop(self):
        logger.info("Dang dung...")
        self._running = False
        if self.motor.is_running:
            self.motor.emergency_stop()
        # Flush tất cả Supabase writes đang chờ
        self._db_queue.join()
        uptime = int(time.monotonic() - self._start_time)
        # Ghi offline đến cả 2 nơi trước khi thoát
        self.mqtt.publish_heartbeat(uptime_s=uptime, status="offline")
        self.supabase.log_heartbeat(DEVICE_ID, uptime, status="offline")
        self.mqtt.disconnect()
        self.encoder.cleanup()
        self.motor.cleanup()
        self.fan.cleanup()
        self.buttons.cleanup()
        if _pi is not None and ON_PI:
            _pi.stop()
        logger.info("Da dung an toan")

    def run_forever(self):
        self.start()

        def _handle(sig, frame):
            self.stop()
            sys.exit(0)
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        logger.info("Dang chay... Ctrl+C de dung")
        try:
            while True:
                time.sleep(1)
        except SystemExit:
            pass


if __name__ == "__main__":
    MotorController().run_forever()
