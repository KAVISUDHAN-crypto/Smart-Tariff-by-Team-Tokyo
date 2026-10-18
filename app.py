
"""WattWise AI - Raspberry Pi Flask backend."""

import csv
import glob
import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import serial
from flask import Flask, jsonify, render_template, request

# --------------------------------------------------
# Configuration
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
SERIAL_PORT = os.getenv("ARDUINO_PORT", "/dev/ttyUSB0")
BAUD_RATE = int(os.getenv("ARDUINO_BAUD", "9600"))
TIMEZONE_NAME = os.getenv("TZ_NAME", "Asia/Kolkata")

try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:
    TIMEZONE = None

LOG_FILE = BASE_DIR / "live_log.csv"
SCHEDULE_FILE = BASE_DIR / "schedule.json"

app = Flask(__name__, template_folder="templates")
TARIFF_RATE = 1.50  # ₹ per kWh — change to your tariff
BASELINE_MULTIPLIER = 1.25  # Estimated 25% higher usage
state_lock = threading.RLock()
serial_lock = threading.Lock()

serial_connection = None
connected_port = None

automation_enabled = False
last_applied_schedule = None

# --------------------------------------------------
# Default schedule
# Load 1 = Light
# Load 2 = Fan
# --------------------------------------------------

DEFAULT_SCHEDULE = [
    {
        "start": "06:00",
        "end": "10:00",
        "load1": False,
        "load2": True,
        "label": "Morning"
    },
    {
        "start": "10:00",
        "end": "16:00",
        "load1": False,
        "load2": False,
        "label": "Day"
    },
    {
        "start": "16:00",
        "end": "18:00",
        "load1": False,
        "load2": True,
        "label": "Evening"
    },
    {
        "start": "18:00",
        "end": "22:00",
        "load1": False,
        "load2": False,
        "label": "Night"
    }
]

# --------------------------------------------------
# Sensor and energy state
# --------------------------------------------------

loads = {
    1: {
        "v": 0.0,
        "i": 0.0,
        "p": 0.0,
        "wh": 0.0,
        "bwh": 0.0,
        "onp": 0.0,
        "relay": False,
        "last": 0.0
    },
    2: {
        "v": 0.0,
        "i": 0.0,
        "p": 0.0,
        "wh": 0.0,
        "bwh": 0.0,
        "onp": 0.0,
        "relay": False,
        "last": 0.0
    }
}

# --------------------------------------------------
# Time helpers
# --------------------------------------------------

def now_local():
    if TIMEZONE:
        return datetime.now(TIMEZONE)
    return datetime.now()


def time_to_minutes(value):
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


# --------------------------------------------------
# Schedule handling
# --------------------------------------------------

def validate_schedule(data):
    if not isinstance(data, list) or len(data) != 4:
        raise ValueError("Exactly four schedule periods are required.")

    result = []

    for row in data:
        if not isinstance(row, dict):
            raise ValueError("Each schedule period must be an object.")

        start = str(row.get("start", ""))
        end = str(row.get("end", ""))

        for value in (start, end):
            if not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d",
                value
            ):
                raise ValueError("Time must use HH:MM format.")

        if start == end:
            raise ValueError("Start and end times cannot be equal.")

        result.append({
            "start": start,
            "end": end,
            "load1": bool(row.get("load1", False)),
            "load2": bool(row.get("load2", False)),
            "label": str(row.get("label", ""))[:60]
        })

    return result


def get_schedule():
    try:
        data = json.loads(
            SCHEDULE_FILE.read_text(encoding="utf-8")
        )
        return validate_schedule(data)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return [row.copy() for row in DEFAULT_SCHEDULE]


def save_schedule(data):
    clean = validate_schedule(data)

    temp_file = SCHEDULE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(clean, indent=2),
        encoding="utf-8"
    )
    temp_file.replace(SCHEDULE_FILE)

    return clean


