/**
 * supabase.js — Kết nối Supabase, realtime + polling kết hợp
 *
 * Chiến lược hybrid (hai lớp nhận dữ liệu):
 *   • Realtime (WebSocket / postgres_changes): raw_sensor, cyber_results, events
 *     → tần suất cao, chấp nhận mất gói thỉnh thoảng
 *   • Polling (setInterval): control_state, heartbeat
 *     → critical, cần đáng tin cậy hơn
 *     (Supabase Realtime filter không ổn định khi RLS tắt hoàn toàn)
 *
 * Lưu ý: KHÔNG dùng filter trong postgres_changes vì không ổn định.
 * Thay vào đó mỗi handler tự lọc device_id trong JS.
 *
 * Cấu trúc:
 *   1. initSupabase()       — tạo Supabase client
 *   2. Fetch helpers        — _fetchLatest, fetchSensorHistory, fetchCyberHistory, fetchRecentEvents
 *   3. Data handlers        — onNewSensorData, onNewCyberResult, onNewControlState, onNewEvent, onNewHeartbeat
 *   4. Polling              — _pollXxx + startPolling()
 *   5. Realtime             — subscribeAll()
 *   6. Bootstrap            — loadInitialData()
 *   7. Helpers              — fmtTimeShort, visibilitychange
 */
'use strict';

// ═══════════════════════════════════════════════════════════════
// 1. KHỞI TẠO SUPABASE CLIENT
// ═══════════════════════════════════════════════════════════════

/** Tạo Supabase client và gán vào window._sbClient để các module khác dùng chung */
function initSupabase() {
  window._sbClient = supabase.createClient(
    CONFIG.SUPABASE_URL,
    CONFIG.SUPABASE_ANON_KEY,
  );
  console.log('[Supabase] ✓ Client khởi tạo');
}

// ═══════════════════════════════════════════════════════════════
// 2. FETCH HELPERS
// ═══════════════════════════════════════════════════════════════

/**
 * Lấy 1 bản ghi mới nhất từ bảng bất kỳ theo device_id.
 * Trả về null nếu lỗi hoặc không có dữ liệu.
 *
 * @param {string} table   — tên bảng Supabase
 * @param {string} columns — danh sách cột cần lấy (mặc định '*')
 */
async function _fetchLatest(table, columns = '*') {
  try {
    const { data, error } = await window._sbClient
      .from(table)
      .select(columns)
      .eq('device_id', CONFIG.DEVICE_ID)
      .order('created_at', { ascending: false })
      .limit(1)
      .single();
    return error ? null : data;
  } catch { return null; }
}

/**
 * Helper dùng chung cho fetchSensorHistory và fetchCyberHistory:
 * Lấy N bản ghi gần nhất theo thứ tự giảm dần rồi đảo lại → tăng dần cho chart.
 *
 * @param {string} table   — tên bảng
 * @param {string} columns — cột cần lấy
 * @param {number} limit   — số bản ghi tối đa
 * @param {string} tag     — tên hiển thị trong log lỗi
 */
async function _fetchHistory(table, columns, limit, tag) {
  try {
    const { data, error } = await window._sbClient
      .from(table)
      .select(columns)
      .eq('device_id', CONFIG.DEVICE_ID)
      .order('created_at', { ascending: false })
      .limit(limit);
    if (error) throw error;
    return (data || []).reverse();   // đảo lại → thứ tự thời gian tăng dần
  } catch (err) {
    console.error(`[Supabase] ${tag}:`, err.message);
    return [];
  }
}

/** Lấy lịch sử sensor (temperature, rpm) để nạp vào chart lúc khởi động */
const fetchSensorHistory = () =>
  _fetchHistory(
    CONFIG.TABLES.RAW_SENSOR,
    'temperature, rpm, created_at',
    CONFIG.SENSOR_HISTORY_LIMIT,
    'fetchSensorHistory',
  );

/** Lấy lịch sử health score để nạp vào chart lúc khởi động */
const fetchCyberHistory = () =>
  _fetchHistory(
    CONFIG.TABLES.CYBER_RESULTS,
    'health_score, created_at',
    CONFIG.SENSOR_HISTORY_LIMIT,
    'fetchCyberHistory',
  );

