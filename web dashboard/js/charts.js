/**
 * charts.js — 3 biểu đồ Chart.js với thiết kế tối (dark theme)
 *
 * Tính năng:
 *   - Gradient fill dùng canvas API (afterLayout plugin)
 *   - Đường viền mỏng, lưới mờ, không có border trục
 *   - Tooltip kiểu glass dark
 *   - Animation easeOutQuart 400ms
 *
 * Cấu trúc:
 *   1. Màu sắc & tiện ích rgba/hex
 *   2. Plugin gradient fill (đăng ký vào Chart.js)
 *   3. Handles (biến giữ 3 chart instances)
 *   4. _baseOpts()   — options dùng chung cho cả 3 chart
 *   5. _mainDs()     — dataset đường chính (có gradient fill)
 *   6. _thresholdDs()— dataset đường ngưỡng (nét đứt, không fill)
 *   7. initCharts()  — khởi tạo 3 chart
 *   8. API công khai — Charts_addSensorPoint / Charts_addHealthPoint / Charts_loadHistory
 *   9. Helpers nội bộ — _push / _fmtT
 */
'use strict';

// ═══════════════════════════════════════════════════════════════
// 1. MÀU SẮC & TIỆN ÍCH
// ═══════════════════════════════════════════════════════════════

/** Bảng màu dùng chung — mỗi màu là mảng [R, G, B] để dễ tạo rgba() */
const CLR = {
  green : [34,  197, 94 ],  // #22c55e — health / motor on
  blue  : [59,  130, 246],  // #3b82f6 — RPM
  red   : [239, 68,  68 ],  // #ef4444 — nhiệt độ
  orange: [249, 115, 22 ],  // #f97316 — (dự phòng)
  text  : '#94a3b8',        // màu chữ trục
  grid  : 'rgba(30,45,69,0.45)', // màu đường lưới
};

/** Tạo chuỗi rgba() từ mảng [R,G,B] và độ mờ a */
const rgba = ([r, g, b], a) => `rgba(${r},${g},${b},${a})`;

/** Tạo chuỗi rgb() từ mảng [R,G,B] */
const hex  = ([r, g, b])    => `rgb(${r},${g},${b})`;

// ═══════════════════════════════════════════════════════════════
// 2. PLUGIN: GRADIENT FILL
// ═══════════════════════════════════════════════════════════════

/**
 * Plugin tùy chỉnh Chart.js — tạo gradient fill cho dataset chính.
 *
 * Lý do dùng afterLayout thay vì khai báo trực tiếp trong dataset:
 * chartArea (top/bottom) chỉ có giá trị SAU KHI chart đã layout xong.
 * Plugin này chạy đúng thời điểm đó, tạo gradient từ top → bottom.
 *
 * Dataset cần có thuộc tính `_rgb` (mảng [R,G,B]) mới được áp dụng.
 */
const gradientPlugin = {
  id: 'gradientFill',
  afterLayout(chart) {
    const { top, bottom } = chart.chartArea || {};
    if (top == null) return;   // chartArea chưa sẵn sàng

    chart.data.datasets.forEach(ds => {
      if (!ds._rgb) return;    // chỉ xử lý dataset có _rgb
      const g = chart.ctx.createLinearGradient(0, top, 0, bottom);
      g.addColorStop(0,   rgba(ds._rgb, 0.22));  // đậm ở trên
      g.addColorStop(0.7, rgba(ds._rgb, 0.06));  // mờ dần
      g.addColorStop(1,   rgba(ds._rgb, 0));     // trong suốt ở đáy
      ds.backgroundColor = g;
    });
  },
};
Chart.register(gradientPlugin);

// ═══════════════════════════════════════════════════════════════
// 3. CHART INSTANCES
// ═══════════════════════════════════════════════════════════════

/** 3 instance Chart.js — null cho đến khi initCharts() được gọi */
let chartHealth = null;
let chartTemp   = null;
let chartRpm    = null;

// ═══════════════════════════════════════════════════════════════
// 4. OPTIONS DÙNG CHUNG
// ═══════════════════════════════════════════════════════════════

/**
 * Tạo object options Chart.js dùng chung cho cả 3 biểu đồ.
 * Chỉ trục Y là khác nhau (min/max/tick format).
 *
 * @param {number}   yMin    — giá trị tối thiểu trục Y
 * @param {number}   yMax    — giá trị tối đa trục Y
 * @param {Function} yTickCb — callback format nhãn trục Y (vd. v => v + '°')
 */