def period_contains(current_minute, start, end):
    start_minute = time_to_minutes(start)
    end_minute = time_to_minutes(end)

    if start_minute < end_minute:
        return start_minute <= current_minute < end_minute

    # Supports periods crossing midnight.
    return (
        current_minute >= start_minute
        or current_minute < end_minute
    )


def desired_schedule():
    current = now_local()
    current_minute = current.hour * 60 + current.minute

    for row in get_schedule():
        if period_contains(
            current_minute,
            row["start"],
            row["end"]
        ):
            return {
                1: row["load1"],
                2: row["load2"]
            }

        return {1: False, 2: False}


# --------------------------------------------------
# Arduino USB serial
# --------------------------------------------------

def find_arduino_port():
    if os.path.exists(SERIAL_PORT):
        return SERIAL_PORT

    ports = sorted(
        glob.glob("/dev/ttyUSB*")
        + glob.glob("/dev/ttyACM*")
    )

    return ports[0] if ports else None


def serial_worker():
    global serial_connection, connected_port

    while True:
        connection = None

        try:
            port = find_arduino_port()

            if not port:
                time.sleep(2)
                continue

            connection = serial.Serial(
                port,
                BAUD_RATE,
                timeout=1
            )

            # Many Arduino boards reset when serial opens.
            time.sleep(2)
            connection.reset_input_buffer()

            with serial_lock:
                serial_connection = connection
                connected_port = port

            app.logger.info(
                "Arduino connected: %s at %s baud",
                port,
                BAUD_RATE
            )

            while connection.is_open:
                raw = connection.readline()

                if not raw:
                    continue

                line = raw.decode(
                    "utf-8",
                    errors="ignore"
                ).strip()

                if line:
                    parse_arduino_line(line)

        except (serial.SerialException, OSError) as error:
            app.logger.warning(
                "Arduino serial error: %s",
                error
            )

        finally:
            with serial_lock:
                serial_connection = None
                connected_port = None

            if connection:
                try:
                    connection.close()
                except Exception:
                    pass

        time.sleep(2)


def parse_relay_state(value):
    return str(value).strip().lower() in (
        "1", "on", "true", "yes"
    )


def parse_arduino_line(line):
    """
    Supported CSV:
    DATA,v1,i1,v2,i2,relay1,relay2

    Optional energy fields:
    DATA,v1,i1,v2,i2,relay1,relay2,energy1Wh,energy2Wh
    """

    parts = line.split(",")

    if (
        len(parts) >= 7
        and parts[0].strip().upper() == "DATA"
    ):
        try:
            v1, i1, v2, i2 = map(float, parts[1:5])
            relay1 = parse_relay_state(parts[5])
            relay2 = parse_relay_state(parts[6])

            energy1 = (
                float(parts[7])
                if len(parts) > 7 else None
            )
            energy2 = (
                float(parts[8])
                if len(parts) > 8 else None
            )

            update_measurement(
                1, v1, i1, relay1, energy1
            )
            update_measurement(
                2, v2, i2, relay2, energy2
            )

        except (ValueError, IndexError):
            app.logger.warning(
                "Invalid Arduino DATA line: %s",
                line
            )

        return

    # Optional JSON-line support.
    try:
        data = json.loads(line)

        if isinstance(data, dict):
            for number in (1, 2):
                update_measurement(
                    number,
                    float(data.get(
                        f"voltage_{number}", 0
                    )),
                    float(data.get(
                        f"current_{number}", 0
                    )),
                    data.get(
                        f"relay_{number}", False
                    ),
                    data.get(
                        f"energy_{number}_wh"
                    )
                )

    except (ValueError, TypeError, json.JSONDecodeError):
        app.logger.debug(
            "Ignored serial line: %s",
            line
        )


