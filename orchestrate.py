#!/usr/bin/env python3
"""
Laptop-side orchestrator for the 3d-printline photogrammetry-to-print pipeline.
Runs on the laptop. SSHes into the Pi for Pi-side work; never exposes
the laptop to inbound SSH.

Flow:
  1. SSH → Pi: run_pipeline.py (discover, fetch, cloud upload)  → result path
  2. SCP ← Pi: pull the model file to local ~/3d-pipeline/models/
  3. Local: Blender Docker mesh decimation
  4. Local: OrcaSlicer slice + Bambu FTPS/MQTT print
  5. SSH → Pi: openclaw Telegram notification
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PIDFILE = os.path.join(SCRIPT_DIR, ".orchestrate.pid")
VERBOSE = False

# Defaults — overridden by .env
PI_HOST = "192.168.1.134"
PI_USER = "sanchobot"
PI_PIPELINE_DIR = "/home/sanchobot/3d-printline/pipeline"
LAPTOP_PIPELINE_DIR = os.path.expanduser("~/3d-pipeline")
OPENCLAW_BIN = "/home/sanchobot/.npm-global/bin/openclaw"
TELEGRAM_TARGET = ""


# ── helpers ──────────────────────────────────────────────────────────

def vlog(msg):
    if VERBOSE:
        print(f"  [verbose] {msg}")


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


def enforce_singleton():
    my_pid = os.getpid()
    my_ppid = os.getppid()
    safe_pids = {my_pid, my_ppid}
    try:
        result = subprocess.run(
            ["pgrep", "-f", "python3.*orchestrate\\.py"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                pid = int(line.strip())
                if pid not in safe_pids:
                    print(f"Killing previous orchestrator PID {pid}...")
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
    try:
        if os.path.exists(PIDFILE):
            with open(PIDFILE) as f:
                if f.read().strip() == str(os.getpid()):
                    os.remove(PIDFILE)
    except OSError:
        pass


def ssh_pi(cmd, timeout=60, check=True):
    """Run a command on the Pi via SSH."""
    full = ["ssh", f"{PI_USER}@{PI_HOST}", cmd]
    vlog(f"ssh → {cmd[:120]}")
    return subprocess.run(full, capture_output=True, text=True, timeout=timeout, check=check)


def notify(message, is_error=False):
    """Send Telegram notification via Pi's OpenClaw."""
    prefix = "❌" if is_error else "ℹ️"
    full_msg = f"{prefix} 3d-printline: {message}"
    print(full_msg)
    if not TELEGRAM_TARGET:
        vlog("notify skipped: TELEGRAM_TARGET not set")
        return
    try:
        cmd = (f"{OPENCLAW_BIN} message send --channel telegram "
               f"--target {TELEGRAM_TARGET} --message '{full_msg}'")
        r = ssh_pi(cmd, timeout=30, check=False)
        vlog(f"notify rc={r.returncode}")
        if r.returncode != 0:
            vlog(f"notify stderr: {r.stderr[:200]}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        vlog(f"notify error: {e}")


def run_step(step_name, func, *args, **kwargs):
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


# ── steps ────────────────────────────────────────────────────────────

def step_pi_worker(config, project_name):
    """SSH into Pi and run steps 0-2. Returns the remote result path on Pi."""
    pi_env = f"{PI_PIPELINE_DIR}/.env"
    verbose_flag = "-v" if VERBOSE else ""
    project_flag = f"--project '{project_name}'" if project_name else ""

    cmd = (f"cd {PI_PIPELINE_DIR} && PYTHONUNBUFFERED=1 python3 run_pipeline.py "
           f"--config {pi_env} {verbose_flag} {project_flag}")

    vlog(f"Pi worker cmd: {cmd}")
    print("Running Pi-side worker (discover → fetch → cloud upload)...")

    # Stream output live
    proc = subprocess.Popen(
        ["ssh", f"{PI_USER}@{PI_HOST}", cmd],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True
    )

    result_path = None
    for line in proc.stdout:
        line = line.rstrip()
        print(f"  [pi] {line}")
        if line.startswith("RESULT_PATH="):
            result_path = line.split("=", 1)[1]

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Pi worker exited with code {proc.returncode}")
    if not result_path:
        raise RuntimeError("Pi worker did not output RESULT_PATH")

    vlog(f"Remote result: {result_path}")
    return result_path


def step_pull_from_pi(config, remote_result_path):
    """SCP the model file from Pi to local laptop. Extracts zip if needed."""
    import zipfile

    local_models = os.path.join(LAPTOP_PIPELINE_DIR, "models")
    os.makedirs(local_models, exist_ok=True)

    filename = os.path.basename(remote_result_path)
    local_path = os.path.join(local_models, filename)
    remote = f"{PI_USER}@{PI_HOST}:{remote_result_path}"

    print(f"SCP: {remote} → {local_path}")
    subprocess.run(
        ["scp", remote, local_path],
        timeout=300, check=True
    )
    size_mb = os.path.getsize(local_path) / 1e6
    vlog(f"Downloaded {size_mb:.1f} MB")

    # If it's a zip, extract and find the OBJ/GLB model inside
    if filename.endswith(".zip") and zipfile.is_zipfile(local_path):
        print("Extracting zip...")
        with zipfile.ZipFile(local_path, 'r') as zf:
            zf.extractall(local_models)
            model_exts = ('.obj', '.glb', '.stl', '.ply')
            for name in zf.namelist():
                if name.lower().endswith(model_exts):
                    extracted = os.path.join(local_models, name)
                    print(f"Extracted model: {name}")
                    return extracted
        raise RuntimeError(f"No model file found in {filename}")

    return local_path


def step_decimate(config, model_path):
    """Run Blender headless decimation locally via Docker."""
    ratio = config.get("DECIMATE_RATIO", "0.5")
    filename = os.path.basename(model_path)
    name_base = os.path.splitext(filename)[0]
    output_stl = os.path.join(LAPTOP_PIPELINE_DIR, "models", f"{name_base}_decimated.stl")

    docker_cmd = [
        "docker", "run", "--rm",
        "-v", f"{LAPTOP_PIPELINE_DIR}:/data",
        "nytimes/blender:latest", "blender", "-b", "-noaudio",
        "-P", "/data/scripts/decimate_and_export.py",
        "--", "--ratio", str(ratio),
        "--inm", f"/data/models/{filename}",
        "--outm", f"/data/models/{name_base}_decimated.stl"
    ]

    vlog(f"Docker cmd: {' '.join(docker_cmd)}")
    print("Running Blender decimation locally...")
    result = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=600)

    if VERBOSE:
        print(result.stdout)
        if result.stderr:
            print(f"  [verbose] stderr: {result.stderr[:500]}")
    else:
        for line in result.stdout.split("\n"):
            if any(k in line for k in ["Original", "Final", "Exported", "Done", "ratio"]):
                print(f"  {line}")
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"Blender decimation failed: exit code {result.returncode}")

    notify(f"Mesh decimated: {name_base} (ratio={ratio})")
    return output_stl