function _baseOpts(yMin, yMax, yTickCb) {
  return {
    responsive         : true,
    maintainAspectRatio: false,
    layout             : { padding: { left: 10, right: 10 } },
    animation          : { duration: 400, easing: 'easeOutQuart' },

    // Tooltip hiện theo cột dọc (index mode), không cần hover đúng điểm
    interaction: { mode: 'index', intersect: false },

    plugins: {
      legend : { display: false },   // ẩn legend — dùng label tự làm
      tooltip: {
        backgroundColor: 'rgba(10,18,35,0.92)',
        borderColor    : 'rgba(59,130,246,0.2)',
        borderWidth    : 1,
        titleColor     : '#94a3b8',
        bodyColor      : '#e2e8f0',
        padding        : { x: 10, y: 8 },
        displayColors  : true,
        boxWidth       : 8,
        boxHeight      : 8,
        usePointStyle  : true,
        callbacks: {
          // Hiển thị đơn vị (°C, RPM…) lấy từ thuộc tính _unit của dataset
          label: ctx => ` ${ctx.parsed.y}${ctx.dataset._unit || ''}`,
        },
      },
    },

    scales: {
      x: {
        ticks : {
          color        : CLR.text,
          font         : { size: 15, family: "'Segoe UI', sans-serif" },
          maxRotation  : 0,       // không xoay nhãn
          autoSkip     : true,
          maxTicksLimit: 8,       // tối đa 6 nhãn thời gian
        },
        grid  : { color: CLR.grid, lineWidth: 0.5 },
        border: { display: false },
      },
      y: {
        min  : yMin,
        max  : yMax,
        ticks: {
          color   : CLR.text,
          font    : { size: 15 },
          callback: yTickCb || undefined,
        },
        grid  : { color: CLR.grid, lineWidth: 0.5 },
        border: { display: false },
      },
    },
  };
}

// ═══════════════════════════════════════════════════════════════
// 5 & 6. DATASET HELPERS
// ═══════════════════════════════════════════════════════════════

/**
 * Dataset đường chính — có gradient fill, hover point.
 * Gradient thực sự được gán bởi gradientPlugin (afterLayout).
 *
 * @param {string} label — tên hiển thị trong tooltip
 * @param {Array}  rgb   — màu [R,G,B] từ CLR
 * @param {string} unit  — đơn vị hiển thị trong tooltip (vd. '°C', ' RPM')
 */
function _mainDs(label, rgb, unit = '') {
  return {
    label,
    data                     : [],
    borderColor              : hex(rgb),
    borderWidth              : 1.5,
    pointRadius              : 0,                  // ẩn điểm khi không hover
    pointHoverRadius         : 4,
    pointHoverBackgroundColor: hex(rgb),
    pointHoverBorderColor    : '#0b1120',
    pointHoverBorderWidth    : 2,
    tension                  : 0.4,                // đường cong mượt
    fill                     : true,
    backgroundColor          : rgba(rgb, 0.1),     // placeholder, plugin sẽ override
    _rgb                     : rgb,                // dùng bởi gradientPlugin
    _unit                    : unit,               // dùng bởi tooltip callback
  };
}

/**
 * Dataset đường ngưỡng — nét đứt, không fill, không hover point.
 * Dữ liệu là mảng hằng số (cùng giá trị ngưỡng cho mọi điểm thời gian).
 *
 * @param {string} label — tên hiển thị trong tooltip
 * @param {string} color — màu CSS (rgb/rgba/hex)
 * @param {number} value — giá trị ngưỡng (chỉ dùng để đặt tên, data được fill sau)
 * @param {string} unit  — đơn vị tooltip
 */
function _thresholdDs(label, color, value, unit = '') {
  return {
    label,
    data           : [],          // được điền bằng Array(n).fill(value) khi có data
    borderColor    : color,
    borderWidth    : 1,
    borderDash     : [5, 5],      // nét đứt
    pointRadius    : 0,
    fill           : false,
    tension        : 0,           // đường thẳng ngang
    backgroundColor: 'transparent',
    _unit          : unit,
  };
}

// ═══════════════════════════════════════════════════════════════
// 7. KHỞI TẠO 3 CHART
// ═══════════════════════════════════════════════════════════════

/** Khởi tạo cả 3 biểu đồ — gọi một lần trong main.js */
function initCharts() {
  _initHealthChart();
  _initTempChart();
  _initRpmChart();
}

/** Biểu đồ Health Score (0–100%, màu xanh lá) */
function _initHealthChart() {
  const canvas = document.getElementById('chHealth');
  if (!canvas) return;
  chartHealth = new Chart(canvas.getContext('2d'), {
    type   : 'line',
    data   : { labels: [], datasets: [_mainDs('Health Score', CLR.green, '%')] },
    options: _baseOpts(0, 100, v => `${v}%`),
  });
}

/**
 * Biểu đồ Nhiệt độ (20–100°C, màu đỏ).
 * Có thêm 2 đường ngưỡng nét đứt:
 *   - Ngưỡng bật quạt (FAN_ON_TEMP) — màu vàng
 *   - Ngưỡng cảnh báo (WARN_TEMP)   — màu tím
 */
function _initTempChart() {
  const canvas = document.getElementById('chTemp');
  if (!canvas) return;
  chartTemp = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels  : [],
      datasets: [
        _mainDs('Nhiệt độ', CLR.red, '°C'),
        _thresholdDs(`Ngưỡng quạt ${CONFIG.FAN_ON_TEMP}°C`, 'rgb(255,255,0)',     CONFIG.FAN_ON_TEMP, '°C'),
        _thresholdDs(`Cảnh báo ${CONFIG.WARN_TEMP}°C`,      'rgba(191,64,191)',   CONFIG.WARN_TEMP,   '°C'),
      ],
    },
    options: _baseOpts(20, 100, v => `${v}°`),
  });
}

