"""
mqtt_service.py  —  MQTT Service  [v6]
================================================================================
Chi chua MQTTService va topic constants.
Firmware import va goi publish_* de day du lieu len MQTT cho Unity.

Supabase duoc xu ly rieng boi supabase_service.py.

SU DUNG:
    from mqtt_service import MQTTService, TOPIC_CONTROL_CMD, TOPIC_CYBER_RESULT
================================================================================
"""

import json
import logging
import time

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)

# ──────────────────────── TOPIC CONSTANTS ────────────────────────────────────
TOPIC_CONTROL_CMD      = "cps/control/cmd"       # nhan lenh tu Unity / Web
TOPIC_RAW_SENSOR       = "cps/raw/sensor"         # sensor realtime
TOPIC_CONTROL_STATE    = "cps/control/state"      # trang thai motor/fan/mode
TOPIC_EVENT_LOG        = "cps/event/log"          # su kien
TOPIC_HEARTBEAT        = "cps/system/heartbeat"   # song/chet firmware
TOPIC_CYBER_RESULT     = "cps/cyber/result"       # ket qua suc khoe day du -> Unity + Supabase


# ──────────────────────── MQTTService ────────────────────────────────────────
class MQTTService:
    """
    Quan ly ket noi MQTT va publish du lieu len broker cho Unity doc.
    Firmware goi connect() mot lan, sau do goi cac publish_*() bat cu luc nao.
    """

    TOPIC_CONTROL_CMD = TOPIC_CONTROL_CMD

    def __init__(self, broker_host: str, broker_port: int,
                 device_id: str, on_command=None):
        self.broker_host = broker_host
        self.broker_port = broker_port
        self.device_id   = device_id
        self.on_command  = on_command
        self._connected  = False

        self.client = mqtt.Client(client_id="pi_" + device_id, clean_session=True)
        self.client.on_connect    = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message    = self._on_message

    def connect(self) -> bool:
        try:
            self.client.connect(self.broker_host, self.broker_port, keepalive=60)
            self.client.loop_start()
            for _ in range(50):
                if self._connected:
                    return True
                time.sleep(0.1)
            logger.error("MQTT: het thoi gian ket noi %s:%s", self.broker_host, self.broker_port)
            return False
        except Exception as exc:
            logger.error("MQTT connect error: %s", exc)
            return False

    def disconnect(self):
        self.client.loop_stop()
        self.client.disconnect()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self._connected = True
            client.subscribe(TOPIC_CONTROL_CMD, qos=1)
            logger.info("MQTT connected [%s:%s] | sub: %s",
                        self.broker_host, self.broker_port, TOPIC_CONTROL_CMD)
        else:
            logger.error("MQTT ket noi that bai rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._connected = False
        if rc != 0:
            logger.warning("MQTT mat ket noi bat ngo (rc=%s)", rc)

    def _on_message(self, client, userdata, msg):
        if msg.topic != TOPIC_CONTROL_CMD:
            return
        try:
            payload = json.loads(msg.payload.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            logger.warning("MQTT: payload khong phai JSON tren %s", msg.topic)
            return
        if self.on_command:
            try:
                self.on_command(msg.topic, payload)
            except Exception as exc:
                logger.error("on_command loi: %s", exc)

    def _publish(self, topic: str, payload: dict,
                 qos: int = 0, retain: bool = False) -> bool:
        if not self._connected:
            logger.warning("MQTT chua connected, bo qua topic=%s", topic)
            return False
        try:
            self.client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
            return True
        except Exception as exc:
            logger.error("MQTT publish loi [%s]: %s", topic, exc)
            return False

    # ── Publish methods ──────────────────────────────────────────────────────

    def publish_heartbeat(self, uptime_s: int, status: str = "online") -> bool:
        return self._publish(TOPIC_HEARTBEAT, {
            "device_id": self.device_id, "status": status,
            "uptime_s": int(uptime_s), "ts": time.time_ns(),
        }, qos=1, retain=True)

    def publish_control_state(self, motor_on: bool,
                              mode: str = "manual", fan: bool = False,
                              ts: int = None) -> bool:
        return self._publish(TOPIC_CONTROL_STATE, {
            "device_id": self.device_id, "motor": bool(motor_on),
            "fan": bool(fan), "mode": mode,
            "ts": ts if ts is not None else time.time_ns(),
        }, qos=1, retain=True)

    def publish_sensor(self, rpm: float, temperature=None, target_rpm=None,
                       motor_on: bool = False, mode: str = "manual",
                       status_rule=None, health_status=None,
                       rpm_error=None, wave_rpm=None, is_stalled=None) -> bool:
        p = {
            "device_id": self.device_id, "rpm": round(float(rpm), 1),
            "motor_on": bool(motor_on), "mode": mode, "ts": time.time_ns(),
        }
        if temperature   is not None: p["temperature"]   = round(float(temperature), 2)
        if target_rpm    is not None: p["target_rpm"]    = int(target_rpm)
        if status_rule   is not None: p["status_rule"]   = status_rule
        if health_status is not None: p["health_status"] = health_status
        if rpm_error     is not None: p["rpm_error"]     = round(float(rpm_error), 2)
        if wave_rpm      is not None: p["wave_rpm"]      = round(float(wave_rpm), 1)
        if is_stalled    is not None: p["is_stalled"]    = bool(is_stalled)
        return self._publish(TOPIC_RAW_SENSOR, p, qos=0, retain=False)

    def publish_event(self, event_name: str, severity: str = "info") -> bool:
        return self._publish(TOPIC_EVENT_LOG, {
            "device_id": self.device_id, "event_name": event_name,
            "severity": severity, "ts": time.time_ns(),
        }, qos=1, retain=False)

    def publish_cyber_result(self, device_id: str, res: dict,
                             avg_temperature=None, avg_rpm=None,
                             overheat_count: int = 0,
                             fan_activation_count: int = 0,
                             total_hours=None,
                             today_runtime_sec=None,
                             health_status: str = None) -> bool:
        """
        cps/cyber/result — ket qua suc khoe day du.
        Unity subscribe de hien thi dashboard / doi mau canh bao.
        retain=True: Unity nhan ngay khi ket noi, khong phai doi lan publish tiep.

        Payload:
          { device_id, health_score, wear_level, failure_probability,
            remaining_life_hours, health_status,
            avg_temperature, avg_rpm, overheat_count,
            fan_activation_count, total_hours, today_runtime_sec, ts }
        """
        p = {
            "device_id":            device_id,
            "health_score":         round(float(res["health_score"]),        2),
            "wear_level":           round(float(res["wear_level"]),           4),
            "failure_probability":  round(float(res["failure_probability"]),  4),
            "remaining_life_hours": round(float(res["remaining_life_hours"]), 2),
            "overheat_count":       int(overheat_count),
            "fan_activation_count": int(fan_activation_count),
            "ts":                   time.time(),
        }
        if health_status     is not None: p["health_status"]    = health_status
        if avg_temperature   is not None: p["avg_temperature"]  = round(float(avg_temperature), 2)
        if avg_rpm           is not None: p["avg_rpm"]          = int(round(avg_rpm))
        if total_hours       is not None: p["total_hours"]      = round(float(total_hours), 4)
        if today_runtime_sec is not None: p["today_runtime_sec"]= int(today_runtime_sec)
        return self._publish(TOPIC_CYBER_RESULT, p, qos=1, retain=True)
