#!/usr/bin/env python3
"""
Fetch scan images from OpenScan Mini via Samba share.
Copies JPGs from the scanner to the local 1TB drive on the Pi.
"""
import os
import subprocess
import sys


def fetch_scan(openscan_ip, project_name, output_dir,
               smb_user="pi", smb_pass="raspberry",
               smb_share="PiShare", scan_path="OpenScan/scans"):
    """
    Download all images from a specific scan project on OpenScan Mini.
    
    Args:
        openscan_ip: IP address of OpenScan Mini
        project_name: Name of the scan project folder
        output_dir: Local directory to save images to
        smb_user: Samba username
        smb_pass: Samba password
        smb_share: Samba share name
        scan_path: Path within share to scans directory
    
    Returns:
        List of downloaded file paths
    """
    local_dir = os.path.join(output_dir, project_name)
    os.makedirs(local_dir, exist_ok=True)

    remote_path = f"{scan_path}/{project_name}"
    smb_cmd = f"recurse; prompt; lcd {local_dir}; cd {remote_path}; mget *"

    print(f"Fetching scan '{project_name}' from {openscan_ip}...")
    print(f"  Remote: //{openscan_ip}/{smb_share}/{remote_path}")
    print(f"  Local:  {local_dir}")

    result = subprocess.run(
        ["smbclient", f"//{openscan_ip}/{smb_share}",
         "-U", f"{smb_user}%{smb_pass}",
         "-c", smb_cmd],
        capture_output=True, text=True, timeout=600
    )

    if result.returncode != 0:
        print(f"smbclient stderr: {result.stderr}", file=sys.stderr)
        raise RuntimeError(f"Failed to fetch scan: smbclient exit code {result.returncode}")

    # List downloaded files
    files = []
    allowed_ext = {".jpg", ".jpeg", ".png"}
    for f in os.listdir(local_dir):
        if os.path.splitext(f)[1].lower() in allowed_ext:
            files.append(os.path.join(local_dir, f))

    print(f"Downloaded {len(files)} images to {local_dir}")
    return files


def get_latest_scan(openscan_ip, smb_user="pi", smb_pass="raspberry"):
    """Get the most recently modified scan directory name."""
    result = subprocess.run(
        ["smbclient", f"//{openscan_ip}/PiShare",
         "-U", f"{smb_user}%{smb_pass}",
         "-c", "ls OpenScan/scans/*"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return None

    dirs = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line and "D" in line:
            parts = line.split()
            if parts and parts[0] not in (".", ".."):
                dirs.append(parts[0])

    if dirs:
        # Return last directory (usually most recent)
        return dirs[-1]
    return None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch scan images from OpenScan Mini")
    parser.add_argument("--ip", required=True, help="OpenScan Mini IP address")
    parser.add_argument("--project", help="Scan project name (default: latest)")
    parser.add_argument("--output", default="/mnt/scandata/scans", help="Output directory")
    parser.add_argument("--user", default="pi", help="Samba username")
    parser.add_argument("--password", default="raspberry", help="Samba password")
    args = parser.parse_args()

    project = args.project
    if not project:
        project = get_latest_scan(args.ip, args.user, args.password)
        if not project:
            print("ERROR: No scans found and no project specified", file=sys.stderr)
            sys.exit(1)
        print(f"Auto-detected latest scan: {project}")

    files = fetch_scan(args.ip, project, args.output, args.user, args.password)
    if not files:
        print("ERROR: No images downloaded", file=sys.stderr)
        sys.exit(1)
    print(f"OK: {len(files)} images")
