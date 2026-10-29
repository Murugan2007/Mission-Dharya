"""
FlowSync — Main Server
======================
Flask web server + MQTT client + SSE streaming.
This is the entry point for the backend. Run it on the
UNO Q's Linux side (or on your laptop for testing).

    python server.py

Endpoints:
    Public:     /                   Dashboard (no login)
                /api/warnings       Active warnings
                /api/stream         SSE stream (public-safe data)

    Operator:   /login              Login page
                /operator           Full dashboard
                /api/junctions      All junction data
                /api/ev/status      EV tracking
                /api/llm/ask        LLM chat
                /api/stream/op      SSE stream (full data)
                ... and more

Every restricted action is checked and logged. No exceptions.
"""

import os
import sys
import json
import time
import queue
import logging
import secrets
import threading
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request, jsonify,
                   session, redirect, url_for, Response, stream_with_context)
from dotenv import load_dotenv

# Our modules
from core import FlowSyncCore, Role
from llm_tools import create_llm

# ─── Configuration ───────────────────────────────────────────
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))

MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))

# Demo credentials (hardcoded for the prototype — obviously
# you'd use a proper auth system in production)
OPERATOR_USER = "operator"
OPERATOR_PASS = "flowsync2026"

# ─── Logging ─────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("flowsync_audit.log"),
    ]
)
logger = logging.getLogger("flowsync")

# ─── SSE Management ─────────────────────────────────────────
# Each connected client gets a queue. When something happens,
# we push the event to all queues. Simple fan-out.

class SSEManager:
    def __init__(self):
        self.public_queues: list[queue.Queue] = []
        self.operator_queues: list[queue.Queue] = []
        self._lock = threading.Lock()

    def add_public(self):
        q = queue.Queue(maxsize=50)
        with self._lock:
            self.public_queues.append(q)
        return q

    def add_operator(self):
        q = queue.Queue(maxsize=50)
        with self._lock:
            self.operator_queues.append(q)
        return q

    def remove(self, q):
        with self._lock:
            if q in self.public_queues:
                self.public_queues.remove(q)
            if q in self.operator_queues:
                self.operator_queues.remove(q)

    def push(self, event_type: str, data: dict, operator_only: bool = False):
        msg = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        with self._lock:
            if not operator_only:
                for q in self.public_queues[:]:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass
            for q in self.operator_queues[:]:
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

sse = SSEManager()

# ─── MQTT Setup ──────────────────────────────────────────────
mqtt_client = None

def mqtt_publish(topic: str, data: dict):
    """Publish a message to MQTT. Called by FlowSyncCore."""
    global mqtt_client
    if mqtt_client and mqtt_client.is_connected():
        mqtt_client.publish(topic, json.dumps(data))
        logger.debug(f"MQTT → {topic}: {data}")

# Initialize the core system
core = FlowSyncCore(mqtt_publish_fn=mqtt_publish)

def sse_callback(event_type, data):
    """Called by core when something changes."""
    operator_only = event_type in ("ev_update", "preclear_update")
    sse.push(event_type, data, operator_only=operator_only)

core.sse_subscribers.append(sse_callback)

def setup_mqtt():
    """Connect to the MQTT broker and subscribe to topics."""
    global mqtt_client
    try:
        import paho.mqtt.client as paho_mqtt
    except ImportError:
        logger.warning("paho-mqtt not installed. MQTT disabled. "
                       "Install with: pip install paho-mqtt")
        return

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            logger.info("MQTT connected to broker")
            client.subscribe("flowsync/+/queue")
            client.subscribe("flowsync/+/status")
            client.subscribe("flowsync/ev/position")
        else:
            logger.error(f"MQTT connection failed: rc={rc}")

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            topic = msg.topic

            if "/queue" in topic:
                jid = topic.split("/")[1]
                core.update_queue(jid, data)
            elif "/status" in topic:
                jid = topic.split("/")[1]
                core.update_status(jid, data)
            elif "ev/position" in topic:
                core.update_ev_position(data)

        except Exception as e:
            logger.error(f"MQTT message error: {e}")

    try:
        mqtt_client = paho_mqtt.Client(
            paho_mqtt.CallbackAPIVersion.VERSION2,
            client_id="flowsync_backend"
        )
    except TypeError:
        # Older paho-mqtt without CallbackAPIVersion
        mqtt_client = paho_mqtt.Client(client_id="flowsync_backend")

    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start()
        logger.info(f"MQTT connecting to {MQTT_BROKER}:{MQTT_PORT}...")
    except Exception as e:
        logger.warning(f"MQTT broker not available: {e}. Running without MQTT.")
        mqtt_client = None


# ─── Auth Helpers ────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "operator":
            core._audit("access_denied", f"Unauthenticated access to {request.path}",
                        session.get("user", "anonymous"))
            return jsonify({"error": "Authentication required. Log in at /login"}), 403
        return f(*args, **kwargs)
    return decorated


# ─── Public Routes ───────────────────────────────────────────