/** Lấy danh sách sự kiện gần nhất để hiển thị trong bảng Events */
async function fetchRecentEvents() {
  try {
    const { data, error } = await window._sbClient
      .from(CONFIG.TABLES.EVENTS)
      .select('event_name, severity, created_at')
      .eq('device_id', CONFIG.DEVICE_ID)
      .order('created_at', { ascending: false })
      .limit(CONFIG.EVENT_LOG_LIMIT);
    if (error) throw error;
    return data || [];
  } catch (err) {
    console.error('[Supabase] fetchRecentEvents:', err.message);
    return [];
  }
}

// ═══════════════════════════════════════════════════════════════
// 3. DATA HANDLERS — xử lý khi có dữ liệu mới (từ Realtime hoặc Polling)
// ═══════════════════════════════════════════════════════════════

/**
 * Guard chung: bỏ qua bản ghi không thuộc device hiện tại.
 * Supabase có thể trả về nhiều device trong cùng channel khi RLS tắt.
 */
const _wrongDevice = row =>
  row.device_id && row.device_id !== CONFIG.DEVICE_ID;

/** Xử lý dữ liệu cảm biến mới — cập nhật state, status cards, chart */
function onNewSensorData(row) {
  if (_wrongDevice(row)) return;
  AppState.temperature = row.temperature;
  AppState.rpm         = row.rpm;
  UI_updateStatusCards(AppState);
  Charts_addSensorPoint(fmtTimeShort(row.created_at), row.temperature, row.rpm);
}

/**
 * Xử lý kết quả cyber mới — cập nhật toàn bộ health panel + ops stats.
 * Dùng ?? để giữ nguyên giá trị cũ nếu cột bị null trong bản ghi mới.
 */
function onNewCyberResult(row) {
  if (_wrongDevice(row)) return;

  // Cập nhật AppState — ưu tiên giá trị mới, fallback về giá trị hiện tại
  AppState.healthScore        = row.health_score         ?? AppState.healthScore;
  AppState.wearLevel          = row.wear_level           ?? AppState.wearLevel;
  AppState.failureProbability = row.failure_probability  ?? AppState.failureProbability;
  AppState.remainingLifeHours = row.remaining_life_hours ?? AppState.remainingLifeHours;
  AppState.totalHours         = row.total_hours          ?? AppState.totalHours;
  AppState.overheatCount      = row.overheat_count       ?? AppState.overheatCount;
  AppState.fanActivations     = row.fan_activation_count ?? AppState.fanActivations;
  AppState.avgTemp            = row.avg_temperature      ?? AppState.avgTemp;
  AppState.avgRpm             = row.avg_rpm              ?? AppState.avgRpm;
  AppState.todayRuntimeSec    = row.today_runtime_sec    ?? AppState.todayRuntimeSec;

  UI_updateHealthGauge(AppState.healthScore);
  UI_updateHealthStats(AppState);
  UI_updateOpsStats(AppState);
  Charts_addHealthPoint(fmtTimeShort(row.created_at), AppState.healthScore);
}

/**
 * Xử lý control_state đã được Pi xác nhận.
 * Ép kiểu an toàn vì Supabase có thể trả về boolean hoặc string 'true'/'false'.
 */
function onNewControlState(row) {
  if (_wrongDevice(row)) return;

  // Chỉ cập nhật nếu field có giá trị thực (tránh ghi đè bằng undefined)
  if (row.motor != null) AppState.motor = row.motor === true || row.motor === 'true';
  if (row.fan   != null) AppState.fan   = row.fan   === true || row.fan   === 'true';
  if (row.mode)          AppState.mode  = row.mode;

  UI_syncAllButtons(AppState);
  UI_updateStatusCards(AppState);
  if (typeof syncModeUI === 'function') syncModeUI(AppState.mode);
  if (typeof UI_setControlPending === 'function') UI_setControlPending(false);

  console.log(`[Supabase] ✓ control_state: motor=${AppState.motor} fan=${AppState.fan} mode=${AppState.mode}`);
}

/** Xử lý sự kiện mới — thêm vào cache và hiện lên đầu bảng */
function onNewEvent(row) {
  if (_wrongDevice(row)) return;
  AppState.events.unshift(row);
  if (AppState.events.length > CONFIG.EVENT_LOG_LIMIT) AppState.events.pop();
  UI_prependEvent(row);
}

