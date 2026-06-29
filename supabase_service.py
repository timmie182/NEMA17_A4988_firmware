"""
supabase_service.py  —  Supabase Service  [v6]
================================================================================
Ghi du lieu thang len Supabase, khong qua MQTT broker.
Chay song song voi MQTTService trong firmware.

  Firmware
    ├─► MQTTService.publish_*()  ──► MQTT ──► Unity (realtime)
    └─► SupabaseService.log_*()  ──► Supabase  (persistence)

HEARTBEAT: dung INSERT moi lan (giong v3 goc).
  Web check online:
    SELECT * FROM heartbeat
    WHERE device_id = 'stepper_01'
    ORDER BY created_at DESC LIMIT 1
  -> kiem tra created_at cua row moi nhat + status = 'online'

CAU HINH:
    export SUPABASE_URL="https://xxxx.supabase.co"
    export SUPABASE_KEY="<key>"
================================================================================
"""

import logging
import queue
import threading

from supabase import create_client

logger = logging.getLogger(__name__)


class SupabaseService:
    """
    Ghi du lieu Supabase qua background queue (khong block vong PID).
    Cac method log_*() dat task vao queue va tra ve ngay.
    Thread rieng thuc thi INSERT/UPDATE/UPSERT trong nen.

    Cac method read (get_latest_result, get_latest_cmd_id, poll_web_commands)
    la DONG BO vi firmware can ket qua ngay.
    """

    def __init__(self, url: str, key: str):
        self._client  = create_client(url, key)
        self._queue   = queue.Queue()
        self._running = False
        self._thread  = None
        logger.info("SupabaseService khoi tao: %s", url)

    # ── Vong doi ────────────────────────────────────────────────────────────

    def start(self):
        """Khoi dong background writer thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name="supa_writer"
        )
        self._thread.start()
        logger.info("SupabaseService writer thread started.")

    def stop(self, timeout: float = 5.0):
        """Doi flush queue roi dung. Goi truoc khi thoat firmware."""
        self._running = False
        try:
            self._queue.join()
        except Exception:
            pass
        logger.info("SupabaseService da dung.")

    def _worker(self):
        while self._running or not self._queue.empty():
            try:
                fn, args, kwargs = self._queue.get(timeout=1.0)
                try:
                    fn(*args, **kwargs)
                except Exception as exc:
                    logger.error("SupabaseService write error: %s", exc)
                finally:
                    self._queue.task_done()
            except queue.Empty:
                continue

    def _enqueue(self, fn, *args, **kwargs):
        """Dat task ghi vao queue, khong block."""
        self._queue.put((fn, args, kwargs))

    # ══════════════════════════════════════════════════════════════════════════
    # WRITE methods — bat dong bo (qua queue)
    # ══════════════════════════════════════════════════════════════════════════

    def log_sensor(self, device_id: str, rpm: float, temperature=None,
                   target_rpm=None, status_rule=None, rpm_error=None,
                   wave_rpm=None, is_stalled=None):
        """raw_sensor table."""
        self._enqueue(self._do_log_sensor, device_id, rpm, temperature,
                      target_rpm, status_rule, rpm_error, wave_rpm, is_stalled)

    def _do_log_sensor(self, device_id, rpm, temperature, target_rpm,
                       status_rule, rpm_error, wave_rpm, is_stalled):
        row = {"device_id": device_id, "rpm": int(round(rpm))}
        if temperature  is not None: row["temperature"]  = round(float(temperature), 2)
        if target_rpm   is not None: row["target_rpm"]   = int(target_rpm)
        if status_rule  is not None: row["status_rule"]  = status_rule
        if rpm_error    is not None: row["rpm_error"]    = round(float(rpm_error), 2)
        if wave_rpm     is not None: row["wave_rpm"]     = round(float(wave_rpm), 2)
        if is_stalled   is not None: row["is_stalled"]  = bool(is_stalled)
        self._client.table("raw_sensor").insert(row).execute()

    # ── heartbeat ────────────────────────────────────────────────────────────

    def log_heartbeat(self, device_id: str, uptime_s: int, status: str = "online"):
        """heartbeat table — INSERT row moi moi lan (giong v3 goc dang hoat dong tot).
        Web check: SELECT * FROM heartbeat WHERE device_id=... ORDER BY created_at DESC LIMIT 1
        => created_at cua row moi nhat phan anh chinh xac thoi diem Pi con song."""
        self._enqueue(self._do_log_heartbeat, device_id, uptime_s, status)

    def _do_log_heartbeat(self, device_id: str, uptime_s: int, status: str):
        self._client.table("heartbeat").insert({
            "device_id": device_id,
            "status":    status,
            "uptime_s":  int(uptime_s),
        }).execute()

    # ── events ───────────────────────────────────────────────────────────────

    def log_event(self, device_id: str, event_name: str, severity: str = "info"):
        """events table."""
        valid = {"info", "warning", "error", "critical"}
        self._enqueue(self._client.table("events").insert({
            "device_id":  device_id,
            "event_name": event_name,
            "severity":   severity if severity in valid else "info",
        }).execute)

    # ── cyber_results ─────────────────────────────────────────────────────────

    def log_cyber_results(self, device_id: str, res: dict,
                          avg_temperature=None, avg_rpm=None,
                          overheat_count: int = 0, fan_activation_count: int = 0,
                          total_hours=None, today_runtime_sec=None):
        """cyber_results table."""
        self._enqueue(self._do_log_cyber, device_id, res, avg_temperature,
                      avg_rpm, overheat_count, fan_activation_count,
                      total_hours, today_runtime_sec)

    def _do_log_cyber(self, device_id, res, avg_temperature, avg_rpm,
                      overheat_count, fan_activation_count,
                      total_hours, today_runtime_sec):
        row = {
            "device_id":            device_id,
            "health_score":         round(float(res["health_score"]),        2),
            "wear_level":           round(float(res["wear_level"]),           4),
            "failure_probability":  round(float(res["failure_probability"]),  4),
            "remaining_life_hours": round(float(res["remaining_life_hours"]), 2),
            "overheat_count":       int(overheat_count),
            "fan_activation_count": int(fan_activation_count),
        }
        if avg_temperature   is not None: row["avg_temperature"]   = round(float(avg_temperature), 2)
        if avg_rpm           is not None: row["avg_rpm"]           = int(round(avg_rpm))
        if total_hours       is not None: row["total_hours"]       = round(float(total_hours), 4)
        if today_runtime_sec is not None: row["today_runtime_sec"] = int(today_runtime_sec)
        self._client.table("cyber_results").insert(row).execute()

    # ── control_state ─────────────────────────────────────────────────────────

    def log_control_state(self, device_id: str, motor: bool,
                          mode: str = "manual", fan: bool = False):
        """control_state table."""
        self._enqueue(
            self._client.table("control_state").insert({
                "device_id": device_id, "motor": motor,
                "fan": fan, "mode": mode,
            }).execute
        )

    # ── control_commands ──────────────────────────────────────────────────────

    def log_command(self, device_id: str, motor=None, target_rpm=None,
                    source: str = "mqtt", mode: str = "manual", fan: bool = False,
                    revolutions=None):
        """control_commands table."""
        row = {"device_id": device_id, "source": source, "mode": mode, "fan": fan}
        if motor       is not None: row["motor"]      = bool(motor)
        if target_rpm  is not None: row["target_rpm"] = max(1, int(target_rpm))
        if revolutions is not None: row["revolutions"]= int(revolutions)
        self._enqueue(self._client.table("control_commands").insert(row).execute)

    # ══════════════════════════════════════════════════════════════════════════
    # READ methods — dong bo (firmware can ket qua ngay)
    # ══════════════════════════════════════════════════════════════════════════

    def get_latest_result(self, device_id: str) -> dict | None:
        """Lay ket qua suc khoe moi nhat de seed runtime khi khoi dong."""
        try:
            r = (self._client.table("cyber_results")
                 .select("wear_level, total_hours")
                 .eq("device_id", device_id)
                 .order("created_at", desc=True)
                 .limit(1)
                 .execute())
            return r.data[0] if r.data else None
        except Exception as exc:
            logger.warning("get_latest_result loi: %s", exc)
            return None

    def get_latest_cmd_id(self, device_id: str) -> int:
        """Lay id lenh web moi nhat de khong phat lai lenh cu khi khoi dong."""
        try:
            r = (self._client.table("control_commands")
                 .select("id")
                 .eq("device_id", device_id)
                 .order("id", desc=True)
                 .limit(1)
                 .execute())
            return int(r.data[0]["id"]) if r.data else 0
        except Exception as exc:
            logger.warning("get_latest_cmd_id loi: %s", exc)
            return 0

    def poll_web_commands(self, device_id: str, after_id: int) -> list[dict]:
        """Lay danh sach lenh web moi (source != mqtt) sau after_id."""
        try:
            r = (self._client.table("control_commands")
                 .select("id, motor, fan, mode, target_rpm")
                 .eq("device_id", device_id)
                 .neq("source", "mqtt")
                 .gt("id", after_id)
                 .order("id", desc=False)
                 .limit(10)
                 .execute())
            return r.data or []
        except Exception as exc:
            logger.warning("poll_web_commands loi: %s", exc)
            return []
