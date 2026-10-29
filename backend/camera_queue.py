#!/usr/bin/env python3
"""
FlowSync — Camera Queue Counter (Junction 1)
============================================
Runs on the Arduino UNO Q Linux side (Cortex-A53).
Captures video from a top-down USB webcam connected via USB-C,
monitors defined approach lanes (East & West) over the toy road mat,
and publishes real-time vehicle counts to MQTT: `flowsync/J1/queue`.

Falls back to a simulated queue feed if no physical camera is detected.
"""

import os
import sys
import time
import json
import logging

try:
    import cv2
    import numpy as np
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

try:
    import paho.mqtt.client as paho_mqtt
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("camera_queue")

MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", 1883))
TOPIC = "flowsync/J1/queue"

# Detection sensitivity parameters for toy cars on road mat
MIN_CONTOUR_AREA = 1200   # minimum pixel area to qualify as a toy car
MAX_CONTOUR_AREA = 25000


def create_mqtt_client():
    if not MQTT_AVAILABLE:
        return None
    try:
        client = paho_mqtt.Client(paho_mqtt.CallbackAPIVersion.VERSION2, client_id="flowsync_camera_j1")
    except (AttributeError, TypeError):
        client = paho_mqtt.Client(client_id="flowsync_camera_j1")
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        logger.info(f"Connected to MQTT broker at {MQTT_BROKER}:{MQTT_PORT}")
        return client
    except Exception as e:
        logger.warning(f"Could not connect to MQTT: {e}. Queue counts will print to terminal only.")
        return None


def run_camera_detector(client):
    logger.info("Initializing USB camera (/dev/video0 or index 0)...")
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        logger.warning("No physical camera detected. Switching to demonstration mode.")
        run_fallback_sim(client)
        return

    # Set 640x480 resolution for low CPU footprint on QRB2210
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    # Background subtractor to isolate vehicles on the road mat
    fgbg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=False)

    last_pub = 0

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            h, w = frame.shape[:2]

            # Define Regions of Interest (ROI) for East and West approaches
            # Left half of frame = West Approach, Right half = East Approach
            roi_west = frame[:, :int(w * 0.45)]
            roi_east = frame[:, int(w * 0.55):]

            # Foreground mask
            fgmask = fgbg.apply(frame)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
            fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_CLOSE, kernel)

            # Count contours in West approach
            mask_west = fgmask[:, :int(w * 0.45)]
            cnts_west, _ = cv2.findContours(mask_west, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            q_west = sum(1 for c in cnts_west if MIN_CONTOUR_AREA < cv2.contourArea(c) < MAX_CONTOUR_AREA)

            # Count contours in East approach
            mask_east = fgmask[:, int(w * 0.55):]
            cnts_east, _ = cv2.findContours(mask_east, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            q_east = sum(1 for c in cnts_east if MIN_CONTOUR_AREA < cv2.contourArea(c) < MAX_CONTOUR_AREA)

            now = time.time()
            if now - last_pub >= 2.0:
                payload = {
                    "east": min(q_east, 5),
                    "west": min(q_west, 5),
                    "east_ok": True,
                    "west_ok": True,
                    "source": "camera_cv"
                }
                if client:
                    client.publish(TOPIC, json.dumps(payload))
                logger.info(f"Published J1 Camera Queues: East={payload['east']} | West={payload['west']}")
                last_pub = now

            time.sleep(0.05)

    except KeyboardInterrupt:
        logger.info("Stopping camera stream.")
    finally:
        cap.release()


def run_fallback_sim(client):
    logger.info("Running Camera Fallback Simulator (periodically sends realistic queue counts)")
    import random
    while True:
        payload = {
            "east": random.choice([0, 1, 1, 2, 3]),
            "west": random.choice([0, 1, 2]),
            "east_ok": True,
            "west_ok": True,
            "source": "camera_sim"
        }
        if client:
            client.publish(TOPIC, json.dumps(payload))
        logger.info(f"[SIMULATED CAMERA] J1 Queues: East={payload['east']} | West={payload['west']}")
        time.sleep(3.0)


def main():
    logger.info("FlowSync USB Camera Detection Daemon Starting...")
    client = create_mqtt_client()
    if OPENCV_AVAILABLE:
        run_camera_detector(client)
    else:
        logger.warning("OpenCV is not installed. Running in simulation mode.")
        run_fallback_sim(client)


if __name__ == "__main__":
    main()
