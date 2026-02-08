#!/usr/bin/env python3
"""
Auto-detect watcher for new scans on OpenScan Mini.
Polls the Samba share periodically and triggers the pipeline when a new scan appears.
Fails silently if OpenScan is unreachable.
"""
import argparse
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env(env_path):
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


def get_scan_list(openscan_ip, smb_user, smb_pass):
    """Get list of scan directories from OpenScan Samba share."""
    try:
        result = subprocess.run(
            ["smbclient", f"//{openscan_ip}/PiShare",
             "-U", f"{smb_user}%{smb_pass}",
             "-c", "ls OpenScan/scans/*"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return None  # Silent fail

        dirs = set()
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if line and "D" in line:
                parts = line.split()
                if parts and parts[0] not in (".", ".."):
                    dirs.add(parts[0])
        return dirs
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None  # Silent fail


def notify(message):
    """Send notification via OpenClaw."""
    print(message)
    try:
        subprocess.run(
            ["openclaw", "send", "--message", f"ℹ️ 3d-printline watcher: {message}"],
            timeout=30, capture_output=True
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def run_pipeline(env_path, project_name):
    """Trigger the full pipeline for a detected scan."""
    pipeline_script = os.path.join(SCRIPT_DIR, "run_pipeline.py")
    cmd = [sys.executable, pipeline_script, "--config", env_path, "--project", project_name]
    print(f"Triggering pipeline: {' '.join(cmd)}")
    subprocess.Popen(cmd)


def watch(env_path, poll_interval=60):
    """Main watcher loop."""
    config = load_env(env_path)
    openscan_host = config.get("OPENSCAN_HOST", "openscan.local")
    smb_user = config.get("OPENSCAN_SMB_USER", "pi")
    smb_pass = config.get("OPENSCAN_SMB_PASS", "raspberry")

    # Resolve hostname to IP
    import socket
    try:
        ip_info = socket.getaddrinfo(openscan_host, None, socket.AF_INET)
        openscan_ip = ip_info[0][4][0] if ip_info else openscan_host
    except socket.gaierror:
        openscan_ip = openscan_host

    known_scans = set()

    # Get initial scan list
    initial = get_scan_list(openscan_ip, smb_user, smb_pass)
    if initial is not None:
        known_scans = initial
        print(f"Initial scans ({len(known_scans)}): {sorted(known_scans)}")
    else:
        print("OpenScan not reachable, will retry...")

    notify("Watcher started — monitoring for new scans")

    while True:
        time.sleep(poll_interval)

        current = get_scan_list(openscan_ip, smb_user, smb_pass)
        if current is None:
            # OpenScan unreachable, fail silently
            continue

        new_scans = current - known_scans
        if new_scans:
            for scan in sorted(new_scans):
                print(f"New scan detected: {scan}")
                notify(f"New scan detected: {scan} — starting pipeline")
                run_pipeline(env_path, scan)

        known_scans = current


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Watch for new scans on OpenScan Mini")
    parser.add_argument("--config", required=True, help="Path to .env config file")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds")
    args = parser.parse_args()

    try:
        watch(args.config, args.interval)
    except KeyboardInterrupt:
        print("\nWatcher stopped")
