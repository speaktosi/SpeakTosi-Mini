# 大文件资产下载与放置

出于体积与协议原因，以下资产**不随 git 仓库分发**。克隆代码后，请下载并放到指定位置（整包用户可跳过本节）。

| 资产 | 放置位置 | 说明 |
|---|---|---|
| MuseTalk V15 权重 (`unet.pth` 等) | `engine/models/musetalkV15/` `engine/models/musetalk/` | 官方 HuggingFace `TMElyralab/MuseTalk` 的 v15 权重 |
| 音频特征模型 (whisper 等) | `engine/models/whisper/` | MuseTalk 官方依赖链 |
| VRM 示例形象 | `app/assets/` | 任意开源 VRM（需商用许可） |
| 示例老师 latents | `engine/data/avatars/` | 按 `docs/custom-avatar.md` 自训生成 |
| Python 基座 | `runtime/py311`, `runtime/py312` | 官方 embeddable zip 解压（py312 须 `python312._pth` 内启用 `import site`） |
| TTS 模型 | `audiocpp/models/` | qwen3-tts 基座权重（Apache-2.0） |

放置完成后双击 `start_speaktosi.vbs`，看门狗会自检拉起全栈。

## 从源码重建环境（高级）

```bat
:: 引擎环境（CUDA 12.8 / RTX50）
runtime\py312\python.exe -m venv engine\venv
engine\venv\Scripts\pip.exe install -r engine\requirements.txt
:: backend
runtime\py311\python.exe -m venv app\backend\.venv
app\backend\.venv\Scripts\pip.exe install -r app\backend\requirements-backend.txt
```
