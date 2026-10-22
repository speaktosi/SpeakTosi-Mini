# Architecture Overview (1020mini)

**English** · [中文说明 →](zh-CN/ARCHITECTURE.md)

This document describes the actual topology running in the Mini version, what each process does, where the key files are, and **why it is trimmed this way**.
The numbers in this article come from per-process/per-source forensics during the 1020 packaging.

---

## 1. Process Topology

```text
┌────────────────────────────┐
│ Electron Desktop App        │  Teacher Window (overlay) / Settings / First-Run Wizard
│ app/main.js + renderer/     │  Tray: Status Light, Start/Fix, Mode Switch
└──────────────┬─────────────┘
               │ HTTP :8765  (The ONLY Interface Layer)
┌──────────────▼─────────────┐        ┌──────────────────────────┐
│ Backend FastAPI             │        │ Watchdog (Daemon)        │
│ app/backend/server.py       │        │ engine/aiteacher_watchdog│
│ /avatars /voices /config    │        │ Single-Instance Lock :18999│
│ /persona /chat /tts …       │  ←──→  │ Boot by Mode + Guard + Health Probe │
└──────────────┬─────────────┘        └───────┬──────────────────┘
               │                              │ Boots
               │                   ┌──────────┴───────────┐
               │                   │ ① Avatar Engine :8011 │  MuseTalk Realtime Lip-sync
               │                   │    (engine/app.py)    │  WebRTC Streaming
               │                   ├──────────────────────┤
               │                   │ ② Clone Voice :18091  │  CosyVoice2 Primary worker
               │                   │ ③ Clone Voice :18092  │  Secondary worker (only boots in full mode)
               │                   └──────────────────────┘
               │
        Dialogue text goes via Cloud LLM (OpenAI compatible protocol, using your own Key)
```

- **The UI only talks to 8765**; 8765 then decides which engine and which voice to use.
- **The Watchdog is the only one who "pulls up services"**: It probes VRAM/RAM to determine the runtime mode, and then guards the processes;
  If a service crashes, it will be pulled up again, so "manually killing the process" usually results in it being pulled right back up (see TROUBLESHOOTING for fixes).
- The engine itself is also an HTTP service: `:8011/` is the homepage (readiness indicator), `POST /offer` is the WebRTC connection endpoint.

## 2. Data Flow of a Single Turn

```text
Student speaks
  → Wake word / Keypress triggers recording
  → Speech Recognition (faster-whisper, bundled in the package)
  → Teacher Persona (soul) + Memory + Current Turn Text → Cloud LLM (qwen3.8-flash, etc.)  ← The only step hitting the network is "thinking content"
  → Teacher reply text
  → Cloned Voice Synthesis (CosyVoice2 :18091, voice = the teacher's preset, with speed/pitch calibration)
  → Audio sent into Avatar Engine :8011 → MuseTalk generates lip-sync frames
  → WebRTC pushes to the Electron window for playback (lip-sync + audio synchronized)
  → Dialogue saved to local disk (sessions/memories), optionally generating a SESSION REVIEW
```

**Teacher Switching = Soul Swapping**: `/teacher/apply` will rebuild `app/backend/config/persona.json` based on the selected teacher
(The System prompt is assembled from the `avatar/persona/<Teacher>.md` body + `Shared-core-adult.md` general rules),
and switches the voice preset at the same time; the engine side routes to the corresponding engine based on the `engine` field.

## 3. Key Files Lookup Table

| Concern | File | Description |
|---|---|---|
| Teacher Registry | `AVATARS` in `app/backend/server.py` | The Mini version only retains `coach_arthur` / `coach_mia` entries |
| Teacher Persona (soul) | `avatar/persona/CoachArthur.md`, `CoachMia.md`, `Shared-core-adult.md` | `[Field] header + Body`, the body is the main System prompt |
| Currently Active Soul | `app/backend/config/persona.json` | Rendered from the three files above (rewritten when switching teachers) |
| Factory Config | `app/backend/config/app.json` | Default teacher, voice, Cloud Key, wake word |
| Engine & Watchdog | `engine/aiteacher_watchdog.py` | Service table `SERVICES`, mode auto-detection, deep probing |
| Engine Entrypoint | `engine/app.py` | Loads avatar package, reads idle sequence, `/offer` for connection |
| Lip-sync Implementation | `engine/avatars/musetalk_avatar.py` | MuseTalk realistic lip-sync |
| Voice Registry | `engine/data/voice_registry.json` | preset → reference audio wav + reference text (Mini version: arthur / mia) |
| Speed & Voice Calibration | `engine/data/tts_calibration.json` | Target speed, seed/temperature/f0/tolerance (cures cloned voice drift) |
| TTS Template | `tools/scripts/tts_demo_voice.json` | Watchdog re-renders `engine/data/tts_runtime.json` based on this at every boot |
| Avatar Package | `engine/data/avatars/<id>/` | latents/coords/mask/full_imgs, etc. (~80~115 MB/avatar) |
| Idle Sequence | `engine/data/customvideo/<id>_idle/*.png` | Read upon engine boot; Mini retains Arthur's (~85 MB) |
| Render Source Mount | `engine/data/custom_config_<id>.json`, `engine/data/custom_config.json` | Tells the engine which image sequence to use |
| Reference Audio | `audiocpp/webui/voice/<voice>_ref.wav` | The "sound source" for cloning the voice (Mini version: arthur_ref / mia_ref) |
| Voice Anchor | `engine/data/voice_anchor_ref_<voice>_ref.wav_*.npz` | Acoustic anchors for start-of-sentence alignment |
| Runtime Mode Record | `engine/data/runtime_mode_active.json` | Written by the watchdog, read by backend `/health` to report back to the UI |