/**
 * Xử lý heartbeat mới — đánh dấu online và reset watchdog timer.
 * Watchdog tự động chuyển offline nếu không nhận heartbeat trong HEARTBEAT_TIMEOUT_MS.
 */
function onNewHeartbeat(row) {
  if (_wrongDevice(row)) return;

  AppState.deviceOnline      = true;
  AppState.lastHeartbeatTime = new Date();
  UI_setOnlineStatus(true, `${CONFIG.DEVICE_ID} · online`);

  // Xóa timer cũ, đặt timer mới — nếu timeout sẽ chuyển sang offline
  clearTimeout(AppState._heartbeatTimer);
  AppState._heartbeatTimer = setTimeout(() => {
    AppState.deviceOnline = false;
    UI_setOnlineStatus(false, `${CONFIG.DEVICE_ID} · offline`);
    console.warn('[Supabase] ⚠ Heartbeat timeout');
  }, CONFIG.HEARTBEAT_TIMEOUT_MS);
}

// ═══════════════════════════════════════════════════════════════
// 4. POLLING — dự phòng đáng tin cậy hơn Realtime
// ═══════════════════════════════════════════════════════════════

/**
 * Timestamp của bản ghi cuối cùng đã xử lý — dùng để bỏ qua khi không có gì mới.
 * So sánh created_at (ISO string) thay vì id để tránh race condition.
 */
let _lastControlStateTs = null;
let _lastHeartbeatTs    = null;
let _lastSensorTs       = null;
let _lastCyberTs        = null;
let _lastEventTs        = null;

/**
 * Helper poll chung: fetch bản ghi mới nhất, so sánh timestamp,
 * chỉ gọi handler nếu thực sự có dữ liệu mới.
 *
 * @param {string}   table    — tên bảng
 * @param {string}   columns  — cột cần lấy
 * @param {string}   lastTs   — tên biến lưu timestamp lần poll trước (tham chiếu qua getter/setter)
 * @param {Function} handler  — hàm xử lý khi có dữ liệu mới
 * @param {Function} getTs    — getter của biến _lastXxxTs
 * @param {Function} setTs    — setter của biến _lastXxxTs
 */
async function _poll(table, columns, getTs, setTs, handler) {
  const row = await _fetchLatest(table, columns);
  if (!row || row.created_at === getTs()) return;
  setTs(row.created_at);
  handler(row);
}

/** Poll control_state mỗi 2s — cần nhạy để lệnh điều khiển phản hồi nhanh */
const _pollControlState = () => _poll(
  CONFIG.TABLES.CONTROL_STATE, '*',
  () => _lastControlStateTs, v => (_lastControlStateTs = v),
  onNewControlState,
);

/** Poll sensor mỗi 2s — sensor Pi gửi mỗi ~2s */
const _pollSensor = () => _poll(
  CONFIG.TABLES.RAW_SENSOR, 'temperature, rpm, device_id, created_at',
  () => _lastSensorTs, v => (_lastSensorTs = v),
  onNewSensorData,
);

/** Poll cyber_results mỗi 10s — bridge tính toán mỗi ~10s */
const _pollCyber = () => _poll(
  CONFIG.TABLES.CYBER_RESULTS, '*',
  () => _lastCyberTs, v => (_lastCyberTs = v),
  onNewCyberResult,
);

/**
 * Poll heartbeat mỗi 15s.
 * Khác với các poll khác: cần kiểm tra ĐỘ TUỔI bản ghi,
 * không chỉ so sánh timestamp — vì heartbeat quá cũ nghĩa là offline.
 */
async function _pollHeartbeat() {
  const row = await _fetchLatest(CONFIG.TABLES.HEARTBEAT, 'device_id, status, created_at');
  if (!row) return;

  const ageMs = Date.now() - new Date(row.created_at).getTime();

  if (ageMs < CONFIG.HEARTBEAT_TIMEOUT_MS) {
    // Heartbeat còn tươi → xử lý nếu là bản ghi mới
    if (row.created_at !== _lastHeartbeatTs) {
      _lastHeartbeatTs = row.created_at;
      onNewHeartbeat(row);
    }
  } else if (AppState.deviceOnline) {
    // Heartbeat quá cũ và đang ở trạng thái online → chuyển offline
    AppState.deviceOnline = false;
    UI_setOnlineStatus(false, `${CONFIG.DEVICE_ID} · offline`);
    console.warn(`[Supabase] Heartbeat quá cũ (${Math.round(ageMs / 1000)}s)`);
  }
}