@app.route("/")
def public_dashboard():
    return render_template("public.html")

@app.route("/api/warnings")
def api_warnings():
    return jsonify(core.get_active_warnings())

@app.route("/api/junctions/public")
def api_junctions_public():
    return jsonify(core.get_all_junctions(Role.PUBLIC))

@app.route("/api/stream")
def api_stream_public():
    """SSE stream for public dashboard. Only safe data."""
    def generate():
        q = sse.add_public()
        try:
            # Send initial state
            yield f"event: init\ndata: {json.dumps({'junctions': core.get_all_junctions(Role.PUBLIC), 'warnings': core.get_active_warnings()})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            sse.remove(q)

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ─── Auth Routes ─────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    data = request.get_json() if request.is_json else request.form
    username = data.get("username", "")
    password = data.get("password", "")

    if username == OPERATOR_USER and password == OPERATOR_PASS:
        session["role"] = "operator"
        session["user"] = username
        core._audit("login_success", f"Operator logged in", username)
        if request.is_json:
            return jsonify({"status": "ok", "redirect": "/operator"})
        return redirect(url_for("operator_dashboard"))
    else:
        core._audit("login_failed", f"Bad credentials for '{username}'", username)
        if request.is_json:
            return jsonify({"error": "Invalid credentials"}), 401
        return render_template("login.html", error="Invalid credentials")

@app.route("/logout")
def logout():
    user = session.get("user", "unknown")
    session.clear()
    core._audit("logout", "Operator logged out", user)
    return redirect("/")


# ─── Operator Routes ────────────────────────────────────────

@app.route("/operator")
@login_required
def operator_dashboard():
    return render_template("operator.html")

@app.route("/api/junctions")
@login_required
def api_junctions():
    return jsonify(core.get_all_junctions(Role.OPERATOR))

@app.route("/api/ev/status")
@login_required
def api_ev_status():
    return jsonify(core.tool_get_ev_status(Role.OPERATOR))

@app.route("/api/junction/<jid>/phase", methods=["POST"])
@login_required
def api_propose_phase(jid):
    data = request.get_json()
    result = core.tool_propose_phase_change(
        jid,
        data.get("phase", "EW_GREEN"),
        data.get("duration_ms", 25000),
        Role.OPERATOR,
    )
    status = 200 if "error" not in result else 400
    return jsonify(result), status

@app.route("/api/incident", methods=["POST"])
@login_required
def api_incident():
    data = request.get_json()
    result = core.tool_report_incident(
        data.get("junction_id", "J1"),
        data.get("description", ""),
        Role.OPERATOR,
    )
    return jsonify(result)

@app.route("/api/audit")
@login_required
def api_audit():
    return jsonify(core.get_audit_log(50))

@app.route("/api/llm/ask", methods=["POST"])
@login_required
def api_llm_ask():
    data = request.get_json()
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Empty question"}), 400

    core._audit("llm_query", question, session.get("user", "operator"))

    llm = create_llm(core, Role.OPERATOR)
    result = llm.ask(question)

    core._audit("llm_response", result.get("response", "")[:200],
                session.get("user", "operator"))
    return jsonify(result)

@app.route("/api/ev/route", methods=["POST"])
@login_required
def api_update_ev_route():
    data = request.get_json()
    new_route = data.get("route", [])
    if not isinstance(new_route, list) or not all(j in core.junctions for j in new_route):
        return jsonify({"error": "Invalid route. Use junction IDs like ['J1', 'J2', 'J3']"}), 400
    core.ev.route = new_route
    core._audit("ev_route_updated", f"New route: {new_route}", session.get("user", "operator"))
    return jsonify({"status": "ok", "route": new_route})

@app.route("/api/stream/op")
@login_required
def api_stream_operator():
    """SSE stream for operator dashboard. Full data including EV tracking."""
    def generate():
        q = sse.add_operator()
        try:
            yield f"event: init\ndata: {json.dumps({'junctions': core.get_all_junctions(Role.OPERATOR), 'warnings': core.get_active_warnings(), 'ev': core.ev.to_operator()})}\n\n"
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield msg
                except queue.Empty:
                    yield ": heartbeat\n\n"
        except GeneratorExit:
            sse.remove(q)

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


# ─── Startup ─────────────────────────────────────────────────

def main():
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║         FlowSync Traffic Intelligence           ║")
    print("║         Backend Server v1.0                     ║")
    print("╚══════════════════════════════════════════════════╝")
    print()
    print(f"  MQTT Broker : {MQTT_BROKER}:{MQTT_PORT}")
    print(f"  Gemini API  : {'configured' if os.environ.get('GEMINI_API_KEY') else 'not set (using mock LLM)'}")
    print(f"  Operator    : {OPERATOR_USER} / {'*' * len(OPERATOR_PASS)}")
    print()

    setup_mqtt()

    print("  Starting web server on http://0.0.0.0:5000")
    print("  Public dashboard : http://localhost:5000/")
    print("  Operator login   : http://localhost:5000/login")
    print()

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
