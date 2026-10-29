"""
FlowSync — Core Logic
=====================
Junction state management, EV tracking, pre-clearing algorithm,
warning lifecycle, and the deterministic what-if tool.

This is the brain of the system. The Flask server (server.py)
imports this and calls its methods. The MQTT messages feed data
in, and the core decides what to do.

Think of it as the dispatcher in a traffic control center:
it watches all the screens, notices when something needs
attention, and makes calls to the field (junctions).
"""

import time
import json
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from enum import Enum

# ─── Logging ─────────────────────────────────────────────────
audit = logging.getLogger("flowsync.audit")


class Role(Enum):
    PUBLIC = "public"
    OPERATOR = "operator"


# ─── Data Models ─────────────────────────────────────────────
# These mirror what the hardware is actually reporting.

@dataclass
class JunctionState:
    junction_id: str
    phase: str = "EW_GREEN"
    mode: str = "adaptive"          # "adaptive" or "fixed_fallback"
    sensor_ok: bool = True
    east_sensor_ok: bool = True
    west_sensor_ok: bool = True
    queues: dict = field(default_factory=lambda: {"east": 0, "west": 0})
    last_update: float = 0.0
    preclear_active: bool = False
    uptime: int = 0

    def to_public(self):
        """What the public sees — no internal details."""
        return {
            "junction_id": self.junction_id,
            "phase": self.phase,
            "mode": self.mode,
            "sensor_ok": self.sensor_ok,
        }

    def to_operator(self):
        """Operators get the full picture."""
        return asdict(self)


@dataclass
class EVState:
    token: str = ""
    nearest: str = "none"
    heading: str = "eastbound"
    route: list = field(default_factory=lambda: ["J1", "J2", "J3"])
    last_seen: float = 0.0
    active: bool = False
    speed_mps: float = 10.0
    etas: dict = field(default_factory=dict)

    def to_operator(self):
        return {
            "token": self.token,
            "nearest": self.nearest,
            "heading": self.heading,
            "route": self.route,
            "active": self.active,
            "speed_kmh": round(self.speed_mps * 3.6, 1),
            "etas": self.etas,
            "last_seen_ago": round(time.time() - self.last_seen, 1) if self.last_seen > 0 else None,
        }


@dataclass
class Warning:
    junction_id: str
    approach: str
    message: str
    active: bool = True
    created_at: float = 0.0

    def to_public(self):
        """Public warnings: approach + message only. No EV identity."""
        return {
            "junction_id": self.junction_id,
            "approach": self.approach,
            "message": self.message,
            "active": self.active,
        }


@dataclass
class Incident:
    junction_id: str
    description: str
    reported_at: float = 0.0
    resolved: bool = False
    reported_by: str = "operator"


# ─── Core System ─────────────────────────────────────────────

