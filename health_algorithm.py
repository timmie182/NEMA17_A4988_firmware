"""
health_algorithm.py  (BẢN VÁ — sửa triệt để lỗi HI bị kẹt thấp)
================================================================
Tính Health Index (HI/health_score), Wear Level, RUL,
Failure Probability (Weibull) và status_rule cho động cơ NEMA17.

NGUYÊN NHÂN LỖI (đã phân tích từ data thật):
  Bản cũ nạp degradation TỨC THỜI (gồm cả sai số RPM) vào một biến ratchet
  max() đơn điệu. Chỉ một mẫu lệch tốc nhất thời lúc đổi target cũng ép HI
  xuống và KHÔNG BAO GIỜ hồi lại, kể cả khi động cơ chạy đúng target trở lại.

CÁCH SỬA (giữ đúng tinh thần đồ án gốc - Wang/Lei/Naderian):
  * Tách degradation làm 2 phần:
      - HAO MÒN THẬT (permanent_wear) = nhiệt + runtime  -> ratchet một chiều,
        đây mới là thứ làm RUL giảm và không tăng lại (đúng yêu cầu gốc).
      - SAI SỐ RPM = chỉ là PHẠT TỨC THỜI lên HI, KHÔNG nạp vào ratchet,
        nên khi rpm bám lại target thì HI tự hồi phục.
  * HI hiển thị = 1 - (permanent_wear_ratcheted + rpm_penalty_tức_thời).
  * RUL / wear_level chỉ dựa trên permanent_wear -> vẫn đơn điệu, không tăng.
  * Khi motor TẮT hoặc target<=0: KHÔNG bịa target (bỏ default 800), rpm_error=None.

  CHỮ KÝ HÀM & TÊN KHÓA TRẢ VỀ GIỮ NGUYÊN (prev_max_degradation, "max_degradation")
  để firmware gọi y như cũ, không phải sửa chỗ gọi calculate_health.
"""

import math

# ============================================================
#  THAM SỐ VẬT LÝ  — *** HIỆU CHỈNH theo motor + datasheet của bạn ***
# ============================================================
BASE_LIFE_HOURS = 20000.0   # Tuổi thọ thiết kế (Rbase), giờ
TEMP_NORMAL     = 30.0      # Nhiệt độ nền (~phòng); raw_sensor của bạn ~29-30°C
TEMP_MAX        = 80.0      # Nhiệt độ tới hạn bề mặt (tham chiếu datasheet NEMA17)
REL_ERR_MAX     = 0.50      # Sai số RPM tương đối coi như hỏng hoàn toàn (50%)

# Trọng số degradation (TỔNG = 1) — theo đề tài: temp 0.3 / rpm 0.3 / runtime 0.4
W_TEMP    = 0.3
W_RPM     = 0.3
W_RUNTIME = 0.4

# RUL phi tuyến:  RUL = Rbase * HI^k   (k>1 -> RUL giảm nhanh khi HI thấp)
EXPONENT_K = 1.5

# Weibull (theo file hướng dẫn):  P = 1 - exp(-(t_eq/eta)^beta)
WEIBULL_BETA = 2.0
WEIBULL_ETA  = 20000.0

# ============================================================
#  CHẾ ĐỘ AUTO — target_rpm nội suy theo nhiệt độ
# ============================================================
AUTO_TEMP_LOW  = 30.0
AUTO_TEMP_HIGH = 55.0
AUTO_RPM_MAX   = 250
AUTO_RPM_MIN   = 60
AUTO_MODE_DIRECTION = "derate"   # "derate": nóng -> chậm | "rampup": nóng -> nhanh

# Khi thiếu target ở manual, dùng mức này. ĐÃ HẠ từ 800 -> 300 cho khớp MAX_RPM mới.
# (Thực ra khi motor TẮT ta trả rpm_error=None nên giá trị này hầu như không dùng.)
DEFAULT_TARGET_RPM = 300

# --- Cách tính status_rule: "raw" (tín hiệu thô) hoặc "hi" (theo Health Index) ---
STATUS_RULE_METHOD = "raw"
TEMP_BANDS = [(72.0, "FAILURE"), (58.0, "DANGER"), (45.0, "WARNING")]
ERR_BANDS  = [(0.20, "FAILURE"), (0.08, "DANGER"), (0.02, "WARNING")]
HI_BANDS   = [(80.0, "NORMAL"), (60.0, "WARNING"), (40.0, "DANGER")]
LEVELS     = ["NORMAL", "WARNING", "DANGER", "FAILURE"]


# ============================================================
#  HÀM TIỆN ÍCH
# ============================================================
def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(v, hi))


def _read(sensor_data, *keys, default=0.0):
    for k in keys:
        if k in sensor_data and sensor_data[k] is not None:
            try:
                return float(sensor_data[k])
            except (TypeError, ValueError):
                continue
    return float(default)


