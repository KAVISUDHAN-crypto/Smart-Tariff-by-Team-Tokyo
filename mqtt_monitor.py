#!/usr/bin/env python3
"""Prints every ESP32 reading the Pi receives. Run it to check the wiring before the dashboard."""
import json, time
import paho.mqtt.client as mqtt

def on_connect(c, u, f, rc, p=None):
    print("connected to broker, waiting for ESP32...")
    c.subscribe("home/load/+/telemetry")

def on_message(c, u, m):
    try:
        n = m.topic.split("/")[2]
        d = json.loads(m.payload)
        v, i = float(d["voltage"]), float(d["current"])
        print(f"{time.strftime('%H:%M:%S')}  load {n}:  {v:6.1f} V  {i:6.3f} A  {v*i:7.1f} W  relay={'ON' if d.get('relay') else 'OFF'}")
    except Exception as e:
        print("bad message on", m.topic, m.payload, e)

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
c.on_connect, c.on_message = on_connect, on_message
c.connect("localhost", 1883)
c.loop_forever()
