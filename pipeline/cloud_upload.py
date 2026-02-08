#!/usr/bin/env python3
"""
Upload scan images to OpenScanCloud, poll for processing completion, and download the result.
Based on the official OpenScanCloud uploader.py.
"""
import os
import requests
import sys
import time
import json
from zipfile import ZipFile


MAX_PART_SIZE = 200_000_000  # 200MB
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}


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


class OpenScanCloudClient:
    def __init__(self, server, token, user="openscan", password="free"):
        self.server = server.rstrip("/") + "/"
        self.token = token
        self.auth = (user, password)

    def _get(self, endpoint, params=None):
        if params is None:
            params = {}
        params["token"] = self.token
        r = requests.get(self.server + endpoint, auth=self.auth, params=params, timeout=120)
        return r

    def get_token_info(self):
        r = self._get("getTokenInfo")
        if r.status_code != 200:
            raise RuntimeError(f"Invalid token: HTTP {r.status_code}")
        info = r.json()
        print(f"Token info: credit={info.get('credit')}, "
              f"limit_photos={info.get('limit_photos')}, "
              f"limit_filesize={info.get('limit_filesize')}")
        return info

    def create_project(self, project_name, num_photos, num_parts, filesize):
        r = self._get("createProject", {
            "project": project_name,
            "photos": num_photos,
            "parts": num_parts,
            "filesize": filesize
        })
        if r.status_code != 200:
            raise RuntimeError(f"createProject failed: HTTP {r.status_code} - {r.text}")
        data = r.json()
        print(f"Project created: {project_name}")
        return data.get("ulink", [])

    def upload_part(self, upload_link, filepath):
        print(f"  Uploading {os.path.basename(filepath)} ({os.path.getsize(filepath) / 1e6:.1f} MB)...")
        with open(filepath, "rb") as f:
            data = f.read()
        r = requests.post(upload_link, data=data,
                          headers={"Content-type": "application/octet-stream"}, timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"Upload failed: HTTP {r.status_code}")

    def start_project(self, project_name):
        # startProject can be slow after large uploads
        params = {"token": self.token, "project": project_name}
        r = requests.get(self.server + "startProject", auth=self.auth, params=params, timeout=300)
        if r.status_code != 200:
            raise RuntimeError(f"startProject failed: HTTP {r.status_code}")
        print("Processing started on OpenScanCloud")

    def get_project_info(self, project_name):
        r = self._get("getProjectInfo", {"project": project_name})
        if r.status_code != 200:
            return {"status": "error", "message": f"HTTP {r.status_code}"}
        return r.json()

    def get_queue_estimate(self):
        try:
            r = self._get("getQueueEstimate")
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}


def collect_images(image_dir):
    """Collect image files from directory."""
    images = []
    for f in sorted(os.listdir(image_dir)):
        if os.path.splitext(f)[1].lower() in ALLOWED_EXTENSIONS:
            images.append(os.path.join(image_dir, f))
    return images


def zip_and_split(images, temp_dir, project_name):
    """Zip images and split into parts if needed."""
    os.makedirs(temp_dir, exist_ok=True)

    zip_path = os.path.join(temp_dir, f"{project_name}.zip")
    print(f"Zipping {len(images)} images...")
    with ZipFile(zip_path, "w") as zf:
        for img in images:
            zf.write(img, os.path.basename(img))

    filesize = os.path.getsize(zip_path)
    print(f"Zip size: {filesize / 1e6:.1f} MB")

    if filesize <= MAX_PART_SIZE:
        return [zip_path], filesize

    # Split into parts
    parts = []
    part_num = 1
    with open(zip_path, "rb") as f:
        while True:
            chunk = f.read(MAX_PART_SIZE)
            if not chunk:
                break
            part_path = f"{zip_path}_part{part_num}"
            with open(part_path, "wb") as pf:
                pf.write(chunk)
            parts.append(part_path)
            part_num += 1

    os.remove(zip_path)
    print(f"Split into {len(parts)} parts")
    return parts, filesize