def _read_opt(sensor_data, *keys):
    for k in keys:
        if k in sensor_data and sensor_data[k] is not None:
            try:
                return float(sensor_data[k])
            except (TypeError, ValueError):
                continue
    return None


def _severity(value, bands):
    for i, (thr, _name) in enumerate(bands):
        if value >= thr:
            return 3 - i
    return 0


# ============================================================
#  AUTO MODE: nội suy target_rpm theo nhiệt độ  (GIỮ NGUYÊN)
# ============================================================
def auto_target_rpm(temperature,
                    temp_low=AUTO_TEMP_LOW, temp_high=AUTO_TEMP_HIGH,
                    rpm_min=AUTO_RPM_MIN, rpm_max=AUTO_RPM_MAX,
                    direction=AUTO_MODE_DIRECTION):
    if temp_high <= temp_low:
        return rpm_max
    t = clamp((temperature - temp_low) / (temp_high - temp_low))
    if direction == "rampup":
        rpm = rpm_min + t * (rpm_max - rpm_min)
    else:
        rpm = rpm_max - t * (rpm_max - rpm_min)
    return round(rpm, 1)


def resolve_target_rpm(sensor_data, default_target_rpm=DEFAULT_TARGET_RPM):
    mode = str(sensor_data.get("mode", "manual")).lower()
    explicit = _read_opt(sensor_data, "target_rpm", "tar_r")
    if explicit is not None and explicit > 0:
        return explicit, "logged"
    if mode == "auto":
        temperature = _read(sensor_data, "temperature", "temp")
        return auto_target_rpm(temperature), "auto_fallback"
    return float(default_target_rpm), "default"


# ============================================================
#  status_rule  — BASELINE THRESHOLD (KHÔNG phải nhãn train RF)  (GIỮ NGUYÊN)
# ============================================================
def status_rule_from_raw(temperature, rpm, target_rpm, motor_on=True):
    if not motor_on or target_rpm is None or target_rpm <= 0:
        return None
    rel_err = abs(rpm - target_rpm) / target_rpm
    sev = max(_severity(temperature, TEMP_BANDS), _severity(rel_err, ERR_BANDS))
    return LEVELS[sev]


def status_rule_from_hi(health_score, motor_on=True):
    if not motor_on:
        return None
    for thr, name in HI_BANDS:
        if health_score >= thr:
            return name
    return "FAILURE"


def compute_status_rule(temperature, rpm, target_rpm, health_score,
                        motor_on=True, method=None):
    method = method or STATUS_RULE_METHOD
    if method == "hi":
        return status_rule_from_hi(health_score, motor_on)
    return status_rule_from_raw(temperature, rpm, target_rpm, motor_on)


# ============================================================
#  SEED: tái dựng hao mòn thật từ runtime (dùng khi khởi động)
#  -> Tránh nạp lại wear_level CŨ đã bị spike làm phồng từ DB.
# ============================================================
def initial_max_wear_from_runtime(runtime_hours, temperature=TEMP_NORMAL):
    """Floor hao mòn lúc seed: CHỈ từ runtime (+ nhiệt nền nếu biết).
    Runtime là đại lượng tích lũy trung thực, không chứa spike RPM."""
    runtime_damage = clamp(runtime_hours / BASE_LIFE_HOURS)
    temp_damage    = clamp((temperature - TEMP_NORMAL) / (TEMP_MAX - TEMP_NORMAL))
    return clamp(W_RUNTIME * runtime_damage + W_TEMP * temp_damage)


