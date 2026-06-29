/**
 * state.js — Trạng thái toàn cục của ứng dụng CPS Dashboard
 *
 * Quy tắc sử dụng:
 *   • Đây là "single source of truth" — mọi module đọc/ghi trực tiếp vào AppState.
 *   • KHÔNG tạo bản sao cục bộ (const s = AppState) vì sẽ mất đồng bộ.
 *   • KHÔNG dùng Object.freeze — AppState cần được cập nhật liên tục.
 *
 * Luồng cập nhật:
 *   Supabase (raw_sensor)    → AppState.temperature / rpm
 *   Supabase (cyber_results) → AppState.healthScore / wearLevel / …
 *   Supabase (control_state) → AppState.motor / fan / mode   ← đây là giá trị "confirmed"
 *   controls.js (optimistic) → AppState.pendingRpm ← chờ Pi xác nhận
 */
'use strict';

const AppState = {

  // ═══════════════════════════════════════════════════════════════
  // CẢM BIẾN TRỰC TIẾP
  // Cập nhật mỗi ~2s từ bảng raw_sensor (qua Realtime hoặc polling).
  // null = chưa nhận được dữ liệu nào kể từ khi mở trang.
  // ═══════════════════════════════════════════════════════════════

  temperature: null,   // °C — nhiệt độ động cơ hiện tại
  rpm        : null,   // vòng/phút — tốc độ thực tế đọc từ encoder

  // ═══════════════════════════════════════════════════════════════
  // TRẠNG THÁI THIẾT BỊ — đã được Pi xác nhận
  // Cập nhật khi nhận control_state mới từ Supabase (onNewControlState).
  // Giá trị khởi tạo chỉ là placeholder, sẽ bị ghi đè ngay khi load trang.
  // ═══════════════════════════════════════════════════════════════

  motor: true,     // true = động cơ đang chạy | false = đã dừng
  fan  : false,    // true = quạt đang bật      | false = quạt tắt
  mode : 'auto',   // 'auto' = Pi tự điều khiển quạt theo nhiệt độ
                   // 'manual' = dashboard toàn quyền điều khiển

  // ═══════════════════════════════════════════════════════════════
  // GIÁ TRỊ PENDING — chờ Pi xác nhận
  // Được đặt bởi controls.js ngay khi người dùng nhấn nút (optimistic UI).
  // Chỉ dùng để hiển thị trong input/slider, KHÔNG dùng để so sánh
  // với trạng thái thực của thiết bị.
  // ═══════════════════════════════════════════════════════════════

  pendingRpm : 800,   // RPM đang đặt trong ô nhập liệu (chế độ manual)

  // ═══════════════════════════════════════════════════════════════
  // THỐNG KÊ VẬN HÀNH — từ bảng cyber_results
  // Được tính toán bởi CyberProcessor trong mqtt_supabase_bridge.py,
  // cập nhật mỗi ~10s.
  // ═══════════════════════════════════════════════════════════════

  totalHours    : 0,   // tổng giờ hoạt động tích lũy (tất cả các phiên)
  overheatCount : 0,   // số lần nhiệt độ vượt WARN_TEMP
  fanActivations: 0,   // số lần quạt được kích hoạt (AUTO hoặc manual)
  avgTemp       : 0,   // °C — nhiệt độ trung bình toàn phiên
  avgRpm        : 0,   // RPM trung bình toàn phiên
  todayRuntimeSec: 0,  // giây hoạt động trong ngày hôm nay (reset lúc 00:00)

  // ═══════════════════════════════════════════════════════════════
  // HEALTH METRICS — từ bảng cyber_results
  // Mô hình dự đoán độ bền, tính bởi CyberProcessor dựa trên
  // lịch sử nhiệt độ, RPM, số lần quá nhiệt, tổng giờ vận hành.
  // ═══════════════════════════════════════════════════════════════

  healthScore       : 100,    // điểm sức khỏe tổng thể (0–100)
  wearLevel         : 0,      // % mức mài mòn ước tính (0 = mới, 100 = hết tuổi thọ)
  failureProbability: 0,      // % xác suất hỏng hóc trong thời gian tới (0–100)
  remainingLifeHours: 20000,  // giờ tuổi thọ ước tính còn lại

  // ═══════════════════════════════════════════════════════════════
  // KẾT NỐI — trạng thái online/offline
  // ═══════════════════════════════════════════════════════════════

  deviceOnline     : false,  // true khi heartbeat còn tươi (< HEARTBEAT_TIMEOUT_MS)
  lastHeartbeatTime: null,   // Date object — thời điểm nhận heartbeat gần nhất

  /**
   * Handle của setTimeout watchdog — lưu để clearTimeout khi reset.
   * Tiền tố _ biểu thị "private": chỉ supabase.js và main.js được đụng vào.
   */
  _heartbeatTimer: null,

  // ═══════════════════════════════════════════════════════════════
  // CACHE SỰ KIỆN — bản sao trong bộ nhớ của bảng Events
  // Được đồng bộ từ Supabase khi load trang và cập nhật realtime.
  // Giới hạn EVENT_LOG_LIMIT phần tử (phần tử mới nhất ở đầu mảng).
  // ═══════════════════════════════════════════════════════════════

  events: [],   // mảng object { event_name, severity, created_at }
};