# Installation and First Launch

**English** · [中文说明 →](zh-CN/INSTALL.md)

## 0. Choose Your Path

| Your Situation | Which Path to Take | Time Required |
|---|---|---|
| Just want to use it, no coding | **A. Self-Contained Package** (`SpeakTosi_Mini_*.zip` from Release) | Unzip 10 mins + Cold start 1~3 mins |
| Want to modify code/submit PRs | **B. Source Code + Manual Dependencies** | Install dependencies 30~60 mins |
| Package broken/Moved/Changed PC | **C. Self-Healing Repair** | 1 min |

---

## A. Self-Contained Package (Recommended)

```text
1) Unzip to an English, space-free path, e.g., D:\SpeakTosi
   ⚠️ Do NOT put it in C:\Program Files, Desktop (might contain Chinese/spaces), or OneDrive sync folders
2) Disk space: ~25 GB after unzipping, plus leave 20 GB for sessions/logs/cache
3) Double-click start_speaktosi.vbs        ← Will silently perform "path self-healing" on first launch
   For troubleshooting: Double-click SETUP.bat to see self-check output first, then start_speaktosi.vbs
4) The [First-Time Configuration] wizard pops up on first launch:
   Select platform → Paste your cloud Key → "Save and Test" (保存并测试) → Done when it shows "Cloud connected" (云端已连通)
5) Wait for the tray icon to turn green (cold start 1~3 mins to load avatar + clone voice model) → Start chatting
```

To change the Key or running mode later: **Right-click the tray icon from the desktop teacher window → Settings**.

## B. Source Code

```bash
# 1) Frontend
cd app && npm install && cd ..

# 2) Avatar Engine (8011)
python -m venv engine/venv
engine/venv/Scripts/pip install -r engine/requirements.txt

# 3) Backend (8765)
python -m venv app/backend/.venv
app/backend/.venv/Scripts/pip install -r app/backend/requirements-backend.txt

# 4) Models and Avatar Assets → See docs/ASSETS.md (binaries not in repo)
# 5) Clone Voice Environment (18091/18092) → See docs/ASSETS.md Section 4
```

After installation, double-click `SETUP.bat` to self-check, then run `start_speaktosi.vbs`.

## C. Self-Healing for Moving / Changing PCs

All absolute paths in the package are **automatically rewritten to the current root directory on the first launch** (implemented by `tools/scripts/first_run_setup.py` → `portable_paths.py`).
So you can freely change the drive letter, move the directory, or copy the entire package to someone else, and it will just work. To trigger it manually:

```bat
SETUP.bat      :: Under the hood: runtime\py312\python.exe tools\scripts\first_run_setup.py
```

`first_run_setup.py` does four things:
1. Fixes the `home` path in `pyvenv.cfg` for `engine/venv` and `app/backend/.venv` (pointing to the Python **inside the package**—this is the pitfall that breaks environments when moving).
2. Rewrites `C:\Speaktosi` to the current root across the tree (skipping venv/node_modules/models, etc.).
3. Generates `config/runtime_mode.json` (auto mode).
4. Self-checks if critical files exist.

---

## First-Time Configuration Wizard (Mandatory)

The Cloud Key is the only thing you must provide (conversations and translations run on cloud LLMs; everything else runs locally).

- How to open: Pops up automatically on first launch; or via Tray → Settings → "Cloud Token" at the top.
- Flow: "Select platform → Paste Key → Save and Test". It's only successful when it shows "Cloud connected".
- The Key is saved in `app/backend/.env` (a local file, not in the package, not in git). Manual method (for troubleshooting):
  Copy `config/student_keys.example.env` → `app/backend/.env` and fill it in.

## How to Verify It's Really Alive After Starting

| What to Check | Normal Behavior |
|---|---|
| Tray Icon | Turns green |
| `engine/data/runtime_mode_active.json` | Has content (`mode` = `full` or `lite`) |
| Ports | `8011` (Engine), `18091` (Voice), `8765` (Backend), `18999` (Watchdog Lock) are all LISTENING |
| Backend | `http://127.0.0.1:8765/health` returns JSON |
| Teacher List | `http://127.0.0.1:8765/avatars` has exactly two: `coach_arthur`, `coach_mia` |

> ⚠️ `tools/verify_vram.py` and `tools/verify_e2e_ui.py` are **maintainer tools**: they actively stop the watchdog and kill services to test VRAM/end-to-end, which is not suitable for daily health checks (see [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md)). For daily checks, just use `SETUP.bat` and the ports/API endpoints in the table above.

## Uninstall

Just delete the entire directory. Except for the sessions/logs inside the directory, it does not write to the registry or install services.