# ============================================================
#  HÀM CHÍNH:  HI / Wear / RUL / Failure Probability
# ============================================================
def calculate_health(sensor_data, prev_max_degradation=0.0,
                     default_target_rpm=DEFAULT_TARGET_RPM):
    """
    prev_max_degradation : floor HAO MÒN THẬT (permanent wear) của bản ghi trước.
                           Tên giữ nguyên để tương thích firmware; nội dung giờ
                           CHỈ là phần ratchet runtime/nhiệt (KHÔNG còn rpm).
    """
    temperature = _read(sensor_data, "temperature", "temp")
    rpm         = _read(sensor_data, "rpm", "current_rpm", "act_r")
    runtime_h   = _read(sensor_data, "runtime_hours", "total_hours", "runtime")
    motor_on    = bool(sensor_data.get("motor", sensor_data.get("motor_state", True)))

    # ---- target_rpm ----
    target_rpm, target_source = resolve_target_rpm(sensor_data, default_target_rpm)

    # ---- Sai số RPM: CHỈ tính khi motor đang chạy & target hợp lệ ----
    if motor_on and target_rpm and target_rpm > 0:
        rpm_error = abs(target_rpm - rpm)
        rel_err   = rpm_error / target_rpm
    else:
        rpm_error = None          # motor tắt -> KHÔNG bịa target, KHÔNG có sai số
        rel_err   = 0.0

    # ---- 3 thành phần hư hại tức thời, mỗi cái trong [0, 1] ----
    temp_damage    = clamp((temperature - TEMP_NORMAL) / (TEMP_MAX - TEMP_NORMAL))
    rpm_damage     = clamp(rel_err / REL_ERR_MAX)
    runtime_damage = clamp(runtime_h / BASE_LIFE_HOURS)

    # ====================================================================
    #  TÁCH RATCHET  ── đây là phần sửa lỗi cốt lõi
    # ====================================================================
    # (1) HAO MÒN THẬT: nhiệt + runtime -> ratchet một chiều (không giảm).
    #     Đây là cái duy nhất làm RUL/wear thay đổi và không bao giờ tăng lại.
    permanent_wear = clamp(W_TEMP * temp_damage + W_RUNTIME * runtime_damage)
    max_wear       = max(permanent_wear, prev_max_degradation)   # <-- chỉ runtime/nhiệt

    # (2) SAI SỐ RPM: chỉ là PHẠT TỨC THỜI, hồi phục được, KHÔNG nạp ratchet.
    #     Chỉ phạt khi motor đang chạy (lúc tắt rpm_error=None -> phạt 0).
    rpm_penalty = (W_RPM * rpm_damage) if rpm_error is not None else 0.0

    # ---- HI hiển thị: hao mòn thật + phạt rpm tức thời (có thể hồi) ----
    degradation_now = clamp(max_wear + rpm_penalty)
    health_score    = (1.0 - degradation_now) * 100.0

    # ---- Wear & RUL: CHỈ theo hao mòn thật -> đơn điệu, không tăng ----
    hi_ratio_perm        = 1.0 - max_wear
    wear_level           = max_wear * 100.0
    remaining_life_hours = BASE_LIFE_HOURS * (hi_ratio_perm ** EXPONENT_K)

    # ---- Failure probability (Weibull) theo hao mòn THẬT ----
    t_eq = runtime_h * (1.0 + max_wear)
    failure_probability = 1.0 - math.exp(-((t_eq / WEIBULL_ETA) ** WEIBULL_BETA))

    # ---- status_rule (baseline, KHÔNG phải nhãn train) ----
    rpm_for_rule = rpm if motor_on else 0.0
    status_rule = compute_status_rule(temperature, rpm_for_rule,
                                      target_rpm if motor_on else None,
                                      health_score, motor_on)

    return {
        # ---> cyber_results
        "health_score":         round(health_score, 2),
        "wear_level":           round(wear_level, 2),
        "failure_probability":  round(failure_probability, 4),
        "remaining_life_hours": round(remaining_life_hours, 2),
        # ---> raw_sensor (per-reading)
        "rpm_error":            (round(rpm_error, 2) if rpm_error is not None else None),
        "status_rule":          status_rule,
        "target_rpm":           round(target_rpm, 1),
        "target_source":        target_source,
        # ---> trạng thái nội bộ: GIỮ TÊN "max_degradation" cho firmware,
        #      nhưng nội dung nay là HAO MÒN THẬT (runtime/nhiệt) đã ratchet.
        "max_degradation":      round(max_wear, 6),
        # thông tin phụ (tiện debug): phần phạt rpm tức thời, không ratchet
        "rpm_penalty":          round(rpm_penalty, 6),
    }


def calculate_health_series(rows, default_target_rpm=DEFAULT_TARGET_RPM):
    results = []
    prev_max = 0.0
    for r in rows:
        out = calculate_health(r, prev_max_degradation=prev_max,
                               default_target_rpm=default_target_rpm)
        prev_max = out["max_degradation"]
        results.append(out)
    return results


# ============================================================
#  TỰ KIỂM TRA NHANH
# ============================================================
if __name__ == "__main__":
    print("== auto_target_rpm (derate) ==")
    for tC in (30, 35, 50, 65, 75):
        print(f"  {tC}°C -> {auto_target_rpm(tC)} RPM")

    print("\n== Mô phỏng cú spike rpm lúc đổi target rồi bám lại ==")
    seq = [
        {"temperature": 30, "rpm": 300, "target_rpm": 300, "runtime_hours": 0.01, "motor": True},
        {"temperature": 30, "rpm": 202, "target_rpm": 300, "runtime_hours": 0.02, "motor": True},  # spike
        {"temperature": 30, "rpm": 300, "target_rpm": 300, "runtime_hours": 0.03, "motor": True},  # bám lại
        {"temperature": 30, "rpm": 300, "target_rpm": 300, "runtime_hours": 0.04, "motor": True},
    ]
    for r, out in zip(seq, calculate_health_series(seq)):
        print(f"  rpm={r['rpm']:>3} tgt={r['target_rpm']} -> HI={out['health_score']:>6} "
              f"wear={out['wear_level']:.2f} RUL={out['remaining_life_hours']:.0f}")
