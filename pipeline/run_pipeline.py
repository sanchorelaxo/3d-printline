#!/usr/bin/env python3
"""
Master pipeline orchestrator for the 3d-printline photogrammetry-to-print workflow.
Runs on the Raspberry Pi (OpenClaw host).

Steps:
  0. Discover OpenScan Mini on network
  1. Fetch scan images via Samba
  2. Upload to OpenScanCloud, poll, download result
  3. SCP model to laptop
  4. Run Blender headless decimation on laptop (Docker)
  5. Slice STL → 3MF on laptop (OrcaSlicer)
  6. Upload 3MF + trigger print on Bambu X1-Carbon
  7. Notify via Telegram (OpenClaw)
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PIDFILE = os.path.join(SCRIPT_DIR, ".pipeline.pid")
VERBOSE = False


def enforce_singleton():
    """Kill ALL other run_pipeline.py processes, then write our PID."""
    my_pid = os.getpid()
    my_ppid = os.getppid()
    safe_pids = {my_pid, my_ppid}

    # Find all python run_pipeline processes
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python3.*run_pipeline\\.py"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                pid = int(line.strip())
                if pid not in safe_pids:
                    print(f"Killing previous pipeline PID {pid}...")
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
            time.sleep(0.5)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass

    with open(PIDFILE, "w") as f:
        f.write(str(my_pid))


def cleanup_pidfile():
    """Remove PID file on exit."""
    try:
        if os.path.exists(PIDFILE):
            with open(PIDFILE) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(PIDFILE)
    except OSError:
        pass


def vlog(msg):
    """Print message only when verbose mode is enabled."""
    if VERBOSE:
        print(f"  [verbose] {msg}")


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


OPENCLAW_BIN = "/home/sanchobot/.npm-global/bin/openclaw"
TELEGRAM_TARGET = ""


def notify(message, is_error=False):
    """Send notification via OpenClaw Telegram (gracefully skips if not configured)."""
    prefix = "❌" if is_error else "ℹ️"
    full_msg = f"{prefix} 3d-printline: {message}"
    print(full_msg)

    target = TELEGRAM_TARGET
    if not target:
        vlog("notify skipped: TELEGRAM_TARGET not set")
        return

    try:
        ocbin = os.environ.get("OPENCLAW_BIN", OPENCLAW_BIN)
        cmd = [ocbin, "message", "send", "--channel", "telegram",
               "--target", target, "--message", full_msg]
        vlog(f"notify → {target}")
        result = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
        vlog(f"notify rc={result.returncode}")
        if result.returncode != 0:
            vlog(f"notify stderr: {result.stderr[:200]}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        vlog(f"notify error: {e}")


def run_step(step_name, func, *args, **kwargs):
    """Run a pipeline step with error handling and timing."""
    print(f"\n{'='*60}")
    print(f"STEP: {step_name}")
    print(f"{'='*60}")
    start = time.time()
    try:
        result = func(*args, **kwargs)
        elapsed = time.time() - start
        print(f"✓ {step_name} completed in {elapsed:.0f}s")
        return result
    except Exception as e:
        elapsed = time.time() - start
        error_msg = f"{step_name} failed after {elapsed:.0f}s: {e}"
        print(f"✗ {error_msg}", file=sys.stderr)
        notify(error_msg, is_error=True)
        raise


def step_discover(config):
    """Step 0: Discover OpenScan Mini."""
    from discover import discover_openscan, check_openscan_samba

    host = config.get("OPENSCAN_HOST", "openscan.local")
    vlog(f"Resolving {host}...")
    ip = discover_openscan(host)
    if not ip:
        raise RuntimeError(f"OpenScan not found at {host}")
    vlog(f"OpenScan IP: {ip}")
    samba_ok = check_openscan_samba(ip)
    vlog(f"Samba port 445 open: {samba_ok}")
    if not samba_ok:
        raise RuntimeError(f"Samba not accessible on {ip}")
    return ip


def step_fetch(config, openscan_ip, project_name):
    """Step 1: Fetch scan images from OpenScan Mini."""
    from scan_fetch import fetch_scan, get_latest_scan

    scandata_dir = config.get("SCANDATA_DIR", "/mnt/scandata")
    output_dir = os.path.join(scandata_dir, "scans")
    smb_user = config.get("OPENSCAN_SMB_USER", "pi")
    smb_pass = config.get("OPENSCAN_SMB_PASS", "raspberry")

    if not project_name:
        project_name = get_latest_scan(openscan_ip, smb_user, smb_pass)
        if not project_name:
            raise RuntimeError("No scans found on OpenScan")
        print(f"Auto-detected latest scan: {project_name}")

    vlog(f"Fetching from //{openscan_ip}/PiShare/OpenScan/scans/{project_name}")
    vlog(f"Output dir: {output_dir}")
    files = fetch_scan(openscan_ip, project_name, output_dir, smb_user, smb_pass)
    if not files:
        raise RuntimeError("No images downloaded")
    vlog(f"Fetched {len(files)} files, first: {files[0]}")

    label = project_name.replace(".zip", "")
    image_dir = os.path.join(output_dir, label)
    vlog(f"image_dir: {image_dir}")
    return image_dir, project_name


def step_cloud_upload(config, image_dir, project_name):
    """Step 2: Upload to OpenScanCloud and download result."""
    from cloud_upload import upload_and_process

    scandata_dir = config.get("SCANDATA_DIR", "/mnt/scandata")
    output_dir = os.path.join(scandata_dir, "results")
    poll_interval = int(config.get("CLOUD_POLL_INTERVAL", "60"))
    env_path = config["_env_path"]

    vlog(f"image_dir: {image_dir}")
    vlog(f"output_dir: {output_dir}")
    vlog(f"poll_interval: {poll_interval}s")
    notify(f"Uploading scan '{project_name}' to OpenScanCloud...")
    result_path = upload_and_process(
        image_dir, output_dir, env_path,
        project_name=project_name, poll_interval=poll_interval
    )
    vlog(f"Result file: {result_path}")
    notify(f"OpenScanCloud processing complete for '{project_name}'")
    return result_path


def step_transfer_to_laptop(config, result_path):
    """Step 3: SCP the model file to the laptop."""
    laptop_host = config.get("LAPTOP_HOST", "192.168.1.23")
    laptop_user = config.get("LAPTOP_USER", "rjodouin")
    laptop_dir = config.get("LAPTOP_PIPELINE_DIR", "/home/rjodouin/3d-pipeline")
    remote_models_dir = f"{laptop_dir}/models"

    filename = os.path.basename(result_path)
    remote_path = f"{laptop_user}@{laptop_host}:{remote_models_dir}/{filename}"

    # Ensure remote directory exists
    subprocess.run(
        ["ssh", f"{laptop_user}@{laptop_host}", f"mkdir -p {remote_models_dir}"],
        timeout=10, check=True
    )

    print(f"SCP: {result_path} → {remote_path}")
    vlog(f"File size: {os.path.getsize(result_path) / 1e6:.1f} MB")
    subprocess.run(
        ["scp", result_path, remote_path],
        timeout=300, check=True
    )
    vlog(f"SCP complete")
    return f"{remote_models_dir}/{filename}"


def step_decimate(config, remote_model_path):
    """Step 4: Run Blender headless decimation on the laptop via SSH."""
    laptop_host = config.get("LAPTOP_HOST", "192.168.1.23")
    laptop_user = config.get("LAPTOP_USER", "rjodouin")
    laptop_dir = config.get("LAPTOP_PIPELINE_DIR", "/home/rjodouin/3d-pipeline")
    ratio = config.get("DECIMATE_RATIO", "0.5")

    filename = os.path.basename(remote_model_path)
    name_base = os.path.splitext(filename)[0]
    output_stl = f"{laptop_dir}/models/{name_base}_decimated.stl"

    docker_cmd = (
        f"docker run --rm "
        f"-v {laptop_dir}:/data "
        f"nytimes/blender:latest blender -b -noaudio "
        f"-P /data/scripts/decimate_and_export.py "
        f"-- --ratio {ratio} "
        f"--inm /data/models/{filename} "
        f"--outm /data/models/{name_base}_decimated.stl"
    )

    vlog(f"Docker cmd: {docker_cmd}")
    print(f"Running Blender decimation on laptop...")
    result = subprocess.run(
        ["ssh", f"{laptop_user}@{laptop_host}", docker_cmd],
        capture_output=True, text=True, timeout=600
    )

    if VERBOSE:
        print(result.stdout)
        if result.stderr:
            print(f"  [verbose] stderr: {result.stderr[:500]}")
    else:
        # Print just the key lines
        for line in result.stdout.split("\n"):
            if any(k in line for k in ["Original", "Final", "Exported", "Done", "ratio"]):
                print(f"  {line}")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Blender decimation failed: exit code {result.returncode}")

    # Also copy to Manyfold library
    try:
        cp_cmd = f"cp {laptop_dir}/models/{filename} {laptop_dir}/models/{name_base}_decimated.stl {laptop_dir}/models/ 2>/dev/null || true"
        subprocess.run(
            ["ssh", f"{laptop_user}@{laptop_host}", cp_cmd],
            timeout=30
        )
    except Exception:
        pass

    notify(f"Mesh decimated: {name_base} (ratio={ratio})")
    return output_stl


def step_slice_and_print(config, remote_stl_path):
    """Step 5+6: Slice and print on the laptop."""
    laptop_host = config.get("LAPTOP_HOST", "192.168.1.23")
    laptop_user = config.get("LAPTOP_USER", "rjodouin")
    laptop_dir = config.get("LAPTOP_PIPELINE_DIR", "/home/rjodouin/3d-pipeline")
    env_path = config["_env_path"]

    # SCP the .env to laptop temporarily for the slice_and_print script
    remote_env = f"{laptop_dir}/.env"
    subprocess.run(
        ["scp", env_path, f"{laptop_user}@{laptop_host}:{remote_env}"],
        timeout=30, check=True
    )

    ssh_cmd = (
        f"cd {laptop_dir} && "
        f"python3 {laptop_dir}/scripts/slice_and_print.py "
        f"--stl {remote_stl_path} "
        f"--config {remote_env}"
    )

    vlog(f"SSH cmd: {ssh_cmd}")
    notify("Starting slice and print...")
    result = subprocess.run(
        ["ssh", f"{laptop_user}@{laptop_host}", ssh_cmd],
        capture_output=True, text=True, timeout=600
    )

    if VERBOSE:
        print(result.stdout)
        if result.stderr:
            print(f"  [verbose] stderr: {result.stderr[:500]}")
    else:
        for line in result.stdout.split("\n"):
            if any(k in line for k in ["Slic", "Upload", "MQTT", "Print", "PRINTER"]):
                print(f"  {line}")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Slice and print failed: exit code {result.returncode}")

    # Parse JSON output from last line
    for line in reversed(result.stdout.strip().split("\n")):
        try:
            print_result = json.loads(line)
            remaining = print_result.get("remaining_minutes", "unknown")
            notify(f"✅ Print started! ETA: {remaining} minutes")
            return print_result
        except (json.JSONDecodeError, ValueError):
            continue

    notify("Print command sent (could not parse status)")
    return {"status": "sent"}


def run_pipeline(env_path, project_name=None, verbose=False):
    """Execute the full pipeline."""
    global VERBOSE, TELEGRAM_TARGET
    VERBOSE = verbose

    import atexit
    enforce_singleton()
    atexit.register(cleanup_pidfile)
    vlog(f"Singleton enforced, PID {os.getpid()}")

    config = load_env(env_path)
    config["_env_path"] = env_path
    TELEGRAM_TARGET = config.get("TELEGRAM_TARGET", "")
    vlog(f"Config loaded: {len(config)} keys")
    vlog(f"Env path: {env_path}")
    vlog(f"Telegram target: {'(not set)' if not TELEGRAM_TARGET else TELEGRAM_TARGET}")

    pipeline_start = time.time()
    notify(f"Pipeline starting{f' for project: {project_name}' if project_name else ''}...")

    try:
        # Step 0: Discover
        openscan_ip = run_step("Discover OpenScan", step_discover, config)

        # Step 1: Fetch
        image_dir, project_name = run_step("Fetch Scan", step_fetch, config, openscan_ip, project_name)

        # Step 2: Cloud upload + process
        result_path = run_step("Cloud Upload & Process", step_cloud_upload, config, image_dir, project_name)

        # Step 3: Transfer to laptop
        remote_model = run_step("Transfer to Laptop", step_transfer_to_laptop, config, result_path)

        # Step 4: Decimate
        remote_stl = run_step("Blender Decimation", step_decimate, config, remote_model)

        # Step 5+6: Slice and print
        print_result = run_step("Slice & Print", step_slice_and_print, config, remote_stl)

        elapsed = time.time() - pipeline_start
        notify(f"✅ Pipeline complete for '{project_name}' in {elapsed / 60:.1f} minutes!")

    except Exception as e:
        elapsed = time.time() - pipeline_start
        notify(f"Pipeline failed after {elapsed / 60:.1f} minutes: {e}", is_error=True)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="3d-printline pipeline orchestrator")
    parser.add_argument("--config", required=True, help="Path to .env config file")
    parser.add_argument("--project", help="Scan project name (auto-detect if omitted)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    args = parser.parse_args()

    run_pipeline(args.config, args.project, verbose=args.verbose)
