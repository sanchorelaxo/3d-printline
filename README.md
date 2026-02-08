# 3d-printline

Automated photogrammetry-to-print pipeline orchestrated by OpenClaw on a Raspberry Pi 5.

**Flow:** OpenScan Mini → OpenScanCloud → Blender headless decimation → OrcaSlicer → Bambu X1-Carbon LAN print → Telegram notification

## Network Architecture

| Device | Role | Hostname |
|--------|------|----------|
| Raspberry Pi 5 | OpenClaw host + orchestrator | `openclaw.local` |
| OpenScan Mini | 3D scanner (images via Samba) | `openscan.local` |
| Laptop | Blender Docker, OrcaSlicer, Manyfold | `sanchopop` |
| Bambu X1-Carbon | 3D printer (LAN mode, FTPS+MQTT) | dynamic IP |

## Pipeline Steps

1. **Scan Detection** — manual trigger via Telegram or auto-detect new scans on OpenScan Samba share
2. **Fetch Images** — copy JPGs from OpenScan Mini via Samba to Pi's 1TB drive
3. **Cloud Processing** — upload to OpenScanCloud API, poll until photogrammetry completes, download OBJ/GLB
4. **Mesh Decimation** — SSH to laptop, run Blender headless Docker with decimate script, export STL
5. **Slicing** — OrcaSlicer CLI on laptop converts STL → 3MF with X1C profile
6. **Print** — upload 3MF via FTPS, trigger print via MQTT on Bambu X1-Carbon
7. **Notify** — OpenClaw sends Telegram message with status at each step

## File Layout

### On Raspberry Pi (`/home/sanchobot/3d-printline/pipeline/`)

| File | Purpose |
|------|---------|
| `run_pipeline.py` | Master orchestrator — chains all steps |
| `discover.py` | mDNS discovery for OpenScan Mini |
| `scan_fetch.py` | Samba image fetch from scanner |
| `cloud_upload.py` | OpenScanCloud upload, poll, download |
| `scan_watcher.py` | Auto-detect mode — polls for new scans |
| `.env` | All credentials and config |

### On Laptop (`/home/rjodouin/3d-pipeline/`)

| File | Purpose |
|------|---------|
| `scripts/decimate_and_export.py` | Blender Python script for mesh decimation + STL export |
| `scripts/slice_and_print.py` | OrcaSlicer CLI + FTPS upload + MQTT print trigger |
| `scripts/bambu_discover.py` | SSDP/port scan to find Bambu printer IP |
| `docker-compose.yml` | Manyfold container |
| `OrcaSlicer.AppImage` | Slicer binary (symlinked to `/usr/local/bin/orca-slicer`) |

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
ssh sanchobot@openclaw.local
cd ~/3d-printline/pipeline
python3 scan_watcher.py --config .env
```

### Direct pipeline run

```bash
ssh sanchobot@openclaw.local
cd ~/3d-printline/pipeline
python3 run_pipeline.py --config .env --project "MyObject"
```

## Configuration

All config is in `.env` — edit on the Pi at `~/3d-printline/pipeline/.env`:

- `OSC_TOKEN` — OpenScanCloud API token
- `BAMBU_SERIAL` / `BAMBU_ACCESS_CODE` — printer LAN credentials
- `DECIMATE_RATIO` — mesh simplification (0.0–1.0, default 0.5)
- `SLICER_PROFILE` — OrcaSlicer profile name
- `LAPTOP_HOST` / `LAPTOP_USER` — SSH target

## Prerequisites

- SSH key auth: Pi → Laptop (passwordless)
- Docker running on laptop with `nytimes/blender:latest` pulled (Blender 3.3.1)
- OrcaSlicer installed on laptop (`/usr/local/bin/orca-slicer`)
- `smbclient` installed on Pi
- Bambu X1-Carbon in LAN mode with Micro SD card inserted
- OpenClaw with Telegram channel configured on Pi

## Dependencies

**Laptop:** `python3-paho-mqtt`, Docker, OrcaSlicer  
**Pi:** `smbclient`, `cifs-utils`, `python3-requests`