def step_slice_and_print(config, stl_path):
    """Run OrcaSlicer + Bambu print locally."""
    scripts_dir = os.path.join(LAPTOP_PIPELINE_DIR, "scripts")
    config_path = os.path.join(LAPTOP_PIPELINE_DIR, ".env")

    # Write a local .env for slice_and_print.py
    env_path = config["_env_path"]
    if env_path != config_path:
        subprocess.run(["cp", env_path, config_path], check=True)

    cmd = [
        "python3", os.path.join(scripts_dir, "slice_and_print.py"),
        "--stl", stl_path,
        "--config", config_path
    ]

    vlog(f"Slice cmd: {' '.join(cmd)}")
    notify("Starting slice and print...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

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


# ── main ─────────────────────────────────────────────────────────────

def run(env_path, project_name=None, verbose=False):
    global VERBOSE, TELEGRAM_TARGET, PI_HOST, PI_USER, PI_PIPELINE_DIR
    global LAPTOP_PIPELINE_DIR, OPENCLAW_BIN
    VERBOSE = verbose

    import atexit
    enforce_singleton()
    atexit.register(cleanup_pidfile)
    vlog(f"Singleton enforced, PID {os.getpid()}")

    config = load_env(env_path)
    config["_env_path"] = env_path

    # Load config into globals
    PI_HOST = config.get("PI_HOST", PI_HOST)
    PI_USER = config.get("PI_USER", PI_USER)
    PI_PIPELINE_DIR = config.get("PI_PIPELINE_DIR",
                                  f"/home/{PI_USER}/3d-printline/pipeline")
    LAPTOP_PIPELINE_DIR = config.get("LAPTOP_PIPELINE_DIR", LAPTOP_PIPELINE_DIR)
    OPENCLAW_BIN = config.get("OPENCLAW_BIN", OPENCLAW_BIN)
    TELEGRAM_TARGET = config.get("TELEGRAM_TARGET", "")

    vlog(f"Config: {len(config)} keys")
    vlog(f"Pi: {PI_USER}@{PI_HOST}:{PI_PIPELINE_DIR}")
    vlog(f"Laptop dir: {LAPTOP_PIPELINE_DIR}")
    vlog(f"Telegram target: {TELEGRAM_TARGET or '(not set)'}")

    pipeline_start = time.time()
    notify(f"Pipeline starting{f' for project: {project_name}' if project_name else ''}...")

    try:
        # Steps 0-2: Pi-side (discover, fetch, cloud upload)
        remote_result = run_step("Pi Worker (discover → cloud)",
                                 step_pi_worker, config, project_name)

        # Step 3: Pull result from Pi
        local_model = run_step("Pull from Pi",
                               step_pull_from_pi, config, remote_result)

        # Step 4: Blender decimation (local Docker)
        local_stl = run_step("Blender Decimation",
                             step_decimate, config, local_model)

        # Step 5+6: Slice and print (local)
        print_result = run_step("Slice & Print",
                                step_slice_and_print, config, local_stl)

        elapsed = time.time() - pipeline_start
        notify(f"✅ Pipeline complete in {elapsed / 60:.1f} minutes!")

    except Exception as e:
        elapsed = time.time() - pipeline_start
        notify(f"Pipeline failed after {elapsed / 60:.1f} minutes: {e}", is_error=True)
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="3d-printline orchestrator (runs on laptop)")
    parser.add_argument("--config", required=True,
                        help="Path to .env config file")
    parser.add_argument("--project",
                        help="Scan project name (auto-detect if omitted)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose output")
    args = parser.parse_args()

    run(args.config, args.project, verbose=args.verbose)
