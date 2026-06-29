/**
 * controls.js — Xử lý điều khiển và gửi lệnh lên Supabase
 *
 * Luồng gửi lệnh:
 *   1. Hàm điều khiển (setMotor / setFan / applySpeed…) được gọi từ onclick HTML
 *   2. Gọi sendCommand(fields) — kiểm tra online, bật pending UI, ghi Supabase
 *   3. _applyOptimisticState(fields) — lưu giá trị pending vào AppState ngay
 *      (không chờ Pi xác nhận) để UI phản hồi tức thì
 *   4. Pi nhận lệnh → ghi control_state → supabase.js nhận và gọi onNewControlState
 *      → UI_setControlPending(false) để tắt hiệu ứng chờ
 *
 * Cấu trúc:
 *   1. sendCommand()        — core: ghi lệnh vào Supabase
 *   2. _applyOptimisticState() — cập nhật AppState pending trước khi Pi xác nhận
 *   3. _clamp()             — helper giới hạn giá trị số trong khoảng [min, max]
 *   4. Lệnh điều khiển      — setMotor, setMode, setFan, applySpeed
 *   5. Input callbacks      — onSpeedInput, onSpeedSlider
 */
'use strict';

// ═══════════════════════════════════════════════════════════════
// 1. CORE: GỬI LỆNH LÊN SUPABASE
// ═══════════════════════════════════════════════════════════════

/**
 * Ghi một lệnh điều khiển vào bảng control_commands.
 * Pi sẽ đọc lệnh này qua MQTT bridge và thực thi.
 *
 * @param {Object} fields — các trường lệnh cần ghi (vd. { motor: true }, { rpm: 800 })
 */
async function sendCommand(fields) {
  if (!window._sbClient) {
    console.warn('[Controls] Supabase client chưa sẵn sàng');
    return;
  }
  if (!AppState.deviceOnline) {
    alert('Thiết bị đang offline, không thể gửi lệnh điều khiển');
    return;
  }

  // Bật hiệu ứng loading trên panel điều khiển (tắt lại khi Pi xác nhận)
  UI_setControlPending(true);

  // Cập nhật AppState ngay (optimistic) — không chờ Pi xác nhận
  _applyOptimisticState(fields);

  const payload = {
    device_id : CONFIG.DEVICE_ID,
    source    : 'web',
    created_at: new Date().toISOString(),
    ...fields,
  };

  try {
    const { error } = await window._sbClient
      .from(CONFIG.TABLES.CONTROL_COMMANDS)
      .insert(payload);

    if (error) {
      console.error('[Controls] ✗ Lỗi gửi lệnh:', error.message);
      UI_setControlPending(false);   // tắt pending vì lệnh thất bại
    } else {
      console.log('[Controls] ✓ Lệnh đã gửi:', fields);
      // Không tắt pending ở đây — supabase.js sẽ tắt khi nhận control_state từ Pi
    }
  } catch (err) {
    console.error('[Controls] Exception:', err);
    UI_setControlPending(false);
  }
}

// ═══════════════════════════════════════════════════════════════
// 2. OPTIMISTIC STATE
// ═══════════════════════════════════════════════════════════════

/**
 * Lưu giá trị "đang chờ xác nhận" vào AppState ngay khi gửi lệnh.
 * Chỉ áp dụng cho rpm — motor/fan/mode sẽ được Pi xác nhận
 * qua control_state và cập nhật bởi onNewControlState() trong supabase.js.
 *
 * @param {Object} fields — object lệnh vừa gửi đi
 */
function _applyOptimisticState(fields) {
  if ('target_rpm' in fields) AppState.pendingRpm = fields.target_rpm;
}

// ═══════════════════════════════════════════════════════════════
// 3. HELPER
// ═══════════════════════════════════════════════════════════════

