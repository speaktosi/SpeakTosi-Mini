# 素材与模型清单

Mini 版有**两种发布件**，差别只在"带不带大件"：

| 发布件 | 内容 | 体量 | 用途 |
|---|---|---|---|
| `SpeakTosi_Mini_*.zip` | 代码 + **全部模型** + 内置 Python 环境 + Electron 依赖 + 克隆语音运行环境 + 两位老师的形象素材 | ≈ 25 GB | 解压即用，给同学/同事 |
| 本仓库(源码档) | 只要代码 + 文档 + 小体积配置/参考音 | ≈ 200 MB | GitHub 发布、改代码、提 PR |

仓库档**故意不含**下面的二进制(体积大、且不适合进 git)；缺失项按本表补齐，或直接取 Release 的自包含包。

---

## 1. 数字人引擎模型(`engine/models/`)

| 路径 | 体量 | 说明 |
|---|---|---|
| `engine/models/musetalkV15/` | ≈ 3.2 GB | MuseTalk V15 口型权重(UNet / VAE / 位置编码等) |
| `engine/models/sd-vae/` | ≈ 320 MB | Stable-Diffusion VAE(图像编解码) |
| `engine/models/whisper/` | ≈ 145 MB | 音频特征提取(口型驱动用) |
| `engine/models/rembg/` | ≈ 168 MB | 抠像/前景分割 |
| `engine/models/face-parse-bisent/` | ≈ 96 MB | 人脸解析(口型区域) |
| `engine/models/sd-vae-ft-mse/` | 占位目录 | 可选变体，Mini 版不需要 |

## 2. 老师形象素材

| 路径 | 体量 | 说明 |
|---|---|---|
| `engine/data/avatars/coach_arthur/` | ≈ 88 MB | Arthur 的 MuseTalk 形象包(latents/coords/mask/full_imgs/cover) |
| `engine/data/avatars/coach_mia/` | ≈ 115 MB | Mia 的形象包 |
| `engine/data/customvideo/coach_arthur_idle/*.png` | ≈ 85 MB | Arthur 待机帧序列(引擎启动即读) |
| `audiocpp/webui/voice/arthur_ref.wav`、`mia_ref.wav` | 各 ≈ 1 MB | 克隆音色参考音(两位老师的声音来源) |
| `engine/data/voice_anchor_ref_*_ref.wav_*.npz` | 每个 < 1 MB | 音色锚点(首句对齐)；缺失会在首次合成时重建 |
| `avatar/Coach-Arthur.png|.wav`、`avatar/Coach-Mia.png|.wav` | 立绘 + 见面语参考音 |
| `avatar/persona/CoachArthur.md`、`CoachMia.md`、`Shared-core-adult.md` | 文本 | 人设(soul) |

> Mia 没有独立待机序列：引擎在 `engine/data/custom_config_coach_mia.json` 不存在时会回退用形象包自带的帧，
> 这是设计行为，不是缺件。

## 3. 运行环境(只有自包含包才带)

| 路径 | 体量 | 说明 |
|---|---|---|
| `runtime/py311/`、`runtime/py312/` | ≈ 89 MB + 67 MB | 内置 Python(后端/引擎各自建 venv 的基底) |
| `engine/venv/` | ≈ 数 GB | 引擎依赖(torch 等，CUDA 版) |
| `app/backend/.venv/` | ≈ 数百 MB | 后端依赖(FastAPI/uvicorn/faster-whisper 等) |
| `app/node_modules/` | ≈ 数百 MB | Electron 前端依赖(含 `electron/dist/electron.exe`) |
| `runtime/voice-pro/` | ≈ 4.5 GB | 克隆语音运行环境(Voice-Pro env + `cosyvoice` 代码 + `CosyVoice2-0.5B` 模型) |
| `audiocpp/webui/ffmpeg.exe` | ≈ 数十 MB | 后端切音频/导出用 |

## 4. 仓库档怎么补齐

```text
1) Python 环境：
     python -m venv engine/venv            && engine/venv/Scripts/pip install -r engine/requirements.txt
     python -m venv app/backend/.venv      && app/backend/.venv/Scripts/pip install -r app/backend/requirements-backend.txt
     cd app && npm install
2) 引擎模型：把 ① 的表按目录结构放进 engine/models/
3) 形象素材：把 ② 的表放进对应目录
4) 克隆语音环境：需要 Voice-Pro(含 CosyVoice2-0.5B)，放到 runtime/voice-pro/，
   结构镜像原样：installer_files/env/Scripts/python.exe、cosyvoice/、model/cosyvoice/CosyVoice2-0.5B/
```

自包含包里的 `runtime/voice-pro/` 就是这份环境的快照，可直接拷到源码档对应位置。

### 仓库档额外缺的两个大件(GitHub 单文件 100 MB 硬上限)

| 文件 | 体量 | 为什么不在仓库里 / 怎么补 |
|---|---|---|
| `audiocpp/webui/ffmpeg.exe` | 227 MB | 超 GitHub 单文件上限，会被直接拒收。从自包含包拷一份，或装系统 ffmpeg 后把该路径指过去 |
| `engine/avatars/wav2lip/face_detection/detection/sfd/s3fd.pth` | 90 MB | 人脸检测权重，只有 wav2lip 渲染路径用；本档走 MuseTalk，运行时不需要。要用时从自包含包拷 |

## 5. 关于 Git LFS

大二进制被 `.gitignore` 挡在仓库外；如果你要用 LFS 管理它们(不推荐，几十 GB 不适合 LFS)，
`.gitattributes` 里已经写好了匹配规则(`*.safetensors`、`*.pt`、`*.pth`、`*.bin`、`*.onnx`、`*.wav`、`*.mp4`、`*.npz` 等)。
默认策略是**不跟踪**：请用 Release 或私有分发渠道传这些文件。