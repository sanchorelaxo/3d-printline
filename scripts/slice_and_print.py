#!/usr/bin/env python3
"""
Slice STL to 3MF using OrcaSlicer CLI, upload via FTPS, and trigger print via MQTT.
Runs on the laptop. Called from Pi via SSH.

Usage:
  python3 slice_and_print.py --stl model.stl --config /path/to/.env
  python3 slice_and_print.py --threemf model.3mf --config /path/to/.env  (skip slicing)
"""
import argparse
import ftplib
import json
import os
import ssl
import subprocess
import sys
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("ERROR: paho-mqtt not installed. Run: pip3 install paho-mqtt", file=sys.stderr)
    sys.exit(1)


def load_env(env_path):
    """Load .env file into a dict."""
    config = {}
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                config[key.strip()] = val.strip()
    return config


def discover_printer(serial):
    """Run bambu_discover.py to find printer IP."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    discover_script = os.path.join(script_dir, "bambu_discover.py")
    result = subprocess.run(
        [sys.executable, discover_script, serial],
        capture_output=True, text=True, timeout=30
    )
    for line in result.stdout.strip().split("\n"):
        if line.startswith("PRINTER_IP="):
            return line.split("=", 1)[1]
    print(f"Discovery output: {result.stdout}", file=sys.stderr)
    print(f"Discovery errors: {result.stderr}", file=sys.stderr)
    return None


def slice_stl(stl_path, output_3mf, slicer_profile=None, filament=None):
    """Slice STL to 3MF using OrcaSlicer CLI.

    slicer_profile: semicolon-separated machine;process JSON paths
    filament: filament JSON path
    Both should be flattened (no inheritance) with 'from': 'system'.
    """
    # --debug 5 is required to bypass a layer_gcode validation bug in OrcaSlicer CLI
    # --no-check skips empty-layer warnings that abort export
    cmd = ["orca-slicer", "--debug", "5", "--no-check", "--slice", "0"]
    if slicer_profile:
        cmd.extend(["--load-settings", slicer_profile])
    if filament:
        cmd.extend(["--load-filaments", filament])
    cmd.extend([stl_path, "--export-3mf", output_3mf])

    print(f"Slicing: {os.path.basename(stl_path)} → {os.path.basename(output_3mf)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"Slicer stdout: {result.stdout[-500:]}", file=sys.stderr)
        print(f"Slicer stderr: {result.stderr[-500:]}", file=sys.stderr)
        raise RuntimeError(f"OrcaSlicer failed with exit code {result.returncode}")
    if not os.path.exists(output_3mf):
        raise RuntimeError("OrcaSlicer exited 0 but 3MF was not created")
    print(f"Sliced: {output_3mf} ({os.path.getsize(output_3mf) / 1e6:.1f} MB)")
    return output_3mf


class ImplicitFTPS(ftplib.FTP_TLS):
    """FTP_TLS subclass for Bambu implicit FTPS (SSL from first byte, port 990).
    Also handles TLS session reuse required by Bambu's vsFTPd."""
    def connect(self, host='', port=0, timeout=-999, source_address=None):
        import socket
        if host != '':
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        self.sock = socket.create_connection(
            (self.host, self.port), self.timeout, source_address)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        # Reuse TLS session from control connection for data connection
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host,
                session=self.sock.session)
        return conn, size


def upload_ftps(printer_ip, access_code, filepath):
    """Upload 3MF to printer via implicit FTPS (port 990)."""
    filename = os.path.basename(filepath)
    print(f"Uploading {filename} to {printer_ip}:990 via FTPS...")

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ftp = ImplicitFTPS(context=ctx)
    ftp.connect(printer_ip, 990, timeout=30)
    ftp.login("bblp", access_code)
    ftp.prot_p()

    # Bambu printers require uploads to /cache/ or /model/
    try:
        ftp.cwd("/cache")
    except ftplib.error_perm:
        try:
            ftp.mkd("/cache")
            ftp.cwd("/cache")
        except ftplib.error_perm:
            pass  # May already be there or not needed

    with open(filepath, "rb") as f:
        ftp.storbinary(f"STOR {filename}", f)

    ftp.quit()
    print(f"Upload complete: {filename}")
    return filename


MQTT_SIGNATURE_REQUIRED = 0x20000000


