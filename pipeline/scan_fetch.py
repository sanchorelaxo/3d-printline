#!/usr/bin/env python3
"""Fetch scan images from OpenScan Mini via Samba share.
Copies JPGs (or zip of JPGs) from the scanner to the local 1TB drive on the Pi.
"""
import os
import subprocess
import sys
import zipfile


def fetch_scan(openscan_ip, project_name, output_dir,
               smb_user="pi", smb_pass="raspberry",
               smb_share="PiShare", scan_path="OpenScan/scans"):
    """
    Download scan from OpenScan Mini. Handles both zip files and directories.
    
    Args:
        openscan_ip: IP address of OpenScan Mini
        project_name: Name of the scan project (zip filename or folder name)
        output_dir: Local directory to save images to
        smb_user: Samba username
        smb_pass: Samba password
        smb_share: Samba share name
        scan_path: Path within share to scans directory
    
    Returns:
        List of downloaded file paths
    """
    # Determine a clean project label for local directory
    label = project_name.replace(".zip", "")
    local_dir = os.path.join(output_dir, label)
    os.makedirs(local_dir, exist_ok=True)

    is_zip = project_name.endswith(".zip")

    if is_zip:
        # Download the zip file first, then extract
        zip_local = os.path.join(output_dir, project_name)
        smb_cmd = f'prompt; lcd {output_dir}; cd {scan_path}; get "{project_name}"'
    else:
        # Try as a directory of images
        remote_path = f"{scan_path}/{project_name}"
        smb_cmd = f"recurse; prompt; lcd {local_dir}; cd {remote_path}; mget *"

    print(f"Fetching scan '{project_name}' from {openscan_ip}...")
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

    # If zip, extract images
    if is_zip:
        print(f"Extracting {project_name}...")
        with zipfile.ZipFile(zip_local, "r") as zf:
            zf.extractall(local_dir)
        os.remove(zip_local)
        print(f"Extracted to {local_dir}")

    # Collect image files (may be in subdirectories after extraction)
    files = []
    allowed_ext = {".jpg", ".jpeg", ".png"}
    for root, dirs, filenames in os.walk(local_dir):
        for f in filenames:
            if os.path.splitext(f)[1].lower() in allowed_ext:
                files.append(os.path.join(root, f))

    print(f"Found {len(files)} images in {local_dir}")
    return files


def get_latest_scan(openscan_ip, smb_user="pi", smb_pass="raspberry"):
    """Get the most recent scan entry (zip file or directory)."""
    result = subprocess.run(
        ["smbclient", f"//{openscan_ip}/PiShare",
         "-U", f"{smb_user}%{smb_pass}",
         "-c", "ls OpenScan/scans/*"],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0:
        return None

    entries = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if not parts or parts[0] in (".", ".."):
            continue
        name = parts[0]
        # Accept zip files and directories (but not 'preview')
        if name.endswith(".zip"):
            entries.append(name)
        elif "D" in line and name != "preview":
            entries.append(name)

    if entries:
        # Return last entry (usually most recent)
        return entries[-1]
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
