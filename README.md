# 3d-printline

Automated photogrammetry-to-print pipeline. Laptop orchestrates; Pi handles scanning + cloud processing; no inbound SSH to the laptop.

**Flow:** OpenScan Mini → Pi (fetch + cloud upload) → Laptop (decimate + slice + print) → Telegram notification

## Network Architecture

| Device | Role | Hostname |
|--------|------|----------|
| Laptop | **Orchestrator** — Blender Docker, OrcaSlicer, Bambu print | `your-laptop` |
| Raspberry Pi 5 | OpenClaw host + Pi worker (scan fetch, cloud upload) | `openclaw.local` |
| OpenScan Mini | 3D scanner (images via Samba) | `openscan.local` |
| Bambu X1-Carbon | 3D printer (LAN mode, FTPS+MQTT) | dynamic IP |

**SSH direction:** Laptop → Pi only. The Pi never initiates connections to the laptop.

## Pipeline Steps

1. **Scan Detection** — manual trigger or auto-detect new scans on OpenScan Samba share
2. **Fetch Images** (Pi) — copy JPGs from OpenScan Mini via Samba to Pi's 1TB drive
3. **Cloud Processing** (Pi) — upload to OpenScanCloud API, poll until photogrammetry completes, download OBJ/GLB
4. **Pull Result** (Laptop) — SCP model from Pi to laptop
5. **Mesh Decimation** (Laptop) — Blender headless Docker, decimate + export STL
6. **Slicing** (Laptop) — OrcaSlicer CLI converts STL → 3MF with X1C profile
7. **Print** (Laptop) — upload 3MF via FTPS, trigger print via MQTT on Bambu X1-Carbon
8. **Notify** — Telegram via Pi's OpenClaw at each step

## File Layout

### On Raspberry Pi (`~/3d-printline/pipeline/`)

| File | Purpose |
|------|---------|
| `run_pipeline.py` | Pi-side worker — steps 0-2 (discover, fetch, cloud) |
| `discover.py` | mDNS discovery for OpenScan Mini |
| `scan_fetch.py` | Samba image fetch from scanner |
| `cloud_upload.py` | OpenScanCloud upload, poll, download |
| `scan_watcher.py` | Auto-detect mode — polls for new scans |
| `.env` | All credentials and config |

### On Laptop (`~/Documents/git/3d-printline/`)

| File | Purpose |
|------|---------|
| `orchestrate.py` | **Main entry point** — laptop-side orchestrator (runs all steps) |
| `scripts/decimate_and_export.py` | Blender Python script for mesh decimation + STL export |
| `scripts/slice_and_print.py` | OrcaSlicer CLI + FTPS upload + MQTT print trigger |
| `scripts/bambu_discover.py` | SSDP/port scan to find Bambu printer IP |
| `docker-compose.yml` | Manyfold container |
| `profiles/x1c_*.json` | Flattened OrcaSlicer profiles for X1C |
| `models/` | Scan results, decimated STL, sliced 3MF (gitignored large files) |

### OpenClaw Skill (`~/.openclaw/workspace/skills/3d-printline/`)

| File | Purpose |
|------|---------|
| `SKILL.md` | Skill definition — trigger phrases and workflow |
| `.env` | Copy of pipeline config |

## Usage

### Manual trigger (via Telegram to OpenClaw)

> "process my scan"

or with a specific project name:

> "process my scan MyObject"

### Auto-detect mode

```bash
ssh piuser@openclaw.local
cd ~/3d-printline/pipeline
python3 scan_watcher.py --config .env
```

### Direct pipeline run (from laptop)

```bash
cd ~/Documents/git/3d-printline
python3 orchestrate.py --config pipeline/.env -v
# or with a specific project:
python3 orchestrate.py --config pipeline/.env --project "MyObject" -v
```

## Configuration

All config is in `.env` — edit on the Pi at `~/3d-printline/pipeline/.env`:

- `OSC_TOKEN` — OpenScanCloud API token
- `BAMBU_SERIAL` / `BAMBU_ACCESS_CODE` — printer LAN credentials
- `PI_HOST` / `PI_USER` — Raspberry Pi SSH target
- `TELEGRAM_TARGET` — Telegram chat ID for notifications
- `DECIMATE_RATIO` — mesh simplification (0.0–1.0, default 0.5)
- `SLICER_PROFILE` — OrcaSlicer profile name

## Prerequisites

- SSH key auth: Laptop → Pi (passwordless) — Pi never SSHes back
- Docker running on laptop with `nytimes/blender:latest` pulled (Blender 3.3.1)
- OrcaSlicer installed on laptop (`/usr/local/bin/orca-slicer`)
- `smbclient` installed on Pi
- Bambu X1-Carbon in LAN mode with Micro SD card inserted
- OpenClaw with Telegram channel configured on Pi

## Dependencies

**Laptop:** `python3-paho-mqtt`, Docker, OrcaSlicer  
**Pi:** `smbclient`, `cifs-utils`, `python3-requests`
