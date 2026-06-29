/**
 * ui-init.js — Script khởi tạo UI nhỏ, chạy sau khi DOM và app đã sẵn sàng
 *
 * Chứa các hàm gắn liền với HTML cụ thể (không thuộc về ui.js hay controls.js):
 *   1. updateClock()           — đồng hồ góc phải header
 *   2. setSpeedPreset(value)   — nút chọn nhanh RPM (chỉ gửi lệnh trong MANUAL)
 *   3. isAutoMode()            — kiểm tra chế độ hiện tại qua CSS class
 *   4. setManualControlsEnabled() — khóa/mở khóa các ô điều khiển chỉ dành cho manual
 *   5. syncModeUI(mode)        — đồng bộ CSS class body + trạng thái controls khi đổi chế độ
 *   6. Wrap window.setMode     — gọi syncModeUI() sau mỗi lần setMode() từ controls.js
 *
 * Phụ thuộc: controls.js phải load trước (để window.setMode, applySpeed, onSpeedInput tồn tại)
 */
'use strict';

// ═══════════════════════════════════════════════════════════════
// 1. ĐỒNG HỒ THỜI GIAN THỰC
// ═══════════════════════════════════════════════════════════════

/** Cập nhật nội dung #clock thành giờ hiện tại, gọi mỗi 1 giây */
function updateClock() {
  const clock = document.getElementById('clock');
  if (!clock) return;
  clock.textContent = new Date().toLocaleTimeString('vi-VN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
}

// Hiển thị ngay + lặp mỗi 1s
updateClock();
setInterval(updateClock, 1000);

// ═══════════════════════════════════════════════════════════════
// 2. PRESET TỐC ĐỘ RPM
// ═══════════════════════════════════════════════════════════════

/**
 * Gọi khi nhấn nút preset RPM.
 * Điền giá trị vào input + slider rồi gửi lệnh ngay khi đang ở MANUAL.
 *
 * @param {number} value — giá trị RPM preset
 */
function setSpeedPreset(value) {
  if (AppState.mode !== 'manual') {
    console.warn('[UI] Preset RPM bị bỏ qua — cần chế độ MANUAL');
    return;
  }

  const speedInput  = document.getElementById('speedInput');
  const speedSlider = document.getElementById('speedSlider');
  if (speedInput)  speedInput.value  = value;
  if (speedSlider) speedSlider.value = value;

  onSpeedInput(value);  // đồng bộ AppState.pendingRpm
  applySpeed();         // gửi lệnh luôn
}

// ═══════════════════════════════════════════════════════════════
// 3 & 4. QUẢN LÝ TRẠNG THÁI CHẾ ĐỘ
// ═══════════════════════════════════════════════════════════════

/**
 * Kiểm tra body có đang ở chế độ AUTO không.
 * Dùng CSS class thay vì đọc AppState để tránh import circular.
 */
function isAutoMode() {
  return document.body.classList.contains('mode-auto');
}

/**
 * Bật / tắt toàn bộ controls trong nhóm .manual-only.
 * Được gọi mỗi khi chế độ thay đổi để ngăn người dùng
 * điều khiển các nhóm chỉ dành cho MANUAL khi đang ở AUTO.
 *
 * @param {boolean} enabled — true = mở khóa, false = khóa
 */
function setManualControlsEnabled(enabled) {
  document.querySelectorAll('.manual-only').forEach(block => {
    block.classList.toggle('is-disabled', !enabled);
    block.querySelectorAll('button, input').forEach(el => {
      el.disabled = !enabled;
    });
  });
}

// ═══════════════════════════════════════════════════════════════
// 5. SYNC MODE UI
// ═══════════════════════════════════════════════════════════════

/**
 * Đồng bộ giao diện theo chế độ AUTO / MANUAL:
 *   - Gán class mode-auto / mode-manual lên <body> (dùng bởi CSS)
 *   - Khóa hoặc mở khóa các ô điều khiển manual-only
 *
 * Được gọi bởi:
 *   - Wrap window.setMode (khi người dùng nhấn nút chế độ)
 *   - onNewControlState() trong supabase.js (khi Pi xác nhận)
 *   - loadInitialData() trong supabase.js (khi tải trang)
 *
 * @param {string} mode — 'auto' | 'manual'
 */
function syncModeUI(mode) {
  const isManual = mode === 'manual';
  document.body.classList.toggle('mode-auto',   !isManual);
  document.body.classList.toggle('mode-manual',  isManual);
  setManualControlsEnabled(isManual);
}

// ═══════════════════════════════════════════════════════════════
// 6. WRAP window.setMode
// ═══════════════════════════════════════════════════════════════

/**
 * Bọc hàm setMode() từ controls.js để gọi thêm syncModeUI() sau khi gửi lệnh.
 * Cách này giúp controls.js không cần biết về DOM/CSS class,
 * giữ đúng nguyên tắc: controls.js chỉ lo gửi lệnh, ui-init.js lo UI.
 */
const _origSetMode = window.setMode;
window.setMode = function (mode) {
  if (typeof _origSetMode === 'function') _origSetMode(mode);
  syncModeUI(mode);
};

// Khởi tạo mặc định AUTO khi trang vừa load
syncModeUI('auto');