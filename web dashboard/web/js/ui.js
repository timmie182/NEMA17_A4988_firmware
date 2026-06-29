/**
 * ui.js — Tất cả hàm thao tác DOM của CPS Dashboard
 *
 * Quy tắc: CHỈ file này được phép thay đổi DOM.
 * Các module khác (supabase.js, controls.js…) gọi các hàm UI_* ở đây.
 *
 * Cấu trúc:
 *   1. Hằng số
 *   2. Helpers nội bộ   — el, _pickColor, severityBadge, fmtTime, formatRuntime, formatHours, _eventRow
 *   3. Header           — UI_setOnlineStatus
 *   4. Status Cards     — UI_updateStatusCards
 *   5. Health Gauge     — UI_updateHealthGauge
 *   6. Health Stats     — UI_updateHealthStats
 *   7. Operations Stats — UI_updateOpsStats
 *   8. Control Buttons  — _syncToggleButtons, UI_syncMotorButtons, UI_syncFanButtons,
 *                         UI_syncModeButtons, UI_syncAllButtons
 *   9. Events Table     — UI_prependEvent, UI_renderEvents
 *  10. Input Controls   — UI_setSpeedInput, UI_setControlPending
 */
'use strict';

// ═══════════════════════════════════════════════════════════════
// 1. HẰNG SỐ
// ═══════════════════════════════════════════════════════════════

/** Chu vi vòng tròn SVG gauge (2π × r=50) */
const GAUGE_CIRCUMFERENCE = 314.16;

// ═══════════════════════════════════════════════════════════════
// 2. HELPERS NỘI BỘ
// ═══════════════════════════════════════════════════════════════

/** Shorthand getElementById — tránh lỗi null im lặng */
const el = id => document.getElementById(id);

/**
 * Chọn màu dựa trên giá trị so với các ngưỡng.
 * @param {number} value      — giá trị cần kiểm tra
 * @param {Array}  thresholds — [{max, color}, …] theo thứ tự tăng dần
 * @param {string} fallback   — màu nếu vượt hết ngưỡng
 * @example
 *   _pickColor(45, [{max:30, color:'#22c55e'}, {max:60, color:'#eab308'}], '#ef4444')
 *   // → '#eab308'
 */
function _pickColor(value, thresholds, fallback) {
  for (const { max, color } of thresholds) {
    if (value < max) return color;
  }
  return fallback;
}

/**
 * Tạo HTML badge severity cho bảng Events.
 * Mặc định về 'info' nếu severity không khớp.
 */
function severityBadge(severity) {
  const MAP = {
    info    : ['li', 'INFO'    ],
    warning : ['lw', 'WARN'    ],
    error   : ['le', 'ERROR'   ],
    critical: ['le', 'CRITICAL'],
  };
  const [cls, label] = MAP[severity] || MAP.info;
  return `<span class="lbadge ${cls}">${label}</span>`;
}

/** Format ISO string → "HH:MM:SS" theo múi giờ VN */
const fmtTime = isoStr =>
  isoStr
    ? new Date(isoStr).toLocaleTimeString('vi-VN', { hour12: false })
    : '--:--:--';

/** Format ISO string → "DD/MM/YYYY" theo múi giờ VN */
const fmtDate = isoStr =>
  isoStr
    ? new Date(isoStr).toLocaleDateString('vi-VN', {
        day: '2-digit', month: '2-digit', year: 'numeric',
      })
    : '';

/**
 * Format tổng giây → chuỗi ngắn gọn.
 * Ví dụ: 3661s → "1h 1m" | 75s → "1m 15s" | 9s → "9s"
 */