/**
 * Poll events mỗi 3s.
 * Riêng vì cần select thêm cột device_id để lọc, khác với _poll chung.
 */
async function _pollEvents() {
  try {
    const { data, error } = await window._sbClient
      .from(CONFIG.TABLES.EVENTS)
      .select('event_name, severity, created_at, device_id')
      .eq('device_id', CONFIG.DEVICE_ID)
      .order('created_at', { ascending: false })
      .limit(1)
      .single();
    if (error || !data || data.created_at === _lastEventTs) return;
    _lastEventTs = data.created_at;
    onNewEvent(data);
  } catch { /* bỏ qua lỗi mạng tạm thời */ }
}

/**
 * Bắt đầu tất cả vòng polling.
 * Poll ngay lần đầu (không chờ interval đầu tiên), sau đó theo chu kỳ.
 */
function startPolling() {
  // Chạy ngay lập tức
  _pollControlState();
  _pollHeartbeat();
  _pollSensor();
  _pollCyber();
  _pollEvents();

  // Đặt chu kỳ lặp — interval theo tần suất thay đổi của từng nguồn dữ liệu
  setInterval(_pollControlState,  2_000);   // 2s  — lệnh điều khiển cần phản hồi nhanh
  setInterval(_pollSensor,        2_000);   // 2s  — sensor Pi gửi mỗi ~2s
  setInterval(_pollEvents,        3_000);   // 3s  — events không quá khẩn cấp
  setInterval(_pollCyber,        10_000);   // 10s — cyber_results tính toán mỗi ~10s
  setInterval(_pollHeartbeat,    15_000);   // 15s — heartbeat Pi gửi mỗi 12s
}

// ═══════════════════════════════════════════════════════════════
// 5. REALTIME — WebSocket cho dữ liệu tần suất cao
// ═══════════════════════════════════════════════════════════════

/**
 * Helper tạo một Realtime channel theo dõi INSERT trên một bảng.
 *
 * @param {string}   channelName — tên channel (phải unique trong app)
 * @param {string}   table       — tên bảng Supabase
 * @param {Function} handler     — hàm xử lý khi có INSERT mới (nhận p.new)
 * @param {string}   [logLabel]  — nhãn hiển thị trong log (mặc định = table)
 * @param {Function} [onError]   — callback khi CHANNEL_ERROR hoặc TIMED_OUT
 */
function _subscribe(channelName, table, handler, logLabel, onError) {
  window._sbClient
    .channel(channelName)
    .on('postgres_changes', { event: 'INSERT', schema: 'public', table }, p => handler(p.new))
    .subscribe(status => {
      if (status === 'SUBSCRIBED')
        console.log(`[Realtime] ✓ ${logLabel || table}`);
      if ((status === 'CHANNEL_ERROR' || status === 'TIMED_OUT') && onError)
        onError(status);
    });
}

/**
 * Đăng ký tất cả Realtime channels.
 * control_state và heartbeat chỉ là backup — polling mới là nguồn chính.
 * Khi Realtime nhận được, cập nhật timestamp để poll tiếp theo bỏ qua trùng lặp.
 */
function subscribeAll() {
  // raw_sensor — tần suất cao (~2s), cập nhật chart + status cards
  _subscribe('cps_raw_sensor', CONFIG.TABLES.RAW_SENSOR, onNewSensorData, 'raw_sensor',
    s => console.warn('[Realtime] ✗ raw_sensor:', s));

  // cyber_results — tần suất thấp (~10s), cập nhật health panel
  _subscribe('cps_cyber', CONFIG.TABLES.CYBER_RESULTS, onNewCyberResult, 'cyber_results');

  // events — tần suất thấp, append vào bảng sự kiện
  _subscribe('cps_events', CONFIG.TABLES.EVENTS, onNewEvent, 'events');

  // control_state — BACKUP: cập nhật timestamp để poll bỏ qua nếu Realtime đến trước
  _subscribe('cps_ctrl_state', CONFIG.TABLES.CONTROL_STATE, row => {
    _lastControlStateTs = row.created_at;
    onNewControlState(row);
  }, 'control_state (backup)');

  // heartbeat — BACKUP: tương tự control_state
  _subscribe('cps_heartbeat', CONFIG.TABLES.HEARTBEAT, row => {
    _lastHeartbeatTs = row.created_at;
    onNewHeartbeat(row);
  }, 'heartbeat (backup)');
}

