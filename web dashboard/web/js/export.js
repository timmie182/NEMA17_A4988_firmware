/**
 * export.js — Xuất dữ liệu ra file CSV để phân tích ngoài (MATLAB, Excel...)
 *
 * Hàm công khai:
 *   exportCSV() — query Supabase, gộp dữ liệu sensor + cyber, tải file .csv
 *
 * Cấu trúc CSV xuất ra:
 *   timestamp, temperature_C, rpm, health_score, wear_level_pct,
 *   failure_probability_pct, remaining_life_hours
 *
 * Dùng trong MATLAB:
 *   data = readtable('CPS_data_xxx.csv');
 *   plot(data.temperature_C)
 *
 * Phụ thuộc: config.js (CONFIG), supabase.js (window._sbClient)
 */
'use strict';

async function exportCSV() {
  if (!window._sbClient) {
    alert('Supabase chưa sẵn sàng');
    return;
  }

  try {
    // Query song song 2 bảng
    const [sensorRes, cyberRes] = await Promise.all([
      window._sbClient
        .from(CONFIG.TABLES.RAW_SENSOR)
        .select('created_at, temperature, rpm')
        .eq('device_id', CONFIG.DEVICE_ID)
        .order('created_at', { ascending: true })
        .limit(5000),

      window._sbClient
        .from(CONFIG.TABLES.CYBER_RESULTS)
        .select('created_at, health_score, wear_level, failure_probability, remaining_life_hours')
        .eq('device_id', CONFIG.DEVICE_ID)
        .order('created_at', { ascending: true })
        .limit(5000),
    ]);

    if (sensorRes.error) throw sensorRes.error;
    if (cyberRes.error)  throw cyberRes.error;

    // Map cyber theo phút để join với sensor
    const cyberMap = {};
    (cyberRes.data || []).forEach(r => {
      const key = r.created_at?.slice(0, 16);
      if (key) cyberMap[key] = r;
    });

    // Header
    const headers = [
      'timestamp',
      'temperature_C',
      'rpm',
      'health_score',
      'wear_level_pct',
      'failure_probability_pct',
      'remaining_life_hours',
    ];

    // Rows — join sensor + cyber theo phút
    const rows = (sensorRes.data || []).map(r => {
      const key   = r.created_at?.slice(0, 16);
      const cyber = cyberMap[key] || {};
      return [
        r.created_at                  ?? '',
        r.temperature                 ?? '',
        r.rpm                         ?? '',
        cyber.health_score            ?? '',
        cyber.wear_level              ?? '',
        cyber.failure_probability     ?? '',
        cyber.remaining_life_hours    ?? '',
      ].join(',');
    });

    // Tạo và tải file
    const csv  = [headers.join(','), ...rows].join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const link = document.createElement('a');
    const now  = new Date().toISOString().slice(0, 16).replace('T', '_').replace(':', '-');

    link.href     = url;
    link.download = `CPS_data_${CONFIG.DEVICE_ID}_${now}.csv`;
    link.click();
    URL.revokeObjectURL(url);

    console.log(`[Export] ✓ Xuất ${rows.length} hàng`);

  } catch (err) {
    console.error('[Export] ✗ Lỗi:', err);
    alert('Xuất CSV thất bại: ' + err.message);
  }
}