function formatRuntime(totalSec) {
  if (!totalSec || totalSec < 0) return '--';
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

/**
 * Format tổng giờ vận hành.
 * Dưới 1 giờ → hiển thị phút. Từ 1 giờ trở lên → 1 chữ số thập phân.
 */
const formatHours = h =>
  h == null ? '--' : h < 1 ? `${Math.round(h * 60)}m` : `${h.toFixed(1)}`;

/**
 * Build HTML một hàng bảng Events.
 * Tách ra để dùng chung giữa UI_prependEvent và UI_renderEvents.
 * Cột thời gian hiển thị 2 dòng: giờ (to) + ngày/tháng/năm (nhỏ).
 */
const _eventRow = evt => `
  <tr>
    <td>
      ${fmtTime(evt.created_at)} - <span class="evt-date">${fmtDate(evt.created_at)}</span>
    </td>
    <td>${evt.event_name || ''}</td>
    <td>${severityBadge(evt.severity)}</td>
  </tr>`;

// ═══════════════════════════════════════════════════════════════
// 3. HEADER — ONLINE / OFFLINE BADGE
// ═══════════════════════════════════════════════════════════════

/**
 * Đổi màu + text badge kết nối ở góc trên phải.
 * @param {boolean} online
 * @param {string}  [label] — vd. "stepper_01 · online"
 */
function UI_setOnlineStatus(online, label) {
  const badge = document.querySelector('.online-badge');
  if (!badge) return;

  // Màu nền / viền / chữ theo trạng thái
  const clr = online ? '34,197,94' : '239,68,68';
  badge.style.background  = `rgba(${clr},.12)`;
  badge.style.borderColor = `rgba(${clr},.35)`;
  badge.style.color       = online ? '#22c55e' : '#ef4444';

  // Chấm nhấp nháy
  const pulse = badge.querySelector('.pulse');
  if (pulse) pulse.style.background = online ? '#22c55e' : '#ef4444';

  // Text node cuối cùng trong badge (tránh dùng querySelector thêm)
  const textNode = badge.childNodes[badge.childNodes.length - 1];
  if (textNode) {
    textNode.textContent =
      label || (online ? `${CONFIG.DEVICE_ID} · online` : 'offline');
  }
}

// ═══════════════════════════════════════════════════════════════
// 4. STATUS CARDS — Motor / RPM / Nhiệt độ / Quạt
// ═══════════════════════════════════════════════════════════════

/**
 * Cập nhật 4 thẻ trạng thái trên đầu trang.
 * Gọi mỗi khi nhận dữ liệu sensor hoặc control_state mới.
 */
function UI_updateStatusCards(state) {
  // Motor: ON (xanh) / OFF (mờ)
  const mEl = el('motorStatus');
  if (mEl) {
    mEl.textContent = state.motor ? 'ON' : 'OFF';
    mEl.className   = `s-val ${state.motor ? 'g' : 'muted'}`;
  }

  // RPM: số thực tế từ cảm biến
  const rEl = el('rpmVal');
  if (rEl) rEl.textContent = state.rpm != null ? state.rpm.toLocaleString() : '--';

  // Nhiệt độ: đỏ khi có dữ liệu, mờ khi chưa có
  const tEl = el('tempVal');
  if (tEl) {
    tEl.textContent = state.temperature != null ? state.temperature.toFixed(1) : '--';
    const parentVal = tEl.closest('.s-val');
    if (parentVal) {
      parentVal.className = `s-val ${state.temperature != null ? 'r' : 'muted'}`;
    }
  }

  // Quạt: ON (xanh) / OFF (mờ)
  const fEl = el('fanStatus');
  if (fEl) {
    fEl.textContent = state.fan ? 'ON' : 'OFF';
    fEl.className   = `s-val ${state.fan ? 'g' : 'muted'}`;
  }
}

// ═══════════════════════════════════════════════════════════════
// 5. HEALTH GAUGE
// ═══════════════════════════════════════════════════════════════

/**
 * Cập nhật vòng tròn gauge sức khỏe.
 * Dùng SVG stroke-dasharray để vẽ phần trăm.
 * Màu: xanh ≥70% | vàng ≥40% | đỏ <40%
 */
function UI_updateHealthGauge(healthScore) {
  const arc  = el('gaugeArc');
  const hPct = el('hPct');
  if (!arc || !hPct) return;

  const pct    = Math.max(0, Math.min(100, healthScore));
  const filled = (pct / 100) * GAUGE_CIRCUMFERENCE;

  // Đảo chiều: score thấp → nguy hiểm, dùng (100 - pct) để chọn màu
  const color = _pickColor(100 - pct,
    [{ max: 30, color: '#22c55e' }, { max: 60, color: '#eab308' }],
    '#ef4444'
  );

  arc.style.stroke          = color;
  arc.style.strokeDasharray = `${filled.toFixed(2)} ${GAUGE_CIRCUMFERENCE}`;
  hPct.textContent          = `${pct.toFixed(1)}%`;
  hPct.style.color          = color;
}

// ═══════════════════════════════════════════════════════════════
// 6. HEALTH STATS
// ═══════════════════════════════════════════════════════════════

/**
 * Cập nhật 3 chỉ số trong panel "Dự đoán độ bền":
 *   - Thời gian sử dụng còn lại  (remLife)
 *   - Mức độ mài mòn             (wearLvl)
 *   - Xác suất hỏng hóc          (failPct + failBadge)
 */
function UI_updateHealthStats(state) {
  // Tuổi thọ còn lại: trên 1000h → rút gọn "Xk giờ"
  const remEl = el('remLife');
  if (remEl) {
    const h = state.remainingLifeHours;
    remEl.textContent = h > 1000
      ? `${(h / 1000).toFixed(1)}k giờ`
      : `${h.toFixed(0)} giờ`;
  }

  // Wear level: nhãn + màu theo 4 mức (Thấp / Vừa / Cao / Nguy hiểm)
  const wearEl = el('wearLvl');
  if (wearEl) {
    const w      = state.wearLevel;
    const labels = [[30, 'Thấp'], [60, 'Vừa'], [85, 'Cao']];
    const label  = (labels.find(([max]) => w < max) || [, 'Nguy hiểm'])[1];
    wearEl.textContent = `${w.toFixed(2)}% — ${label}`;
    wearEl.style.color = _pickColor(w,
      [{ max: 30, color: '#22c55e' }, { max: 60, color: '#eab308' }, { max: 85, color: '#f97316' }],
      '#ef4444'
    );
  }

  // Failure probability: màu + badge nhãn trạng thái
  const failPctEl   = el('failPct');
  const failBadgeEl = el('failBadge');
  if (failPctEl) {
    const fp    = state.failureProbability;
    const color = _pickColor(fp,
      [{ max: 15, color: '#22c55e' }, { max: 40, color: '#eab308' }, { max: 70, color: '#f97316' }],
      '#ef4444'
    );
    failPctEl.textContent = `${fp.toFixed(1)}%`;
    failPctEl.style.color = color;

    if (failBadgeEl) {
      const badges = [[15, '✓ AN TOÀN'], [40, '⚠ CHÚ Ý'], [70, '⚠ CẢNH BÁO']];
      failBadgeEl.textContent = (badges.find(([max]) => fp < max) || [, '✖ NGUY HIỂM'])[1];
      failBadgeEl.style.color = color;
    }
  }
}

// ═══════════════════════════════════════════════════════════════
// 7. OPERATIONS STATS — 6 ô thống kê vận hành
// ═══════════════════════════════════════════════════════════════

/**
 * Cập nhật bảng thống kê vận hành.
 * Gọi mỗi khi nhận cyber_results mới từ Supabase.
 */
function UI_updateOpsStats(state) {
  // Mỗi phần tử: [id element, giá trị hiển thị]
  const updates = [
    ['totHours', formatHours(state.totalHours)                                    ],
    ['ovhCount', state.overheatCount                                              ],
    ['fanActEl', state.fanActivations                                             ],
    ['avgTmpEl', state.avgTemp ? state.avgTemp.toFixed(1)             : '--'      ],
    ['avgRpmEl', state.avgRpm  ? Math.round(state.avgRpm).toLocaleString() : '--' ],
    ['todayRT',  formatRuntime(state.todayRuntimeSec)                             ],
  ];
  for (const [id, text] of updates) {
    const elem = el(id);
    if (elem) elem.textContent = text;
  }
}

// ═══════════════════════════════════════════════════════════════
// 8. CONTROL BUTTONS
// ═══════════════════════════════════════════════════════════════

/**
 * Helper chung: đồng bộ cặp nút ON/OFF (hoặc tương đương).
 * @param {string}  idOn     — id nút "bật"
 * @param {string}  idOff    — id nút "tắt"
 * @param {boolean} isActive — true = nút ON đang active
 * @param {string}  clsOn    — class active cho nút ON  (vd. 'on')
 * @param {string}  clsOff   — class active cho nút OFF (vd. 'off-act')
 */
function _syncToggleButtons(idOn, idOff, isActive, clsOn, clsOff) {
  const btnOn  = el(idOn);
  const btnOff = el(idOff);
  if (!btnOn || !btnOff) return;
  btnOn.className  = `cb${isActive  ? ` ${clsOn}`  : ''}`;
  btnOff.className = `cb${!isActive ? ` ${clsOff}` : ''}`;
}

/** Đồng bộ cặp nút ON / OFF động cơ */
const UI_syncMotorButtons = motorOn =>
  _syncToggleButtons('btnMOn', 'btnMOff', motorOn, 'on', 'off-act');

/** Đồng bộ cặp nút ON / OFF quạt */
const UI_syncFanButtons = fanOn =>
  _syncToggleButtons('btnFOn', 'btnFOff', fanOn, 'on', 'off-act');

/** Đồng bộ cặp nút AUTO / MANUAL */
function UI_syncModeButtons(mode) {
  const btnAuto   = el('btnAuto');
  const btnManual = el('btnManual');
  if (!btnAuto || !btnManual) return;
  btnAuto.className   = `cb${mode === 'auto'   ? ' auto'       : ''}`;
  btnManual.className = `cb${mode === 'manual' ? ' manual-act' : ''}`;
}

/** Cập nhật tất cả nút điều khiển cùng lúc theo state */
function UI_syncAllButtons(state) {
  UI_syncMotorButtons(state.motor);
  UI_syncFanButtons(state.fan);
  UI_syncModeButtons(state.mode);
}

// ═══════════════════════════════════════════════════════════════
// 9. EVENTS TABLE
// ═══════════════════════════════════════════════════════════════

/**
 * Thêm 1 sự kiện mới lên ĐẦU bảng Events (realtime).
 * Tự cắt bỏ hàng cuối nếu vượt quá EVENT_LOG_LIMIT.
 */
function UI_prependEvent(evt) {
  const tbody = el('evtBody');
  if (!tbody) return;

  const tr = document.createElement('tr');
  tr.innerHTML = _eventRow(evt);
  tbody.insertBefore(tr, tbody.firstChild);

  // Giới hạn số hàng hiển thị
  while (tbody.rows.length > CONFIG.EVENT_LOG_LIMIT) {
    tbody.removeChild(tbody.lastChild);
  }
}

/**
 * Render lại TOÀN BỘ bảng Events (dùng khi load dữ liệu ban đầu).
 * @param {Array} events — mảng event objects từ Supabase
 */
function UI_renderEvents(events) {
  const tbody = el('evtBody');
  if (tbody) tbody.innerHTML = events.map(_eventRow).join('');
}

// ═══════════════════════════════════════════════════════════════
// 10. INPUT CONTROLS
// ═══════════════════════════════════════════════════════════════

/** Đặt giá trị đồng bộ cho ô nhập RPM và thanh trượt */
function UI_setSpeedInput(value) {
  const inp = el('speedInput');
  const sld = el('speedSlider');
  if (inp) inp.value = value;
  if (sld) sld.value = value;
}

/**
 * Bật/tắt hiệu ứng "đang chờ xác nhận" trên panel điều khiển.
 * CSS class is-pending dùng để hiện spinner hoặc disable input.
 */
function UI_setControlPending(isPending) {
  const panel = document.querySelector('.ctrl-panel');
  if (panel) panel.classList.toggle('is-pending', isPending);
}