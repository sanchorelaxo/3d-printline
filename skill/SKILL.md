---
name: 3d-printline
description: Automated photogrammetry-to-print pipeline. Trigger with "process my scan" or "scan and print". Fetches images from OpenScan Mini, uploads to OpenScanCloud for photogrammetry, decimates mesh in Blender headless, slices with OrcaSlicer, and prints on Bambu X1-Carbon via LAN. Sends Telegram status at each stage.
---

# 3d-printline: Photogrammetry Pipeline

## Overview

Automates the full workflow from 3D scanning to 3D printing:
1. Fetch scan images from OpenScan Mini (via Samba)
2. Upload to OpenScanCloud for photogrammetry processing
3. Decimate the resulting mesh in Blender headless (Docker on laptop)
4. Slice the STL with OrcaSlicer on the laptop
5. Upload and print on Bambu X1-Carbon via LAN (FTPS + MQTT)
6. Notify via Telegram at each stage

## Trigger phrases

- "process my scan" — process the latest scan
- "process my scan [project_name]" — process a specific scan
- "scan and print" — same as above
- "start printline" — same as above
- "watch for scans" — start auto-detect watcher mode

## Configuration

All config lives in `.env` in the skill directory. Key values:
- `OSC_TOKEN` — OpenScanCloud API token
- `BAMBU_SERIAL` / `BAMBU_ACCESS_CODE` — printer credentials
- `DECIMATE_RATIO` — mesh simplification ratio (0.0–1.0)
- `SLICER_PROFILE` — OrcaSlicer profile name
- `LAPTOP_HOST` / `LAPTOP_USER` — SSH target for laptop steps

## Workflow

### Manual trigger (Mode A)

When the user says "process my scan":

1. Run the pipeline script:
   ```bash
   cd /home/pi/3d-printline/pipeline
   python3 run_pipeline.py --config .env
   ```
   Or with a specific project name:
   ```bash
   python3 run_pipeline.py --config .env --project "MyObject"
   ```

2. The script handles all steps automatically and sends Telegram notifications at each stage.

3. If any step fails, the script stops and sends an error notification with the failed step name and error details.

### Auto-detect mode (Mode B)

When the user says "watch for scans":

1. Run the watcher:
   ```bash
   cd /home/pi/3d-printline/pipeline
   python3 scan_watcher.py --config .env
   ```

2. The watcher polls the OpenScan Mini Samba share every 60 seconds for new scan folders.

3. If the OpenScan Mini is unreachable, it fails silently and retries on the next poll.

4. When a new scan is detected, it automatically triggers the full pipeline.

## Error handling

- Each pipeline step is wrapped with error handling
- On failure: send Telegram message with step name and error details
- The pipeline stops at the first failure — it does not continue to subsequent steps
- Transient network errors (OpenScan offline, laptop unreachable) are reported clearly

## Pipeline scripts location

On the Pi: `~/3d-printline/pipeline/`
- `run_pipeline.py` — master orchestrator
- `discover.py` — network discovery (OpenScan, Bambu)
- `scan_fetch.py` — Samba image fetch
- `cloud_upload.py` — OpenScanCloud upload/poll/download
- `scan_watcher.py` — auto-detect mode

On the laptop: `~/3d-pipeline/scripts/`
- `decimate_and_export.py` — Blender Python script (runs in nytimes/blender:latest Docker)
- `slice_and_print.py` — OrcaSlicer + FTPS + MQTT
- `bambu_discover.py` — Bambu printer IP discovery

## Prerequisites

- SSH key from Pi to laptop must be configured (passwordless)
- Docker must be running on the laptop
- OrcaSlicer must be installed on the laptop
- Bambu X1-Carbon must be in LAN mode with SD card inserted
- `smbclient` must be installed on the Pi