// ═══════════════════════════════════════════════════════════════
// 6. BOOTSTRAP — tải dữ liệu ban đầu khi mở trang
// ═══════════════════════════════════════════════════════════════

/**
 * Tải song song tất cả dữ liệu cần thiết để hiển thị dashboard ngay lập tức.
 * Gọi một lần trong main.js sau initSupabase() và initCharts().
 */
async function loadInitialData() {
  console.log('[Supabase] Đang tải dữ liệu ban đầu...');

  // Tải song song — Promise.all giảm thời gian chờ
  const [sensorRows, cyberRows, latestCyber, latestCtrl, latestHb, events] =
    await Promise.all([
      fetchSensorHistory(),
      fetchCyberHistory(),
      _fetchLatest(CONFIG.TABLES.CYBER_RESULTS),
      _fetchLatest(CONFIG.TABLES.CONTROL_STATE),
      _fetchLatest(CONFIG.TABLES.HEARTBEAT, 'device_id, status, created_at'),
      fetchRecentEvents(),
    ]);

  // ── Chart: nạp lịch sử cảm biến + health ────────────────────
  Charts_loadHistory(sensorRows, cyberRows);

  // ── Status cards: lấy giá trị sensor mới nhất ───────────────
  if (sensorRows.length > 0) {
    const last = sensorRows[sensorRows.length - 1];
    AppState.temperature = last.temperature;
    AppState.rpm         = last.rpm;
    _lastSensorTs        = last.created_at;   // tránh poll lại bản ghi này
    UI_updateStatusCards(AppState);
  }

  // ── Health panel ─────────────────────────────────────────────
  if (latestCyber) {
    _lastCyberTs = latestCyber.created_at;
    onNewCyberResult(latestCyber);
  } else {
    // Chưa có dữ liệu → hiển thị mặc định 100% healthy
    UI_updateHealthGauge(100);
    UI_updateHealthStats(AppState);
    UI_updateOpsStats(AppState);
  }

  // ── Control buttons: đồng bộ trạng thái từ Pi ───────────────
  if (latestCtrl) {
    _lastControlStateTs = latestCtrl.created_at;
    onNewControlState(latestCtrl);
  } else {
    // Chưa có lệnh nào → hiển thị theo AppState mặc định (state.js)
    UI_syncAllButtons(AppState);
    if (typeof syncModeUI === 'function') syncModeUI(AppState.mode);
  }

  // ── Heartbeat: kiểm tra độ tuổi để xác định online/offline ──
  if (latestHb) {
    const ageMs = Date.now() - new Date(latestHb.created_at).getTime();
    if (ageMs < CONFIG.HEARTBEAT_TIMEOUT_MS) {
      _lastHeartbeatTs = latestHb.created_at;
      onNewHeartbeat(latestHb);
    } else {
      UI_setOnlineStatus(false, `${CONFIG.DEVICE_ID} · offline`);
    }
  } else {
    UI_setOnlineStatus(false, 'Chờ kết nối...');
  }

  // ── Events table ─────────────────────────────────────────────
  AppState.events = events;
  UI_renderEvents(events);
  if (events.length > 0) _lastEventTs = events[0].created_at;

  console.log('[Supabase] ✓ Dữ liệu ban đầu sẵn sàng');
}

// ═══════════════════════════════════════════════════════════════
// 7. HELPERS
// ═══════════════════════════════════════════════════════════════

/** Format ISO string → "HH:MM:SS" — dùng làm nhãn trục X cho chart */
const fmtTimeShort = isoStr =>
  isoStr
    ? new Date(isoStr).toLocaleTimeString('vi-VN', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
      })
    : '';

/**
 * Khi tab được focus lại sau khi bị ẩn → poll ngay lập tức thay vì
 * chờ đến interval tiếp theo (có thể vừa bỏ lỡ nhiều dữ liệu).
 */
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    _pollSensor();
    _pollCyber();
    _pollControlState();
    _pollHeartbeat();
  }
});