def upload_and_process(image_dir, output_dir, env_path, project_name=None, poll_interval=60):
    """
    Full upload pipeline: zip → create project → upload → start → poll → download.
    
    Returns path to downloaded result file, or raises RuntimeError on failure.
    """
    config = load_env(env_path)
    server = config.get("OSC_SERVER", "http://openscanfeedback.dnsuser.de:1334/")
    token = config.get("OSC_TOKEN", "")
    user = config.get("OSC_USER", "openscan")
    password = config.get("OSC_PASS", "free")

    if not token:
        raise RuntimeError("OSC_TOKEN not set in config")

    client = OpenScanCloudClient(server, token, user, password)

    # Verify token
    token_info = client.get_token_info()

    # Collect images
    images = collect_images(image_dir)
    if not images:
        raise RuntimeError(f"No images found in {image_dir}")
    print(f"Found {len(images)} images")

    # Check limits
    limit_photos = token_info.get("limit_photos", 999)
    limit_filesize = token_info.get("limit_filesize", 2_000_000_000)
    if len(images) > limit_photos:
        raise RuntimeError(f"Too many photos: {len(images)} > limit {limit_photos}")

    # Generate project name — API expects simple alphanumeric-OSC.zip format
    import re
    if project_name:
        # Strip .zip and sanitize: keep only alphanumeric, dash, underscore
        label = project_name.replace(".zip", "")
        label = re.sub(r'[^a-zA-Z0-9_-]', '_', label)
    else:
        label = "scan"
    project_name = f"{int(time.time() * 100)}-{label}-OSC.zip"

    # Zip and split
    temp_dir = os.path.join(output_dir, "temp")
    parts, filesize = zip_and_split(images, temp_dir, project_name)

    if filesize > limit_filesize:
        raise RuntimeError(f"File too large: {filesize} > limit {limit_filesize}")

    # Create project
    upload_links = client.create_project(project_name, len(images), len(parts), filesize)
    if not upload_links:
        raise RuntimeError("No upload links received from server")

    # Upload parts
    for i, (part, link) in enumerate(zip(parts, upload_links)):
        print(f"Uploading part {i + 1}/{len(parts)}...")
        client.upload_part(link, part)

    # Start processing
    client.start_project(project_name)

    # Poll for completion
    print("Waiting for OpenScanCloud processing...")
    queue = client.get_queue_estimate()
    if queue:
        print(f"Queue estimate: {json.dumps(queue)}")

    max_wait = 3600  # 1 hour max
    start_time = time.time()

    while time.time() - start_time < max_wait:
        time.sleep(poll_interval)
        info = client.get_project_info(project_name)
        status = info.get("status", "unknown")
        print(f"  Status: {status} (elapsed: {int(time.time() - start_time)}s)")

        if status == "done":
            dlink = info.get("dlink", "")
            if not dlink:
                raise RuntimeError("Processing done but no download link")

            # Download result
            print(f"Downloading result from: {dlink}")
            os.makedirs(output_dir, exist_ok=True)

            r = requests.get(dlink, timeout=300)
            if r.status_code != 200:
                raise RuntimeError(f"Download failed: HTTP {r.status_code}")

            # Determine filename from URL or content-disposition
            ext = ".zip"
            if "." in dlink.split("/")[-1]:
                ext = "." + dlink.split("/")[-1].split(".")[-1]

            result_filename = project_name.replace(".zip", "") + "_result" + ext
            result_path = os.path.join(output_dir, result_filename)
            with open(result_path, "wb") as f:
                f.write(r.content)

            print(f"Result saved: {result_path} ({len(r.content) / 1e6:.1f} MB)")

            # Cleanup temp files
            for part in parts:
                if os.path.exists(part):
                    os.remove(part)

            return result_path

        elif "failed" in status.lower() or "error" in status.lower():
            raise RuntimeError(f"OpenScanCloud processing failed: {json.dumps(info)}")

    raise RuntimeError(f"Timed out after {max_wait}s waiting for processing")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Upload to OpenScanCloud and download result")
    parser.add_argument("--images", required=True, help="Directory containing scan images")
    parser.add_argument("--output", default="/mnt/scandata/results", help="Output directory for result")
    parser.add_argument("--config", required=True, help="Path to .env config file")
    parser.add_argument("--project", help="Project name (auto-generated if omitted)")
    parser.add_argument("--poll-interval", type=int, default=60, help="Polling interval in seconds")
    args = parser.parse_args()

    try:
        result = upload_and_process(
            args.images, args.output, args.config,
            project_name=args.project, poll_interval=args.poll_interval
        )
        print(f"SUCCESS: {result}")
    except RuntimeError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
