/**
 * main.js — Điểm khởi động ứng dụng CPS Dashboard
 *
 * Thứ tự khởi tạo:
 *   1. initSupabase()        — tạo Supabase client (window._sbClient)
 *   2. initCharts()          — tạo 3 Chart.js instances
 *   3. loadInitialData()     — fetch lịch sử từ Supabase, nạp vào UI + chart
 *   4. subscribeAll()        — đăng ký Realtime WebSocket channels
 *   5. startPolling()        — bắt đầu polling control_state, heartbeat, sensor…
 *   6. startHeartbeatWatch() — đặt timeout watchdog ban đầu
 *
 * Phụ thuộc — phải load theo thứ tự này trong index.html:
 *   config.js → state.js → ui.js → charts.js → controls.js → supabase.js → main.js
 */
'use strict';

// ═══════════════════════════════════════════════════════════════
// 1. KIỂM TRA DEPENDENCIES
// ═══════════════════════════════════════════════════════════════

/**
 * Kiểm tra tất cả thư viện và module cần thiết đã được load chưa.
 * Nếu thiếu bất kỳ thứ gì → log lỗi và dừng khởi động.
 *
 * @returns {boolean} true nếu đủ dependencies, false nếu thiếu
 */
function _checkDependencies() {
  const missing = [
    typeof supabase === 'undefined' && 'supabase-js CDN',
    typeof Chart    === 'undefined' && 'Chart.js CDN',
    typeof CONFIG   === 'undefined' && 'config.js',
    typeof AppState === 'undefined' && 'state.js',
  ].filter(Boolean);   // lọc bỏ các giá trị false

  if (missing.length > 0) {
    console.error('[Main] ✗ Thiếu dependencies:', missing.join(', '));
    return false;
  }
  return true;
}

// ═══════════════════════════════════════════════════════════════
// 2. HEARTBEAT WATCHDOG
// ═══════════════════════════════════════════════════════════════

/**
 * Đặt timer watchdog ban đầu ngay khi trang load.
 * Nếu không nhận được heartbeat nào trong HEARTBEAT_TIMEOUT_MS
 * kể từ lúc mở trang → hiển thị offline.
 *
 * Sau đó, mỗi lần nhận heartbeat, onNewHeartbeat() trong supabase.js
 * sẽ tự reset timer này — main.js không cần lo thêm.
 */
function startHeartbeatWatch() {
  // Hiển thị "đang kết nối" ngay lập tức khi trang vừa load
  if (!AppState.deviceOnline) {
    UI_setOnlineStatus(false, 'Đang kết nối...');
  }

  // Xóa timer cũ nếu có (tránh duplicate)
  clearTimeout(AppState._heartbeatTimer);

  // Nếu hết timeout mà vẫn chưa online → báo offline
  AppState._heartbeatTimer = setTimeout(() => {
    if (!AppState.deviceOnline) {
      UI_setOnlineStatus(false, `${CONFIG.DEVICE_ID} · offline`);
      console.warn('[Main] Chưa nhận heartbeat — thiết bị có thể đang offline');
    }
  }, CONFIG.HEARTBEAT_TIMEOUT_MS);
}

// ═══════════════════════════════════════════════════════════════
// 3. BOOTSTRAP
// ═══════════════════════════════════════════════════════════════

/**
 * Hàm khởi động chính — chạy sau khi DOM sẵn sàng.
 * Mọi lỗi nghiêm trọng đều được bắt ở đây để tránh crash im lặng.
 */
async function main() {
  console.log('═'.repeat(50));
  console.log('  CPS Dashboard — Khởi động');
  console.log('═'.repeat(50));

  if (!_checkDependencies()) return;

  try {
    initSupabase();          // 1. Kết nối Supabase
    initCharts();            // 2. Tạo chart (cần DOM canvas sẵn sàng)
    await loadInitialData(); // 3. Tải dữ liệu lịch sử (async)
    subscribeAll();          // 4. Lắng nghe Realtime
    startPolling();          // 5. Bắt đầu polling định kỳ
    startHeartbeatWatch();   // 6. Watchdog timeout ban đầu

    console.log('[Main] ✓ Khởi động hoàn tất');
  } catch (err) {
    console.error('[Main] ✗ Lỗi khởi động:', err);
  }
}

// ═══════════════════════════════════════════════════════════════
// 4. ENTRY POINT
// ═══════════════════════════════════════════════════════════════

// Chờ DOM sẵn sàng — xử lý cả hai trường hợp:
//   - Script load ở <head> (chưa có DOM) → đợi DOMContentLoaded
//   - Script defer hoặc ở cuối <body>    → DOM đã sẵn sàng, gọi luôn
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', main);
} else {
  main();
}