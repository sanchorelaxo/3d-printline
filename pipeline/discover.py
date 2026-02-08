#!/usr/bin/env python3
"""
Network discovery for OpenScan Mini (mDNS/Samba) and Bambu printer (SSDP/port scan).
Runs on the Raspberry Pi.
"""
import socket
import subprocess
import sys
import re


def discover_openscan(hostname="openscan.local", timeout=3):
    """Try to resolve OpenScan Mini via mDNS."""
    try:
        ip = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM, 0, 0)
        if ip:
            addr = ip[0][4][0]
            print(f"OpenScan found: {addr} ({hostname})")
            return addr
    except socket.gaierror:
        pass

    # Fallback: try avahi-resolve
    try:
        result = subprocess.run(
            ["avahi-resolve", "-n", hostname, "-4"],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                addr = parts[1]
                print(f"OpenScan found via avahi: {addr}")
                return addr
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return None


def check_openscan_samba(ip, timeout=3):
    """Verify Samba port 445 is open on OpenScan."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        result = sock.connect_ex((ip, 445))
        return result == 0
    finally:
        sock.close()


def list_openscan_scans(ip, smb_user="pi", smb_pass="raspberry"):
    """List scan directories available on OpenScan Samba share."""
    try:
        result = subprocess.run(
            ["smbclient", f"//{ip}/PiShare", "-U", f"{smb_user}%{smb_pass}",
             "-c", "ls OpenScan/scans/*"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            dirs = []
            for line in result.stdout.strip().split("\n"):
                line = line.strip()
                if line and "D" in line and not line.startswith("."):
                    # Parse smbclient ls output: "dirname  D  0  date"
                    parts = line.split()
                    if parts and parts[0] not in (".", ".."):
                        dirs.append(parts[0])
            return dirs
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"WARNING: Could not list scans: {e}", file=sys.stderr)
    return []


def discover_all(openscan_host="openscan.local", smb_user="pi", smb_pass="raspberry"):
    """Discover OpenScan and return info dict. Fails silently if not found."""
    result = {"openscan_ip": None, "samba_ok": False, "scans": []}

    ip = discover_openscan(openscan_host)
    if not ip:
        print("OpenScan not found on network (silent fail)")
        return result

    result["openscan_ip"] = ip
    result["samba_ok"] = check_openscan_samba(ip)

    if result["samba_ok"]:
        result["scans"] = list_openscan_scans(ip, smb_user, smb_pass)
        print(f"Available scans: {result['scans']}")

    return result


if __name__ == "__main__":
    import json
    info = discover_all()
    print(json.dumps(info, indent=2))