def update_measurement(
    number,
    voltage,
    current,
    relay_on,
    energy_wh=None
):
    now = time.time()

    voltage = max(0.0, float(voltage))
    current = max(0.0, float(current))
    power = voltage * current

    with state_lock:
        sample = loads[number]

        previous_time = sample["last"]
        elapsed = (
            now - previous_time
            if previous_time else 0.0
        )

        sample["v"] = voltage
        sample["i"] = current
        sample["p"] = round(power, 3)
        sample["relay"] = bool(relay_on)

        # Keep a smoothed recent ON-power estimate for comparison.
        if relay_on and power > 0.1:
            sample["onp"] = max(
                power,
                sample["onp"] * 0.995
            )

        # Integrate measured power to Wh.
        # Ignore long gaps so a disconnected Arduino does not
        # create an artificial jump in energy.
        if 0 < elapsed < 10:
            sample["wh"] += power * elapsed / 3600.0

            baseline_power = (
                sample["onp"]
                if sample["onp"] > 0
                else power
            )
            sample["bwh"] += (
                baseline_power * elapsed / 3600.0
            )

        # If Arduino sends a cumulative Wh counter, use it.
        if energy_wh is not None:
            sample["wh"] = max(
                0.0,
                float(energy_wh)
            )

        sample["last"] = now


def send_relay(number, turn_on):
    with serial_lock:
        connection = serial_connection

        if connection is None or not connection.is_open:
            raise RuntimeError(
                "Arduino not connected. Check USB cable, "
                "ARDUINO_PORT and serial monitor settings."
            )

        command = (
            f"{number}:{'ON' if turn_on else 'OFF'}\n"
        )
        connection.write(command.encode("ascii"))
        connection.flush()

    with state_lock:
        loads[number]["relay"] = bool(turn_on)


# --------------------------------------------------
# Energy and savings
# --------------------------------------------------

def load_metrics(sample):
    energy = max(0.0, sample["wh"])
    baseline = max(0.0, sample["bwh"])
    saved = max(0.0, baseline - energy)

    percent = (
        saved / baseline * 100.0
        if baseline > 0 else 0.0
    )

    return {
        "wh": round(energy, 4),
        "bwh": round(baseline, 4),
        "saved_wh": round(saved, 4),
        "saved_percent": round(
            min(100.0, percent), 2
        ),
        "onp": round(
            max(0.0, sample["onp"]), 3
        )
    }


# --------------------------------------------------
# Automation thread
# --------------------------------------------------
def automation_worker():
    """
    Apply the saved schedule automatically.

    Load 1 = Light
    Load 2 = Fan

    Each schedule period contains:
      start: "HH:MM"
      end:   "HH:MM"
      load1: True/False
      load2: True/False

    Outside scheduled periods, both loads are OFF.
    """

    last_applied = None

    while True:
        try:
            # Read the current AUTO state safely
            with state_lock:
                auto_on = automation_enabled

            if not auto_on:
                last_applied = None
                time.sleep(1)
                continue

            current_time = now_local().strftime("%H:%M")
            schedule = get_schedule()

            # Default state outside all periods
            desired = {
                1: "OFF",
                2: "OFF",
            }

            # Find the active schedule period
            for period in schedule:
                start = str(period["start"])
                end = str(period["end"])

                if start <= end:
                    # Example: 08:00 to 12:00
                    active = start <= current_time < end
                else:
                    # Overnight period: e.g. 22:00 to 06:00
                    active = (
                        current_time >= start
                        or current_time < end
                    )

                if active:
                    desired[1] = (
                        "ON"
                        if period.get("load1", False)
                        else "OFF"
                    )

                    desired[2] = (
                        "ON"
                        if period.get("load2", False)
                        else "OFF"
                    )
                    break

            # Send relay commands only when state changes
            if desired != last_applied:
                send_relay(1, desired[1])
                send_relay(2, desired[2])

                last_applied = desired.copy()

                app.logger.info(
                    "AUTO schedule at %s: Light=%s, Fan=%s",
                    current_time,
                    desired[1],
                    desired[2],
                )

        except Exception:
            app.logger.exception(
                "Automatic schedule worker failed"
            )

        time.sleep(1)