def check_mqtt_signature_required(printer_ip, serial, access_code):
    """Check if printer firmware requires MQTT message signing."""
    import uuid
    result = {"required": None}

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe(f"device/{serial}/report")
            client.publish(f"device/{serial}/request",
                           json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            if "print" in data and "fun" in data["print"]:
                fun_int = int(data["print"]["fun"], 16)
                result["required"] = bool(fun_int & MQTT_SIGNATURE_REQUIRED)
        except (json.JSONDecodeError, KeyError, ValueError):
            pass

    client = mqtt.Client(client_id=f"probe-{uuid.uuid4()}",
                         protocol=mqtt.MQTTv311, clean_session=True)
    client.username_pw_set("bblp", access_code)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(printer_ip, 8883, 60)

    deadline = time.time() + 5
    while time.time() < deadline and result["required"] is None:
        client.loop(timeout=0.5)
    client.disconnect()
    return result["required"] or False


def trigger_print(printer_ip, serial, access_code, filename):
    """Send MQTT command to start printing the uploaded file.

    If firmware requires MQTT signing (01.11+), returns upload-only status
    so the user can start the print from the touchscreen.
    """
    # Check if firmware blocks unsigned MQTT commands
    if check_mqtt_signature_required(printer_ip, serial, access_code):
        print("NOTE: Firmware requires MQTT message signing (01.11+).")
        print(f"File '{filename}' uploaded to printer SD card (/cache/).")
        print(">>> Start the print from the printer touchscreen: SD Card → cache → " + filename)
        return {"status": "UPLOADED", "percent": 0, "remaining": 0,
                "note": "MQTT signing required; start print from touchscreen"}

    topic_request = f"device/{serial}/request"
    topic_report = f"device/{serial}/report"

    print_cmd = {
        "print": {
            "sequence_id": 0,
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "subtask_name": filename.replace(".3mf", ""),
            "file": "",
            "url": f"file:///sdcard/cache/{filename}",
            "timelapse": False,
            "bed_type": "auto",
            "bed_leveling": True,
            "flow_cali": True,
            "vibration_cali": True,
            "layer_inspect": True,
            "ams_mapping": [0],
            "use_ams": False
        }
    }

    result = {"status": None, "percent": 0, "remaining": 0}

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            print("MQTT connected")
            client.subscribe(topic_report)
            payload = json.dumps(print_cmd)
            print(f"Sending print command to {topic_request}")
            client.publish(topic_request, payload)
        else:
            print(f"MQTT connection failed: rc={rc}", file=sys.stderr)

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload)
            if "print" in data:
                p = data["print"]
                state = p.get("gcode_state", "")
                percent = p.get("mc_percent", 0)
                remaining = p.get("mc_remaining_time", 0)
                if state:
                    result["status"] = state
                    result["percent"] = percent
                    result["remaining"] = remaining
        except (json.JSONDecodeError, KeyError):
            pass

    client = mqtt.Client()
    client.username_pw_set("bblp", access_code)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"Connecting MQTT to {printer_ip}:8883...")
    client.connect(printer_ip, 8883, 60)
    client.loop_start()

    timeout = 30
    start = time.time()
    while time.time() - start < timeout:
        if result["status"] in ("RUNNING", "PREPARE"):
            print(f"Print started! Status: {result['status']}")
            break
        time.sleep(1)
    else:
        if result["status"]:
            print(f"Print status after {timeout}s: {result['status']}")
        else:
            print("WARNING: No status received, print may not have started", file=sys.stderr)

    client.loop_stop()
    client.disconnect()
    return result


def main():
    parser = argparse.ArgumentParser(description="Slice, upload, and print on Bambu X1-Carbon")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--stl", help="STL file to slice and print")
    group.add_argument("--threemf", help="Pre-sliced 3MF file to print directly")
    parser.add_argument("--config", required=True, help="Path to .env config file")
    parser.add_argument("--printer-ip", help="Override printer IP (skip discovery)")
    args = parser.parse_args()

    config = load_env(args.config)
    serial = config.get("BAMBU_SERIAL", "")
    access_code = config.get("BAMBU_ACCESS_CODE", "")

    if not serial or not access_code:
        print("ERROR: BAMBU_SERIAL and BAMBU_ACCESS_CODE required in config", file=sys.stderr)
        sys.exit(1)

    # Discover printer
    printer_ip = args.printer_ip
    if not printer_ip:
        printer_ip = discover_printer(serial)
        if not printer_ip:
            print("ERROR: Could not discover printer", file=sys.stderr)
            sys.exit(1)

    # Slice if needed
    if args.stl:
        output_dir = os.path.dirname(args.stl) or "."
        threemf_path = os.path.join(output_dir, os.path.splitext(os.path.basename(args.stl))[0] + ".3mf")

        # Build profile paths (flattened JSONs in profiles/ dir)
        pipeline_dir = config.get("LAPTOP_PIPELINE_DIR",
                                   os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        profiles = os.path.join(pipeline_dir, "profiles")
        machine_json = os.path.join(profiles, "x1c_machine.json")
        process_json = os.path.join(profiles, "x1c_process.json")
        filament_json = os.path.join(profiles, "x1c_filament.json")

        slicer_profile = None
        if os.path.isfile(machine_json) and os.path.isfile(process_json):
            slicer_profile = f"{machine_json};{process_json}"
        filament_path = filament_json if os.path.isfile(filament_json) else None

        slice_stl(
            args.stl, threemf_path,
            slicer_profile=slicer_profile,
            filament=filament_path
        )
    else:
        threemf_path = args.threemf

    if not os.path.exists(threemf_path):
        print(f"ERROR: File not found: {threemf_path}", file=sys.stderr)
        sys.exit(1)

    # Upload and print
    filename = upload_ftps(printer_ip, access_code, threemf_path)
    result = trigger_print(printer_ip, serial, access_code, filename)

    # Output result for pipeline consumption
    print(json.dumps({
        "printer_ip": printer_ip,
        "file": filename,
        "status": result.get("status"),
        "percent": result.get("percent", 0),
        "remaining_minutes": result.get("remaining", 0)
    }))


if __name__ == "__main__":
    main()
