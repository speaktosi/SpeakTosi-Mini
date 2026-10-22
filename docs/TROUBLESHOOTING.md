# Troubleshooting Guide

**English** · [中文说明 →](zh-CN/TROUBLESHOOTING.md)

Check this first: **What color is the tray icon?**
Green = Full stack is ready; Gray/Red = The watchdog failed to pull up all services (common reasons: ports are occupied or VRAM is insufficient).

---

## 1. Port Occupied (The most common "Won't Start" issue)

Occupied ports: `8011` (Engine), `18091`, `18092` (Voice), `18999` (Watchdog Lock), `8765` (Backend).

```bat
netstat -ano | findstr ":8011 :8765 :18091 :18999"
```

```powershell
# Look up who it is by PID
Get-Process -Id <PID> | Select-Object ProcessName, Path
```

Solution: Close the occupying program (an old instance of this program, another AI workbench, browser debugging ports, etc.), or
starting a second set in **another directory** will cause them to fight for ports — you can only run one instance per machine.

> Known scenario: If you have another local AI service binding 8011/18091 running (or an instance that didn't exit cleanly last time),
> the newly started service will repeatedly be "pulled up → fail → pulled up again" by the watchdog. Use `taskkill /PID <PID> /T /F` first.

## 2. Insufficient VRAM / Repeated Restarts

```bat
nvidia-smi
```

- The Mini version **requires at least 12 GB of VRAM**: MuseTalk ≈ 9 GB + Single TTS ≈ 2 GB.
- Before starting, please close things that consume VRAM: another image generation/local LLM workbench, games, or heavily hardware-accelerated browser pages.
- You can see the measured value for the mode in `engine/data/runtime_mode_active.json`; the settings page lets you manually select `full` / `lite`.
- For 12 GB cards, it is recommended to stay on `lite` (single TTS worker). `full` requires ≥16 GB.
- If it shows "even `ultra` is not enough": The Mini version has no cartoon engine to fall back to; `ultra` automatically runs as `lite`,
  so when VRAM is below 11 GB, **there are no fallback modes** — you can only reduce other things occupying VRAM.

## 3. Voice Service Judged "Dead" and Repeatedly Killed (Port is open, but no sound)

The way the watchdog checks TTS health is by **actually synthesizing a sentence** (only probing the port can be fooled by "port is open but model hasn't finished loading").
The voice used for this criterion must exist in `engine/data/voice_registry.json`.

- The Mini version has fixed the probe voice to `arthur` (this must be synced when trimming voices, otherwise it causes an endless loop). If you trimmed voices yourself,
  remember to synchronize the `'voice'` field in `deep_tts_ok()` inside `engine/aiteacher_watchdog.py`.
- Manually verify that the voice can actually synthesize:

```bat
curl -X POST http://127.0.0.1:18091/v1/audio/speech ^
     -H "Content-Type: application/json" ^
     -d "{\"input\":\"Hello, this is a voice test.\",\"voice\":\"arthur\",\"response_format\":\"wav\"}" ^
     --output test.wav
```

Getting `test.wav` (>1 KB) = The voice service is actually alive.

## 4. "Wrong Person" in the Teacher Window

- The teacher window goes through `engine/web/desktop.html`. When connecting, it sends a request **without the avatar parameter**,
  rendering the engine's **default boot slot** (Mini version = `coach_arthur`).
- If you want to change the person in the window: Switch the teacher in the Settings page (this will swap the persona and the voice together); the default slot is only defined in the watchdog service table.
- If the window is solid green/solid black: That is a legacy artifact of the **cartoon-style** rendering layer → The Mini version has no cartoon engine,
  please make sure you didn't manually add back the `--model viseme` service (it requires cartoon assets, which are not included in this version).

## 5. Crash After Moving (Changed Directory / Changed Drive Letter / Copied to Someone Else)

```bat
SETUP.bat          :: = runtime\py312\python.exe tools\scripts\first_run_setup.py
```

It will fix the `home` in `pyvenv.cfg` (pointing to the Python inside the package) and rewrite absolute paths across the whole tree.
**You must use the Python inside the package to run it**: `runtime\py312\python.exe`, do not use the system Python.

Manual traces: There will be full paths inside `engine/data/avatars/*/avator_info.json`, and the self-healing script will fix them all together.

## 6. venv Won't Start / `Fatal error in launcher` / Interpreter Points to Dev Machine

This is a classic "venv move" pitfall: the launcher hardcodes the Python path at the time of creation. The self-healing script will rewrite `pyvenv.cfg`;
if it still fails (after changing PCs, the base points to a non-existent path), rebuild it using the bundled Python:

```bat
runtime\py312\python.exe -m venv engine\venv
engine\venv\Scripts\pip.exe install -r engine\requirements.txt
runtime\py311\python.exe -m venv app\backend\.venv
app\backend\.venv\Scripts\pip.exe install -r app\backend\requirements-backend.txt
```

## 7. No Response to Chat / Stuck on "Thinking"

- Cloud Key not configured or invalid: Tray → Settings → "Cloud Token" → Save and Test.
- UI opens but the teacher doesn't speak: Check `app/backend/logs/` and the root `日志/` directory.
- `401 Unauthorized` = Key is invalid or the platform doesn't recognize this model name.

## 8. Maintainer Tools (Will interrupt services, don't use daily)

| Tool | What It Does | Note |
|---|---|---|
| `tools/verify_vram.py` | Stops watchdog → Kills services sequentially to measure VRAM delta → Restarts | Will **kill** currently running services |
| `tools/verify_e2e_ui.py` | End-to-end (including CDP reading the UI DOM) | Same as above, and will open Electron |
| `tools/scripts/live_opener.py` | Opens the page after waiting for ports to be ready | Read-only wait |

After running them, remember to let the watchdog pull the services back up (Tray "Start/Fix", or directly restart the program).

## 9. Still Not Working: Collect Information in This Order

```bat
SETUP.bat                                   :: Critical files / Path self-healing self-check
type engine\data\runtime_mode_active.json   :: Active mode + Measured VRAM/RAM
netstat -ano | findstr ":8011 :8765"        :: Who is listening on ports
nvidia-smi                                  :: VRAM usage
```

Then post the newest logs from `app/backend/logs/` together with the information.
