#!/usr/bin/env python3
"""
Discover Bambu Lab printer on the local network via SSDP and port scanning.
Returns the printer's current IP address.
"""
import socket
import ssl
import struct
import sys
import time
import re
import subprocess


SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 2021  # Bambu Lab uses port 2021 for SSDP, not standard 1900
MQTT_PORT = 8883
SUBNET_PREFIX = "192.168.1."
SCAN_TIMEOUT = 2


def discover_ssdp(timeout=5):
    """Try Bambu Lab SSDP discovery on port 2021."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 3\r\n"
        "ST: urn:bambulab-com:device:3dprinter:1\r\n"
        "\r\n"
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    sock.settimeout(timeout)

    try:
        sock.sendto(msg.encode(), (SSDP_ADDR, SSDP_PORT))
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                response = data.decode(errors="ignore")
                if "bambu" in response.lower() or "3dprinter" in response.lower():
                    print(f"SSDP: Found printer at {addr[0]}")
                    return addr[0]
            except socket.timeout:
                break
    finally:
        sock.close()

    return None


def discover_port_scan(serial=None, timeout=SCAN_TIMEOUT):
    """Scan subnet for hosts with MQTT port 8883 open, verify via TLS cert."""
    print(f"Port scanning {SUBNET_PREFIX}0/24 for MQTT :8883 ...")
    found = []

    for i in range(1, 255):
        ip = f"{SUBNET_PREFIX}{i}"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        result = sock.connect_ex((ip, MQTT_PORT))
        sock.close()
        if result == 0:
            found.append(ip)
            print(f"  Port 8883 open: {ip}")

    for ip in found:
        detected_serial = verify_bambu_tls(ip)
        if detected_serial:
            if serial is None or detected_serial == serial:
                print(f"Confirmed Bambu printer at {ip} (serial: {detected_serial})")
                return ip
            else:
                print(f"  {ip} serial {detected_serial} != expected {serial}, skipping")

    # If no TLS match but found open ports, return first one
    if found:
        print(f"Returning first candidate: {found[0]}")
        return found[0]

    return None


def verify_bambu_tls(ip):
    """Connect to MQTT TLS port and extract serial from certificate CN."""
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((ip, MQTT_PORT), timeout=3) as sock:
            with ctx.wrap_socket(sock, server_hostname=ip) as ssock:
                cert = ssock.getpeercert(binary_form=False)
                if cert:
                    for field in cert.get("subject", ()):
                        for key, value in field:
                            if key == "commonName":
                                return value
                # Try binary cert
                der = ssock.getpeercert(binary_form=True)
                if der:
                    # Look for serial pattern in raw bytes
                    text = str(der)
                    match = re.search(r"00M\w+", text)
                    if match:
                        return match.group(0)
    except Exception as e:
        pass
    return None


def discover(serial=None):
    """Try SSDP first, fall back to port scan."""
    print("Attempting SSDP discovery...")
    ip = discover_ssdp()
    if ip:
        return ip

    print("SSDP failed, trying port scan...")
    ip = discover_port_scan(serial=serial)
    return ip


if __name__ == "__main__":
    serial = sys.argv[1] if len(sys.argv) > 1 else None
    ip = discover(serial=serial)
    if ip:
        print(f"PRINTER_IP={ip}")
        sys.exit(0)
    else:
        print("ERROR: Could not find Bambu printer on network", file=sys.stderr)
        sys.exit(1)