/** Biểu đồ RPM (0 → RPM_MAX, màu xanh dương) */
function _initRpmChart() {
  const canvas = document.getElementById('chRpm');
  if (!canvas) return;
  chartRpm = new Chart(canvas.getContext('2d'), {
    type   : 'line',
    data   : { labels: [], datasets: [_mainDs('RPM', CLR.blue, ' RPM')] },
    // Tick format: ≥1000 → "1.5k", còn lại hiển thị nguyên
    options: _baseOpts(0, CONFIG.RPM_MAX +30, v => v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v),
  });
}

// ═══════════════════════════════════════════════════════════════
// 8. API CÔNG KHAI
// ═══════════════════════════════════════════════════════════════

/**
 * Thêm 1 điểm sensor mới vào chart Nhiệt độ và RPM (realtime).
 * Đồng thời giữ độ dài 2 dataset ngưỡng khớp với dataset chính.
 *
 * @param {string} timeLabel — nhãn thời gian (HH:MM:SS)
 * @param {number} temp      — nhiệt độ °C
 * @param {number} rpm       — tốc độ vòng/phút
 */
function Charts_addSensorPoint(timeLabel, temp, rpm) {
  _push(chartTemp, timeLabel, temp);
  _push(chartRpm,  timeLabel, rpm);

  if (chartTemp) {
    // Giữ đường ngưỡng luôn đủ điểm, khớp với số nhãn hiện tại
    const n = chartTemp.data.labels.length;
    chartTemp.data.datasets[1].data = Array(n).fill(CONFIG.FAN_ON_TEMP);
    chartTemp.data.datasets[2].data = Array(n).fill(CONFIG.WARN_TEMP);
    chartTemp.update('none');   // 'none' = không animation để realtime mượt
  }
  if (chartRpm) chartRpm.update('none');
}

/**
 * Thêm 1 điểm health score mới vào chart Health (realtime).
 *
 * @param {string} timeLabel — nhãn thời gian
 * @param {number} score     — health score 0–100
 */
function Charts_addHealthPoint(timeLabel, score) {
  _push(chartHealth, timeLabel, score);
  if (chartHealth) chartHealth.update('none');
}

/**
 * Nạp toàn bộ lịch sử vào cả 3 chart khi khởi động trang.
 * Gọi một lần trong loadInitialData() (supabase.js).
 *
 * @param {Array} sensorRows — mảng {temperature, rpm, created_at}
 * @param {Array} cyberRows  — mảng {health_score, created_at}
 */
function Charts_loadHistory(sensorRows, cyberRows) {
  if (!chartTemp || !chartRpm || !chartHealth) return;

  // ── Sensor → Temp + RPM ──────────────────────────────────────
  const labels = sensorRows.map(r => _fmtT(r.created_at));
  const n      = labels.length;

  chartTemp.data.labels           = labels;
  chartTemp.data.datasets[0].data = sensorRows.map(r => r.temperature);
  chartTemp.data.datasets[1].data = Array(n).fill(CONFIG.FAN_ON_TEMP);  // ngưỡng quạt
  chartTemp.data.datasets[2].data = Array(n).fill(CONFIG.WARN_TEMP);    // ngưỡng cảnh báo
  chartTemp.update();

  chartRpm.data.labels           = labels;
  chartRpm.data.datasets[0].data = sensorRows.map(r => r.rpm);
  chartRpm.update();

  // ── Cyber → Health ───────────────────────────────────────────
  if (cyberRows?.length) {
    chartHealth.data.labels           = cyberRows.map(r => _fmtT(r.created_at));
    chartHealth.data.datasets[0].data = cyberRows.map(r => r.health_score);
    chartHealth.update();
  }
}

// ═══════════════════════════════════════════════════════════════
// 9. HELPERS NỘI BỘ
// ═══════════════════════════════════════════════════════════════

/**
 * Thêm 1 điểm vào dataset[0] của chart và tự cắt bỏ điểm cũ
 * nếu vượt quá CHART_MAX_POINTS.
 *
 * @param {Chart}  chart — instance Chart.js
 * @param {string} label — nhãn trục X (thời gian)
 * @param {number} value — giá trị Y
 */
function _push(chart, label, value) {
  if (!chart) return;
  chart.data.labels.push(label);
  chart.data.datasets[0].data.push(value);

  // Xóa điểm cũ nhất nếu vượt giới hạn (sliding window)
  if (chart.data.labels.length > CONFIG.CHART_MAX_POINTS) {
    chart.data.labels.shift();
    chart.data.datasets[0].data.shift();
  }
}

/** Format ISO timestamp → "HH:MM:SS" theo giờ Việt Nam */
const _fmtT = iso =>
  iso
    ? new Date(iso).toLocaleTimeString('vi-VN', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
      })
    : '';