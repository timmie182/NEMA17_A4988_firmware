/**
 * config.js — Hằng số cấu hình toàn hệ thống CPS
 *
 * ⚠️  Quan trọng: Các giá trị ngưỡng vật lý (FAN_ON_TEMP, WARN_TEMP…)
 *     và tên bảng (TABLES) PHẢI khớp với:
 *       - mqtt_supabase_bridge.py  (CyberProcessor, TABLE_COLUMNS)
 *       - pi_publisher.py          (ngưỡng điều khiển quạt)
 *
 * Toàn bộ object được đóng băng (Object.freeze) để tránh vô tình
 * ghi đè hằng số từ bất kỳ module nào.
 */
'use strict';

const CONFIG = Object.freeze({

  // ═══════════════════════════════════════════════════════════════
  // SUPABASE — thông tin kết nối
  // ═══════════════════════════════════════════════════════════════

  /** URL project Supabase — lấy từ Settings > API trong Supabase dashboard */
  SUPABASE_URL: 'https://mhloddjfpujjynkjsnlf.supabase.co',

  /**
   * Anon key — public key, an toàn để để trong client-side JS.
   * Key này chỉ cho phép truy cập theo RLS policy, không phải service key.
   * Chia nhỏ thành nhiều dòng cho dễ đọc trong git diff.
   */
  SUPABASE_ANON_KEY:
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
    + '.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1obG9kZGpmcHVqanlua2pzbmxmIiwicm9sZSI6ImFub24'
    + 'iLCJpYXQiOjE3Nzk1MDE4MjIsImV4cCI6MjA5NTA3NzgyMn0'
    + '.zkXVXJadWWbC5yrJAm-OqX55Zyxn26y1VKXQrZNJMTc',

  // ═══════════════════════════════════════════════════════════════
  // THIẾT BỊ
  // ═══════════════════════════════════════════════════════════════

  /**
   * ID thiết bị — dùng để lọc dữ liệu trong tất cả các bảng Supabase.
   * Phải khớp với DEVICE_ID trong pi_publisher.py và mqtt_supabase_bridge.py.
   */
  DEVICE_ID: 'stepper_01',

  // ═══════════════════════════════════════════════════════════════
  // TÊN BẢNG SUPABASE
  // Dùng CONFIG.TABLES.XXX thay vì hardcode chuỗi để tránh lỗi typo
  // và dễ thay đổi tên bảng sau này ở một chỗ duy nhất.
  // ═══════════════════════════════════════════════════════════════

  TABLES: Object.freeze({
    RAW_SENSOR      : 'raw_sensor',       // dữ liệu cảm biến thô (nhiệt độ, RPM)
    CYBER_RESULTS   : 'cyber_results',    // kết quả phân tích CyberProcessor (health, wear…)
    CONTROL_COMMANDS: 'control_commands', // lệnh điều khiển từ dashboard → Pi
    CONTROL_STATE   : 'control_state',    // trạng thái đã xác nhận từ Pi → dashboard
    EVENTS          : 'events',           // nhật ký sự kiện (cảnh báo, lỗi, thông tin)
    HEARTBEAT       : 'heartbeat',        // tín hiệu Pi còn sống, gửi mỗi ~12s
  }),

  // ═══════════════════════════════════════════════════════════════
  // NGƯỠNG VẬT LÝ — điều khiển quạt & cảnh báo
  // Hysteresis: bật quạt ở 55°C, tắt ở 50°C (tránh bật/tắt liên tục)
  // ═══════════════════════════════════════════════════════════════

  FAN_ON_TEMP  : 40,   // °C — bật quạt tự động (chế độ AUTO)
  FAN_OFF_TEMP : 35,   // °C — tắt quạt tự động (chế độ AUTO), thấp hơn FAN_ON để có hysteresis
  WARN_TEMP    : 75,   // °C — hiển thị cảnh báo quá nhiệt trên dashboard
  CRITICAL_TEMP: 85,   // °C — ngưỡng nghiêm trọng, bridge có thể dừng động cơ khẩn cấp

  // ═══════════════════════════════════════════════════════════════
  // GIỚI HẠN ĐIỀU KHIỂN — validate input trước khi gửi lệnh
  // ═══════════════════════════════════════════════════════════════

  RPM_MIN  : 1,    // RPM tối thiểu — dưới mức này động cơ không ổn định
  RPM_MAX  : 600,    // RPM tối đa — giới hạn cơ học của stepper motor

  // ═══════════════════════════════════════════════════════════════
  // BIỂU ĐỒ & LỊCH SỬ — giới hạn hiển thị
  // ═══════════════════════════════════════════════════════════════

  CHART_MAX_POINTS    : 40,  // số điểm tối đa trên chart (sliding window — điểm cũ tự bị xóa)
  SENSOR_HISTORY_LIMIT: 40,  // số bản ghi sensor/cyber lấy từ Supabase lúc khởi động
  EVENT_LOG_LIMIT     : 50,  // số hàng tối đa trong bảng Events

  // ═══════════════════════════════════════════════════════════════
  // KẾT NỐI — heartbeat watchdog
  // ═══════════════════════════════════════════════════════════════

  /**
   * Thời gian tối đa (ms) không nhận heartbeat trước khi đánh dấu offline.
   * Pi gửi heartbeat mỗi 12s → 45s = ~3.75 lần bỏ lỡ mới báo offline,
   * đủ rộng để tránh false alarm do mạng chậm nhất thời.
   */
  HEARTBEAT_TIMEOUT_MS: 45_000,
});