/**
 * Giới hạn giá trị trong khoảng [min, max].
 * Dùng để validate input số trước khi gửi lệnh.
 *
 * @param {number} value — giá trị cần clamp
 * @param {number} min   — giới hạn dưới
 * @param {number} max   — giới hạn trên
 * @param {number} fallback — giá trị fallback nếu parse thất bại
 */
const _clamp = (value, min, max, fallback) =>
  Math.max(min, Math.min(max, parseInt(value, 10) || fallback));

// ═══════════════════════════════════════════════════════════════
// 4. LỆNH ĐIỀU KHIỂN — gọi từ onclick trong HTML
// ═══════════════════════════════════════════════════════════════

/**
 * Bật / tắt động cơ.
 * Bỏ qua nếu trạng thái không thay đổi (tránh gửi lệnh trùng).
 *
 * @param {any} value — truthy = bật, falsy = tắt
 */
function setMotor(value) {
  const motorOn = Boolean(value);
  if (AppState.motor === motorOn) return;
  sendCommand({ motor: motorOn });
}

/**
 * Chuyển chế độ AUTO / MANUAL.
 * Chỉ chấp nhận 'manual', mọi giá trị khác đều về 'auto'.
 *
 * @param {string} mode — 'auto' | 'manual'
 */
function setMode(mode) {
  const newMode = mode === 'manual' ? 'manual' : 'auto';
  if (AppState.mode === newMode) return;
  sendCommand({ mode: newMode });
}

/**
 * Bật / tắt quạt — chỉ hoạt động trong chế độ MANUAL.
 * Trong AUTO, quạt được điều khiển tự động theo nhiệt độ.
 *
 * @param {any} value — truthy = bật, falsy = tắt
 */
function setFan(value) {
  if (AppState.mode !== 'manual') {
    console.warn('[Controls] Lệnh quạt bị bỏ qua — cần chế độ MANUAL');
    return;
  }
  const fanOn = Boolean(value);
  if (AppState.fan === fanOn) return;
  sendCommand({ fan: fanOn });
}

/**
 * Áp dụng tốc độ RPM từ ô nhập liệu.
 * Chỉ cho phép gửi target_rpm trong chế độ MANUAL.
 * Clamp giá trị trong [RPM_MIN, RPM_MAX] trước khi gửi.
 */
function applySpeed() {
  if (AppState.mode !== 'manual') {
    console.warn('[Controls] Lệnh RPM bị bỏ qua — cần chế độ MANUAL');
    return;
  }
  const inp = document.getElementById('speedInput');
  if (!inp) return;
  const rpm = _clamp(inp.value, CONFIG.RPM_MIN, CONFIG.RPM_MAX, CONFIG.RPM_MIN);
  inp.value           = rpm;          // cập nhật lại input về giá trị đã clamp
  AppState.pendingRpm = rpm;
  sendCommand({ target_rpm: rpm });
}


// ═══════════════════════════════════════════════════════════════
// 5. INPUT CALLBACKS — đồng bộ input ↔ slider khi người dùng gõ/kéo
// ═══════════════════════════════════════════════════════════════

/**
 * Gọi khi người dùng gõ vào ô RPM — đồng bộ sang thanh trượt.
 * Chỉ cập nhật AppState.pendingRpm, KHÔNG gửi lệnh (chờ nhấn nút Áp dụng).
 *
 * @param {string} value — giá trị chuỗi từ input.value
 */
function onSpeedInput(value) {
  const sld = document.getElementById('speedSlider');
  if (sld) sld.value  = value;
  AppState.pendingRpm = parseInt(value, 10) || AppState.pendingRpm;
}

/**
 * Gọi khi người dùng kéo thanh trượt RPM — đồng bộ sang ô nhập.
 * Chỉ cập nhật AppState.pendingRpm, KHÔNG gửi lệnh.
 *
 * @param {string} value — giá trị chuỗi từ slider.value
 */
function onSpeedSlider(value) {
  const inp = document.getElementById('speedInput');
  if (inp) inp.value  = value;
  AppState.pendingRpm = parseInt(value, 10) || AppState.pendingRpm;
}