## 4. Runtime Modes (Auto-determined by Watchdog)

| Mode | What Boots | Measured VRAM (Original 13-Teacher Version) | Applicable Environment |
|---|---|---|---|
| `full` | Avatar Engine + **Dual** TTS workers | ≈ 13 GB | RAM ≥ 29 GB AND VRAM ≥ 16 GB |
| `lite` | Avatar Engine + **Single** TTS worker | ≈ 10.8 GB | RAM 16~28 GB OR VRAM 11~15 GB |
| ~~`ultra`~~ | ~~Only runs cartoon engine~~ | — | **N/A for Mini version** (cartoon engine trimmed) |

The measured footprint for the Mini version: MuseTalk ~9 GB + Single TTS ~2 GB ≈ **11 GB VRAM**.
The Mini version **clamps `ultra` requests to `lite`** (the original definition of "only running cartoon-style engine" has no target in this version).
The auto-detection logic is written in `engine/aiteacher_watchdog.py`, the result lands in `runtime_mode_active.json`, and can be viewed/changed in the settings page.

## 5. What Was Trimmed in the Mini Version and Why

| Trimmed Item | Size | Reason |
|---|---|---|
| Avatar packages for the other 11 teachers | ~1.1 GB | Only Arthur/Mia are in `AVATARS`; unassigned avatars shouldn't be in the package |
| Idle sequences/personas/ref audio/voice anchors/presets for other teachers | ~0.5 GB | Same as above; `tts_runtime.json` is re-rendered from the trimmed template → the UI voice options are exactly two |
| **The entire cartoon-style viseme engine (8010)** | Engine process + Assets ~0.4 GB | This version only has realistic teachers. The viseme lip shapes are **shared cartoon layers** (`avatar/viseme/*.png`) pasted onto idle frames; realistic base images + cartoon lips = misaligned facial features; plus, the 1017 test showed feeding musetalk assets to the viseme engine caused deadlocks |
| Legacy local Qwen3-TTS service leftovers (`audiocpp/{models,gpu,venv,cpu}`) | ~7.7 GB | Retired on 1015, not loaded at runtime at all (the low-spec version uses the same trimming) |
| Personal state: history/sessions, facial calibration, window position, `.env` | — | Factory purity: Start from scratch after installation; Cloud Key must be provided via the First-Run Wizard |

**Why 8011 doubles as the "desktop window"**: When `engine/web/desktop.html` (Tray: "Teacher appears on desktop") connects, it sends an `/offer` **without the avatar parameter**, rendering the engine's **default boot slot**. The Mini version sets the default slot to `coach_arthur`,
so the window that appears is the teacher for this version, not a non-existent cartoon teacher.

**Deep Probe Trap (Mandatory Fix)**: The watchdog uses a "real synthesis of a sentence" to check if the TTS is alive (only probing the port can be fooled by "port is open but not working").
If the voice used for this probe is still set to `lingxue` when that voice has been trimmed → 18091 will always be judged as "dead" → repeatedly killed, creating an endless loop.
The Mini version changes the probe voice to `arthur`.

## 6. Purity and Security (Enforced for all packaged versions)

- Sessions/Memories: `history.json` / `sessions.json` cleared; `persona.json` reset to the factory teacher.
- Personal Calibrations: `landmarks.json`, `.calib`, `app/config/electron.json` are completely excluded from the package.
- Runtime State: `tts_endpoints.json`, `runtime_mode_active.json`, `tts_runtime.json` are excluded, rebuilt on startup.
- Keys: All `*.env` files excluded + a full-tree sniff before packaging (rejects package if a hit is found); `llm_api_key` in the factory `app.json` is blanked out.
- Relocation Self-Healing: Absolute paths inside the package are rewritten to the current root directory at first launch by `tools/scripts/first_run_setup.py` → `portable_paths.py`.