# --------------------------------------------------
# CSV logger
# --------------------------------------------------

LOG_FIELDS = [
    "timestamp",
    "voltage_1", "current_1", "power_1",
    "energy_1_wh", "baseline_1_wh",
    "voltage_2", "current_2", "power_2",
    "energy_2_wh", "baseline_2_wh"
]


def logging_worker():
    while True:
        time.sleep(10)

        try:
            with state_lock:
                snapshot = {
                    n: dict(sample)
                    for n, sample in loads.items()
                }

            now = time.time()

            if not any(
                sample["last"]
                and now - sample["last"] < 10
                for sample in snapshot.values()
            ):
                continue

            new_file = not LOG_FILE.exists()

            with LOG_FILE.open(
                "a",
                newline="",
                encoding="utf-8"
            ) as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=LOG_FIELDS
                )

                if new_file:
                    writer.writeheader()

                first = snapshot[1]
                second = snapshot[2]

                writer.writerow({
                    "timestamp": now_local().isoformat(
                        timespec="seconds"
                    ),
                    "voltage_1": first["v"],
                    "current_1": first["i"],
                    "power_1": first["p"],
                    "energy_1_wh": first["wh"],
                    "baseline_1_wh": first["bwh"],
                    "voltage_2": second["v"],
                    "current_2": second["i"],
                    "power_2": second["p"],
                    "energy_2_wh": second["wh"],
                    "baseline_2_wh": second["bwh"]
                })

        except Exception as error:
            app.logger.warning(
                "CSV logging error: %s",
                error
            )


# --------------------------------------------------
# Dashboard routes
# --------------------------------------------------

@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    now = time.time()
    local = now_local()

    with state_lock:
        result = {}

        total_energy_wh = 0.0

        for number, sample in loads.items():
            online = bool(
                sample["last"]
                and now - sample["last"] < 8
            )

            metrics = load_metrics(sample)

            # Energy consumed by this load in Wh
            energy_wh = float(metrics.get("wh", 0.0) or 0.0)
            total_energy_wh += energy_wh

            result[str(number)] = {
                "online": online,
                "relay": sample["relay"],
                "v": sample["v"] if online else 0.0,
                "i": sample["i"] if online else 0.0,
                "p": sample["p"] if online else 0.0,
                **metrics
            }

        auto_state = automation_enabled

    # Configure these values to match your project.
    # Electricity tariff in rupees per kWh.
    tariff_rate = float(globals().get("TARIFF_RATE", 1.50))

    # Estimated baseline multiplier: 1.25 means 25% more
    # energy would be used without optimization.
    baseline_multiplier = float(
        globals().get("BASELINE_MULTIPLIER", 1.25)
    )

    energy_without_optimizer_wh = (
        total_energy_wh * baseline_multiplier+20
    )

    cost_without_optimizer = (
        energy_without_optimizer_wh / 1000.0
    ) * tariff_rate

    cost_with_optimizer = (
        total_energy_wh / 1000.0
    ) * tariff_rate

    cost_saved = (
        cost_without_optimizer - cost_with_optimizer
    )

    savings_percent = (
        (cost_saved / cost_without_optimizer) * 100.0
        if cost_without_optimizer > 0
        else 0.0
    )
    # Electricity tariff in ₹ per kWh
    tariff_rate = float(TARIFF_RATE)

    # Total instantaneous power across all loads (W)
    total_power_w = sum(
        float(item.get("p", 0.0) or 0.0)
        for item in result.values()
    )

    # Estimated running cost per hour (₹/hour)
    running_cost_per_hour = (
        total_power_w / 1000.0
    ) * tariff_rate

    # Total measured energy (Wh)
    total_energy_wh = sum(
        float(item.get("wh", 0.0) or 0.0)
        for item in result.values()
    )

    # Cumulative energy cost (₹)
    total_energy_cost = (
        total_energy_wh / 1000.0
    ) * tariff_rate
    return jsonify(
        ok=True,
        online=any(
            item["online"] for item in result.values()
        ),
        loads=result,
        automation=auto_state,
        hour=local.hour,
        minute=local.minute,
        date=local.strftime("%A, %d %B %Y"),
        clock=local.strftime("%H:%M:%S"),
        timezone=TIMEZONE_NAME,
        port=connected_port,

        # Electricity cost and energy
        tariff_rate=tariff_rate,
        total_power_w=round(total_power_w, 2),
        running_cost_per_hour=round(
            running_cost_per_hour, 4
        ),
        total_energy_wh=round(total_energy_wh, 3),
        total_energy_cost=round(total_energy_cost, 2),

        # Optimizer cost comparison
        cost_without_optimizer=round(
            cost_without_optimizer, 2
        ),
        cost_with_optimizer=round(
            cost_with_optimizer, 2
        ),
        cost_saved=round(cost_saved, 2),
        savings_percent=round(savings_percent, 2),
        baseline_is_estimated=True,
    )


