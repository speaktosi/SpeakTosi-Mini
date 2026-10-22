# Assets and Models List

**English** · [中文说明 →](zh-CN/ASSETS.md)

The Mini edition has **two types of releases**, the only difference being whether they "include the large files":

| Release Package | Content | Size | Purpose |
|---|---|---|---|
| `SpeakTosi_Mini_*.zip` | Code + **All Models** + Built-in Python Environment + Electron Dependencies + Voice Cloning Environment + Avatar assets for two coaches | ≈ 25 GB | Unzip and run, for students/colleagues |
| This Repository (Source Package) | Code + Documentation + Small config/reference audio only | ≈ 200 MB | GitHub release, modifying code, submitting PRs |

The repository package **intentionally excludes** the following binaries (which are large and unsuitable for git). Fill in the missing items according to this table, or simply grab the standalone package from Releases.

---

## 1. Avatar Engine Models (`engine/models/`)

| Path | Size | Description |
|---|---|---|
| `engine/models/musetalkV15/` | ≈ 3.2 GB | MuseTalk V15 lip-sync weights (UNet / VAE / positional encoding, etc.) |
| `engine/models/sd-vae/` | ≈ 320 MB | Stable-Diffusion VAE (image encoding/decoding) |
| `engine/models/whisper/` | ≈ 145 MB | Audio feature extraction (for lip-sync driving) |
| `engine/models/rembg/` | ≈ 168 MB | Matting/foreground segmentation |
| `engine/models/face-parse-bisent/` | ≈ 96 MB | Face parsing (lip region) |
| `engine/models/sd-vae-ft-mse/` | Placeholder dir | Optional variant, not needed for the Mini edition |

## 2. Coach Avatar Assets

| Path | Size | Description |
|---|---|---|
| `engine/data/avatars/coach_arthur/` | ≈ 88 MB | Arthur's MuseTalk avatar package (latents/coords/mask/full_imgs/cover) |
| `engine/data/avatars/coach_mia/` | ≈ 115 MB | Mia's avatar package |
| `engine/data/customvideo/coach_arthur_idle/*.png` | ≈ 85 MB | Arthur's idle frame sequence (loaded on engine startup) |
| `audiocpp/webui/voice/arthur_ref.wav`, `mia_ref.wav` | ≈ 1 MB each | Voice cloning reference audio (source of the two coaches' voices) |
| `engine/data/voice_anchor_ref_*_ref.wav_*.npz` | < 1 MB each | Voice anchors (for first-sentence alignment); missing ones are rebuilt on first synthesis |
| `avatar/Coach-Arthur.png|.wav`, `avatar/Coach-Mia.png|.wav` | Portraits + greeting reference audio |
| `avatar/persona/CoachArthur.md`, `CoachMia.md`, `Shared-core-adult.md` | Text | Persona (soul) |

> Mia does not have a separate idle sequence: when `engine/data/custom_config_coach_mia.json` does not exist, the engine falls back to using the frames built into the avatar package.
> This is by design, not a missing file.

## 3. Runtime Environment (Included in standalone package only)

| Path | Size | Description |
|---|---|---|
| `runtime/py311/`, `runtime/py312/` | ≈ 89 MB + 67 MB | Built-in Python (base for backend/engine venvs) |
| `engine/venv/` | ≈ Several GB | Engine dependencies (torch, etc., CUDA version) |
| `app/backend/.venv/` | ≈ Several hundred MB | Backend dependencies (FastAPI/uvicorn/faster-whisper, etc.) |
| `app/node_modules/` | ≈ Several hundred MB | Electron frontend dependencies (contains `electron/dist/electron.exe`) |
| `runtime/voice-pro/` | ≈ 4.5 GB | Voice cloning runtime environment (Voice-Pro env + `cosyvoice` code + `CosyVoice2-0.5B` model) |
| `audiocpp/webui/ffmpeg.exe` | ≈ Tens of MB | Used by backend for audio slicing/export |

## 4. How to Complete the Repository Package

```text
1) Python Environment:
     python -m venv engine/venv            && engine/venv/Scripts/pip install -r engine/requirements.txt
     python -m venv app/backend/.venv      && app/backend/.venv/Scripts/pip install -r app/backend/requirements-backend.txt
     cd app && npm install
2) Engine Models: Place items from table ① into engine/models/ maintaining the directory structure
3) Avatar Assets: Place items from table ② into their corresponding directories
4) Voice Cloning Environment: Requires Voice-Pro (including CosyVoice2-0.5B), place into runtime/voice-pro/
   Structure mirrors the original: installer_files/env/Scripts/python.exe, cosyvoice/, model/cosyvoice/CosyVoice2-0.5B/
```

The `runtime/voice-pro/` in the standalone package is a snapshot of this environment, which can be directly copied to the corresponding location in the source code package.

### Two Extra Large Files Missing from the Repository Package (Due to GitHub's 100 MB strict single-file limit)

| File | Size | Why it's not in the repo / How to get it |
|---|---|---|
| `audiocpp/webui/ffmpeg.exe` | 227 MB | Exceeds GitHub's single-file limit and would be rejected. Copy from the standalone package, or install system ffmpeg and point the path to it |
| `engine/avatars/wav2lip/face_detection/detection/sfd/s3fd.pth` | 90 MB | Face detection weights, used only by the wav2lip rendering path; this package uses MuseTalk and does not need it at runtime. Copy from the standalone package if needed |

## 5. About Git LFS

Large binaries are kept out of the repository by `.gitignore`; if you want to manage them with LFS (not recommended, tens of GBs are not suitable for LFS), matching rules (`*.safetensors`, `*.pt`, `*.pth`, `*.bin`, `*.onnx`, `*.wav`, `*.mp4`, `*.npz`, etc.) are already written in `.gitattributes`.
The default strategy is **untracked**: please use Releases or private distribution channels to transfer these files.
