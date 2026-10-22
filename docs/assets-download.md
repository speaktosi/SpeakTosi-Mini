# Large Asset Downloads and Placement

**English** · [中文说明 →](zh-CN/assets-download.md)

Due to size and licensing reasons, the following assets are **not distributed with the git repository**. After cloning the code, please download and place them in the specified locations (users of the full packaged version can skip this section).

| Asset | Placement Location | Description |
|---|---|---|
| MuseTalk V15 Weights (`unet.pth`, etc.) | `engine/models/musetalkV15/`, `engine/models/musetalk/` | Official HuggingFace `TMElyralab/MuseTalk` v15 weights |
| Audio Feature Models (whisper, etc.) | `engine/models/whisper/` | MuseTalk official dependency chain |
| VRM Example Avatars | `app/assets/` | Any open-source VRM (commercial license required) |
| Example Coach latents | `engine/data/avatars/` | Self-trained generation according to `docs/custom-avatar.md` |
| Python Base | `runtime/py311`, `runtime/py312` | Official embeddable zip extracted (py312 requires enabling `import site` in `python312._pth`) |
| TTS Models | `audiocpp/models/` | qwen3-tts base weights (Apache-2.0) |

After placement is complete, double-click `start_speaktosi.vbs`; the watchdog will self-check and spin up the full stack.

## Rebuilding the Environment from Source (Advanced)

```bat
:: 引擎环境（CUDA 12.8 / RTX50）
runtime\py312\python.exe -m venv engine\venv
engine\venv\Scripts\pip.exe install -r engine\requirements.txt
:: backend
runtime\py311\python.exe -m venv app\backend\.venv
app\backend\.venv\Scripts\pip.exe install -r app\backend\requirements-backend.txt
```