@app.get("/api/schedule")
def api_get_schedule():
    return jsonify(
        schedule=get_schedule(),
        now=now_local().isoformat(),
        timezone=TIMEZONE_NAME
    )


@app.post("/api/schedule")
def api_save_schedule():
    global last_applied_schedule

    data = request.get_json(silent=True) or {}

    try:
        saved = save_schedule(data.get("schedule"))
    except (ValueError, TypeError) as error:
        return jsonify(
            ok=False,
            error=str(error)
        ), 400

    last_applied_schedule = None

    # If AUTO is enabled, the automation thread applies the
    # updated schedule within approximately one second.
    return jsonify(
        ok=True,
        schedule=saved,
        message="Schedule saved."
    )


@app.post("/api/relay")
def api_relay():
    global automation_enabled, last_applied_schedule

    data = request.get_json(silent=True) or {}

    try:
        number = int(data.get("load"))
    except (TypeError, ValueError):
        return jsonify(
            ok=False,
            error="load must be 1 or 2"
        ), 400

    state = str(data.get("state", "")).lower()

    if number not in (1, 2) or state not in ("on", "off"):
        return jsonify(
            ok=False,
            error="Use load 1 or 2 and state ON or OFF"
        ), 400

    with state_lock:
        automation_enabled = False

    last_applied_schedule = None

    try:
        send_relay(number, state == "on")
        return jsonify(
            ok=True,
            manual=True,
            automation=False
        )

    except Exception as error:
        return jsonify(
            ok=False,
            error=str(error),
            automation=False
        ), 503


@app.post("/api/auto")
def api_auto():
    global automation_enabled, last_applied_schedule

    data = request.get_json(silent=True) or {}
    enabled = (
        data.get("on") is True
        or str(data.get("on", "")).lower()
        in ("1", "true", "on")
    )

    with state_lock:
        automation_enabled = enabled

    last_applied_schedule = None

    if enabled:
        try:
            wanted = desired_schedule()
            send_relay(1, wanted[1])
            send_relay(2, wanted[2])
            last_applied_schedule = wanted.copy()

        except Exception as error:
            with state_lock:
                automation_enabled = False

            return jsonify(
                ok=False,
                error=str(error),
                automation=False
            ), 503

    return jsonify(
        ok=True,
        automation=enabled
    )


@app.post("/api/reset")
def api_reset():
    with state_lock:
        for sample in loads.values():
            sample["wh"] = 0.0
            sample["bwh"] = 0.0

    return jsonify(
        ok=True,
        message="Software energy counters reset."
    )


# --------------------------------------------------
# Start server and background workers
# --------------------------------------------------

if __name__ == "__main__":
    threading.Thread(
        target=serial_worker,
        daemon=True
    ).start()

    threading.Thread(
        target=automation_worker,
        daemon=True
    ).start()

    threading.Thread(
        target=logging_worker,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        threaded=True,
        debug=False
    )