class FlowSyncCore:
    """
    Central coordinator for the FlowSync system.
    Manages junction states, EV tracking, pre-clearing,
    warnings, and the what-if tool.
    """

    def __init__(self, mqtt_publish_fn=None):
        """
        Args:
            mqtt_publish_fn: A function(topic, payload_dict) that publishes
                            to MQTT. Injected by server.py so we don't
                            couple to a specific MQTT library.
        """
        self.junctions = {
            "J1": JunctionState("J1"),
            "J2": JunctionState("J2"),
            "J3": JunctionState("J3"),
        }
        self.ev = EVState()
        self.warnings: list[Warning] = []
        self.incidents: list[Incident] = []
        self.audit_log: list[dict] = []

        # Pre-clearing state
        self.preclear_cooldown: dict[str, float] = {}
        self.COOLDOWN_S = 30.0
        self.STALE_THRESHOLD_S = 5.0
        self.DISCHARGE_TIME_PER_CAR = 2.5
        self.PRECLEAR_MARGIN_S = 5.0

        # SSE subscribers (functions to call with updates)
        self.sse_subscribers: list = []

        # MQTT publish function
        self._mqtt_pub = mqtt_publish_fn

    def set_mqtt_publish(self, fn):
        self._mqtt_pub = fn

    def _publish(self, topic: str, data: dict):
        if self._mqtt_pub:
            self._mqtt_pub(topic, data)

    def _audit(self, action: str, detail: str, user: str = "system"):
        entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user": user,
            "action": action,
            "detail": detail,
        }
        self.audit_log.append(entry)
        if len(self.audit_log) > 500:
            self.audit_log = self.audit_log[-500:]
        audit.info(f"{user} | {action} | {detail}")

    def _notify_sse(self, event_type: str, data: dict):
        """Push an update to all SSE subscribers."""
        for callback in self.sse_subscribers:
            try:
                callback(event_type, data)
            except Exception:
                pass

    # ─── Data Ingestion (called by MQTT handler) ─────────────

    def update_queue(self, junction_id: str, data: dict):
        """Called when a junction reports its IR sensor readings."""
        if junction_id not in self.junctions:
            return
        j = self.junctions[junction_id]
        j.queues["east"] = data.get("east", 0)
        j.queues["west"] = data.get("west", 0)
        j.east_sensor_ok = data.get("east_ok", True)
        j.west_sensor_ok = data.get("west_ok", True)
        j.sensor_ok = j.east_sensor_ok and j.west_sensor_ok
        j.last_update = time.time()

        self._notify_sse("queue_update", {
            "junction_id": junction_id,
            "queues": j.queues,
            "sensor_ok": j.sensor_ok,
        })

    def update_status(self, junction_id: str, data: dict):
        """Called when a junction reports its overall status."""
        if junction_id not in self.junctions:
            return
        j = self.junctions[junction_id]
        j.mode = data.get("mode", j.mode)
        j.phase = data.get("phase", j.phase)
        j.sensor_ok = data.get("sensor_ok", j.sensor_ok)
        j.east_sensor_ok = data.get("east_sensor_ok", j.east_sensor_ok)
        j.west_sensor_ok = data.get("west_sensor_ok", j.west_sensor_ok)
        j.uptime = data.get("uptime", j.uptime)
        j.last_update = time.time()

        # If a sensor just failed, that's worth logging
        if not j.sensor_ok:
            self._audit("sensor_fault", f"{junction_id} sensor issue detected", "system")

        self._notify_sse("status_update", {
            "junction_id": junction_id,
            "phase": j.phase,
            "mode": j.mode,
            "sensor_ok": j.sensor_ok,
        })

    def update_ev_position(self, data: dict):
        """
        Called when the ambulance beacon publishes a position.
        This is where the pre-clearing magic happens.
        """
        self.ev.token = data.get("token", self.ev.token)
        self.ev.nearest = data.get("nearest", self.ev.nearest)
        self.ev.heading = data.get("heading", self.ev.heading)
        self.ev.active = data.get("active", False)
        self.ev.speed_mps = data.get("speed_mps", self.ev.speed_mps)
        self.ev.etas = data.get("etas", {})
        self.ev.last_seen = time.time()

        if self.ev.active:
            self._process_ev()
        else:
            self._cancel_all_warnings("Emergency vehicle no longer active")

        self._notify_sse("ev_update", self.ev.to_operator())

    # ─── Pre-Clearing Algorithm ──────────────────────────────

    def _process_ev(self):
        """
        The core pre-clearing logic. For each junction ahead of
        the ambulance, figure out if we need to start flushing
        the queue now so it's clear by the time it arrives.
        """
        now = time.time()

        # Stale check: if we haven't heard from the beacon in a while,
        # the data is unreliable. Cancel everything.
        if now - self.ev.last_seen > self.STALE_THRESHOLD_S:
            self._cancel_all_warnings("EV location data stale — warning expired")
            return

        nearest = self.ev.nearest
        if nearest == "none" or nearest == "past":
            self._cancel_all_warnings("Emergency vehicle has passed")
            return

        route = self.ev.route
        if nearest not in route:
            return

        idx = route.index(nearest)
        upcoming = route[idx:]
        passed = route[:idx]

        # Cancel warnings for junctions we've passed
        for jid in passed:
            self._cancel_warning(jid, "Emergency vehicle has passed. Resume normal driving.")

        # Process each upcoming junction
        for jid in upcoming:
            eta = self.ev.etas.get(jid, None)
            if eta is None or eta <= 0:
                eta = 0.5  # we're basically there

            j = self.junctions.get(jid)
            if not j:
                continue

            queue_size = max(j.queues.values()) if j.queues else 0
            discharge_needed = queue_size * self.DISCHARGE_TIME_PER_CAR + self.PRECLEAR_MARGIN_S

            # Check cooldown
            if jid in self.preclear_cooldown:
                if now - self.preclear_cooldown[jid] < self.COOLDOWN_S:
                    continue

            # Pre-clear: if ETA is within the discharge window, start now
            if eta <= discharge_needed and not j.preclear_active:
                duration_ms = int(discharge_needed * 1000)
                duration_ms = max(10000, min(duration_ms, 60000))

                self._publish(f"flowsync/{jid}/phase", {
                    "phase": "EW_GREEN",
                    "duration": duration_ms,
                })
                j.preclear_active = True
                self.preclear_cooldown[jid] = now

                self._audit("preclear",
                            f"{jid}: queue={queue_size}, ETA={eta:.1f}s, "
                            f"discharge={discharge_needed:.1f}s, green={duration_ms}ms")

                # Send feedback to the ambulance
                self._publish("flowsync/ev/feedback",
                              {"junction": jid, "status": "preclearing",
                               "msg": f"{jid}: clearing queue ({queue_size} vehicles)"})

            # Full preemption when very close
            elif eta <= 10 and j.preclear_active:
                self._publish(f"flowsync/{jid}/phase", {
                    "phase": "EW_GREEN",
                    "duration": 20000,
                })
                self._publish("flowsync/ev/feedback",
                              {"junction": jid, "status": "green_secured",
                               "msg": f"{jid}: green secured for your approach"})
                self._audit("preempt", f"{jid}: full preemption, ETA={eta:.1f}s")

            # Issue/update warning for this junction's approaches
            self._issue_warning(jid, eta)

        # Notify SSE about pre-clear status
        self._notify_sse("preclear_update", {
            "active_junctions": [jid for jid, j in self.junctions.items() if j.preclear_active],
        })

    def _issue_warning(self, junction_id: str, eta: float):
        """
        Issue or update a warning. Note the careful wording:
        we NEVER tell drivers to run red lights or make unsafe
        manoeuvres. We just ask them to make way when safe.
        """
        eta_rounded = max(1, round(eta))
        direction = "west" if self.ev.heading == "eastbound" else "east"

        msg = (f"Emergency vehicle approaching from the {direction}, "
               f"approximately {eta_rounded}s away. "
               f"Please make way when safe and lawful. "
               f"Do not follow the emergency vehicle.")

        # Update existing warning or create new
        existing = None
        for w in self.warnings:
            if w.junction_id == junction_id and w.active:
                existing = w
                break

        if existing:
            existing.message = msg
        else:
            w = Warning(junction_id, direction, msg, True, time.time())
            self.warnings.append(w)
            self._publish(f"flowsync/{junction_id}/warning",
                          {"msg": msg, "active": True})
            self._audit("warning_issued",
                        f"{junction_id}/{direction}: ETA {eta_rounded}s")

        self._notify_sse("warning_update", {
            "junction_id": junction_id,
            "approach": direction,
            "message": msg,
            "active": True,
            "eta": eta_rounded,
        })

    def _cancel_warning(self, junction_id: str, reason: str):
        for w in self.warnings:
            if w.junction_id == junction_id and w.active:
                w.active = False
                self._publish(f"flowsync/{junction_id}/warning",
                              {"msg": reason, "active": False})
                self._audit("warning_cancelled", f"{junction_id}: {reason}")

                self._notify_sse("warning_update", {
                    "junction_id": junction_id,
                    "message": reason,
                    "active": False,
                })

        # Release pre-clear
        j = self.junctions.get(junction_id)
        if j:
            j.preclear_active = False

    def _cancel_all_warnings(self, reason: str):
        for jid in ["J1", "J2", "J3"]:
            self._cancel_warning(jid, reason)

    # ─── LLM Tool Functions ─────────────────────────────────
    # These are called by the LLM tool orchestration layer.
    # Each one returns a dict that the LLM uses to form its response.

    def tool_get_junction_state(self, junction_id: str, role: Role) -> dict:
        if junction_id not in self.junctions:
            return {"error": f"No junction called '{junction_id}'"}
        j = self.junctions[junction_id]
        self._audit("tool_call", f"get_junction_state({junction_id})", role.value)
        return j.to_operator() if role == Role.OPERATOR else j.to_public()

    def tool_get_queues(self, role: Role) -> dict:
        self._audit("tool_call", "get_queues()", role.value)
        return {jid: j.queues for jid, j in self.junctions.items()}

    def tool_get_ev_status(self, role: Role) -> dict:
        if role != Role.OPERATOR:
            self._audit("access_denied", "get_ev_status() by public user", role.value)
            return {"error": "Emergency vehicle tracking is restricted to operators"}
        self._audit("tool_call", "get_ev_status()", role.value)
        return self.ev.to_operator()

    def tool_report_incident(self, junction_id: str, description: str, role: Role) -> dict:
        if role != Role.OPERATOR:
            self._audit("access_denied", "report_incident() by public user", role.value)
            return {"error": "Incident reporting is restricted to operators"}

        incident = Incident(junction_id, description, time.time(), False, role.value)
        self.incidents.append(incident)
        self._audit("incident_reported", f"{junction_id}: {description}", role.value)

        self._notify_sse("incident", {
            "junction_id": junction_id,
            "description": description,
        })

        return {
            "status": "reported",
            "junction": junction_id,
            "description": description,
        }

    def tool_propose_phase_change(self, junction_id: str, phase: str,
                                   duration_ms: int, role: Role) -> dict:
        if role != Role.OPERATOR:
            self._audit("access_denied", "propose_phase_change() by public user", role.value)
            return {"error": "Phase changes are restricted to operators"}

        # Backend validation
        if phase not in ("EW_GREEN", "NS_GREEN"):
            return {"error": f"Unknown phase: {phase}. Use EW_GREEN or NS_GREEN."}
        if duration_ms < 10000:
            return {"error": "Duration must be at least 10000ms (10s minimum green)"}
        if duration_ms > 60000:
            return {"error": "Duration cannot exceed 60000ms (60s maximum)"}
        if junction_id not in self.junctions:
            return {"error": f"No junction called '{junction_id}'"}

        # Forward to the junction
        self._publish(f"flowsync/{junction_id}/phase", {
            "phase": phase,
            "duration": duration_ms,
        })

        self._audit("phase_change_proposed",
                     f"{junction_id} → {phase} for {duration_ms}ms", role.value)

        return {
            "status": "accepted",
            "junction": junction_id,
            "phase": phase,
            "duration_ms": duration_ms,
            "note": "Sent to junction. MCU will validate and transition safely.",
        }

    def tool_what_if(self, junction_id: str, action: str, role: Role) -> dict:
        """
        Deterministic what-if simulation. Returns hard numbers that
        the LLM can explain — the LLM doesn't compute these itself.

        This is how we keep the LLM honest: it uses numbers from
        a trusted tool instead of making them up.
        """
        if junction_id not in self.junctions:
            return {"error": f"No junction called '{junction_id}'"}

        j = self.junctions[junction_id]
        total_queue = sum(j.queues.values())
        self._audit("tool_call", f"what_if({junction_id}, {action})", role.value)

        if action == "preempt":
            # How much disruption would a preemption cause?
            cars_delayed = total_queue
            delay_per_car = 15 + (total_queue * 2)  # gets worse with longer queues
            total_disruption = cars_delayed * delay_per_car

            # Is the EV actually nearby?
            ev_eta = self.ev.etas.get(junction_id)
            ev_benefit = ev_eta * 0.7 if ev_eta else 0  # rough time saved

            recommendation = "preempt" if total_disruption < 200 else "consider_waiting"
            reason = ("Queue is short enough that preemption won't cause much "
                      "disruption to cross traffic." if recommendation == "preempt"
                      else "Heavy cross-traffic queue — preemption would delay "
                      f"about {cars_delayed} vehicles by ~{delay_per_car}s each. "
                      "Consider waiting for the current phase to end naturally.")

            return {
                "junction": junction_id,
                "action": action,
                "current_queue": total_queue,
                "cars_affected": cars_delayed,
                "est_delay_per_car_s": delay_per_car,
                "total_disruption_car_seconds": total_disruption,
                "ev_eta_s": ev_eta,
                "ev_time_saved_s": round(ev_benefit, 1) if ev_benefit else None,
                "recommendation": recommendation,
                "reasoning": reason,
            }

        elif action == "extend_green":
            current_phase = j.phase
            benefit = total_queue * 2.5  # seconds of queue discharge
            return {
                "junction": junction_id,
                "action": action,
                "current_phase": current_phase,
                "current_queue": total_queue,
                "discharge_time_s": round(benefit, 1),
                "recommendation": "extend" if total_queue > 2 else "no_need",
                "reasoning": (f"There are {total_queue} vehicles queued. "
                              f"Extending green by {benefit:.0f}s would clear them."
                              if total_queue > 2 else
                              "Queue is short — no need to extend."),
            }

        return {"error": f"Unknown action: '{action}'. Try 'preempt' or 'extend_green'."}

    def tool_suggest_alt_route(self, blocked_junction: str, role: Role) -> dict:
        if blocked_junction not in self.junctions:
            return {"error": f"No junction called '{blocked_junction}'"}

        self._audit("tool_call", f"suggest_alt_route({blocked_junction})", role.value)

        # In a real system this would query a routing engine.
        # For the prototype, we give a sensible static suggestion.
        suggestions = {
            "J1": "Reroute via the South bypass road, avoiding J1 entirely. "
                  "Adds approximately 45 seconds to travel time.",
            "J2": "Reroute via the North service road between J1 and J3, "
                  "bypassing J2. Adds approximately 60 seconds.",
            "J3": "Reroute via the South ring road from J2 to the eastern "
                  "exit. Adds approximately 50 seconds.",
        }

        return {
            "blocked_junction": blocked_junction,
            "suggestion": suggestions.get(blocked_junction, "No alternative route available."),
            "estimated_extra_time_s": {"J1": 45, "J2": 60, "J3": 50}.get(blocked_junction, 0),
        }

    # ─── Convenience ──────────────────────────────────────────

    def get_all_junctions(self, role: Role) -> dict:
        if role == Role.OPERATOR:
            return {jid: j.to_operator() for jid, j in self.junctions.items()}
        return {jid: j.to_public() for jid, j in self.junctions.items()}

    def get_active_warnings(self) -> list:
        return [w.to_public() for w in self.warnings if w.active]

    def get_audit_log(self, count: int = 50) -> list:
        return self.audit_log[-count:]
