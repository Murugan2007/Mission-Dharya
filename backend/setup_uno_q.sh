#!/bin/bash
# ============================================================
#  FlowSync — Arduino UNO Q (Linux Side) Setup Script
#  Target: Qualcomm QRB2210 Cortex-A53, Debian Linux
# ============================================================

set -e

echo "============================================================"
echo "  Setting up FlowSync Backend on Arduino UNO Q (Linux side)"
echo "============================================================"

# 1. System packages & MQTT Broker
echo "[1/4] Installing Mosquitto MQTT Broker & System Tools..."
sudo apt-get update
sudo apt-get install -y mosquitto mosquitto-clients python3-pip python3-venv v4l-utils

# Enable and start Mosquitto broker
sudo systemctl enable mosquitto
sudo systemctl start mosquitto

# Allow external connections to Mosquitto (for ESP32s on Wi-Fi)
MOSQ_CONF="/etc/mosquitto/conf.d/flowsync.conf"
if [ ! -f "$MOSQ_CONF" ]; then
    echo -e "listener 1883\nallow_anonymous true" | sudo tee "$MOSQ_CONF"
    sudo systemctl restart mosquitto
    echo "[✓] Configured Mosquitto for LAN access on port 1883"
fi

# 2. Python Virtual Environment
echo "[2/4] Setting up Python virtual environment..."
cd "$(dirname "$0")"
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 3. Create Audit Log Directory
echo "[3/4] Creating audit log storage..."
sudo mkdir -p /var/log/flowsync
sudo chown -R $USER:$USER /var/log/flowsync
sudo touch /var/log/flowsync/audit.log

# 4. Generate .env Configuration
echo "[4/4] Generating configuration (.env)..."
if [ ! -f ".env" ]; then
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > .env << EOF
FLASK_SECRET_KEY=${SECRET_KEY}
MQTT_BROKER=localhost
MQTT_PORT=1883
# Optional: Set your Gemini API key to activate cloud reasoning
# GEMINI_API_KEY=AIzaSy...
EOF
    echo "[✓] Created .env configuration file"
fi

echo ""
echo "============================================================"
echo "  FlowSync Backend setup complete!"
echo "  To launch the backend:"
echo "    source venv/bin/activate"
echo "    python server.py"
echo "  Access dashboards:"
echo "    Public:   http://<UNO_Q_IP>:5000/"
echo "    Operator: http://<UNO_Q_IP>:5000/login"
echo "============================================================"
