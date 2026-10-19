"""
audio.cpp WebUI (Route B) — a thin Gradio frontend that proxies to the local
audiocpp_server HTTP API.

Model loading is on demand: instead of preloading a model at server startup, this
WebUI reads models_catalog.json, lets you pick a model, and (re)starts
audiocpp_server with a single-model config only when you actually load/run it.
One model lives in VRAM at a time — picking a different model swaps it.

Uploaded files are saved by Gradio to local temp paths, which we pass to the
server as `voice_ref` / `audio` (frontend + server run on the same machine).

Just launch this (it starts the server for you):
    venv\\Scripts\\python audiocpp-portable\\webui.py

Env overrides:
    AUDIOCPP_BACKEND=gpu|cpu     which bin dir to launch the server from
                                 (default: auto — gpu when an NVIDIA driver and the
                                 gpu server build are both present, else cpu)
    AUDIOCPP_THREADS=N           ggml compute threads (default 1; cpu backend
                                 defaults to all cores minus one)
    AUDIOCPP_SERVER=http://...   talk to an already-running server instead of managing one
    AUDIOCPP_LOAD_TIMEOUT=300    seconds to wait for a model to finish loading
    AUDIOCPP_NO_BROWSER=1        don't open a browser tab
"""
import atexit
import base64
import io
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import warnings
import wave
from urllib.parse import urlparse

import numpy as np
import requests
import gradio as gr

# 降噪：屏蔽 Gradio 内部触发、每次请求都会刷屏的 Starlette 弃用告警。
warnings.filterwarnings("ignore", message=r".*HTTP_422_UNPROCESSABLE.*")


def _silence_proactor_connection_reset():
    """Windows: swallow the benign `ConnectionResetError [WinError 10054]` that
    asyncio's proactor prints when a browser/HTTP connection drops abruptly."""
    if sys.platform != "win32":
        return
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport
    except Exception:
        return
    _orig = _ProactorBasePipeTransport._call_connection_lost

    def _patched(self, exc):
        if isinstance(exc, ConnectionResetError):
            exc = None  # peer reset == normal close; still run the cleanup below
        try:
            return _orig(self, exc)
        except ConnectionResetError:
            # _orig's own sock.shutdown() raced a peer reset (WinError 10054),
            # which skips the rest of its cleanup — finish it here.
            sock = getattr(self, "_sock", None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                self._sock = None
            server = getattr(self, "_server", None)
            if server is not None:
                try:
                    server._detach()
                except Exception:
                    pass
                self._server = None
            self._called_connection_lost = True

    _ProactorBasePipeTransport._call_connection_lost = _patched


def _silence_h11_content_length_race():
    """Large uploads (e.g. a multi-minute reference wav) can have the browser
    abort/replace an in-flight preview fetch for the same file while uvicorn
    is still streaming its body; h11 then raises LocalProtocolError trying to
    close out that half-sent response. It's a benign race — verified the
    aborted request doesn't affect the server or any other request, the
    browser's follow-up fetch of the same file completes fine — but uvicorn
    logs it as a full "Exception in ASGI application" traceback per occurrence.
    Drop just that one exception type from uvicorn's logger instead of hiding
    all uvicorn.error output."""
    try:
        from h11 import LocalProtocolError
    except Exception:
        return

    class _DropContentLengthRace(logging.Filter):
        def filter(self, record):
            exc = record.exc_info[1] if record.exc_info else None
            msg = str(exc or "")
            if isinstance(exc, LocalProtocolError) and "declared Content-Length" in msg:
                return False
            # uvicorn with httptools raises these RuntimeErrors on the same
            # preview race: "shorter" when the browser aborts/replaces the
            # fetch, "longer" when gradio sized Content-Length off a large
            # upload still being written to disk and the file grew mid-stream.
            # The request is already dead / the browser refetches; do not spam
            # a full ASGI traceback for either direction.
            if isinstance(exc, RuntimeError) and "than Content-Length" in msg:
                return False
            return True

    logging.getLogger("uvicorn.error").addFilter(_DropContentLengthRace())


def _patch_gradio_render_config_race():
    """Gradio 6.19 上游竞态（gradio-app/gradio#9991 只冻结了 blocks、漏了 fns）：
    本页有 7 个 @gr.render 动态参数区，页面加载/模型切换时多个渲染事件并发，
    一个事件在 get_config 里遍历 session 的 blocks/fns 字典的同时，另一个渲染
    正往里注册组件，偶发 "RuntimeError: dictionary changed size during
    iteration"（前端表现为该次渲染丢失/报错）。get_config 是纯只读操作，
    撞上竞态时稍等重读即可收敛。"""
    try:
        BlocksConfig = gr.blocks.BlocksConfig
        orig = BlocksConfig.get_config
    except AttributeError:
        return

    def _get_config_retry(self, renderable=None):
        for _ in range(10):
            try:
                return orig(self, renderable)
            except RuntimeError:
                time.sleep(0.02)
        return orig(self, renderable)

    BlocksConfig.get_config = _get_config_retry


_silence_proactor_connection_reset()
_silence_h11_content_length_race()
_patch_gradio_render_config_race()

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

# WebUI-local working dirs, kept next to this file and created on startup.
CONFIG_DIR = os.path.join(HERE, "configs")
OUTPUT_DIR = os.path.join(HERE, "output")
VOICE_DIR = os.path.join(HERE, "voice")
LOG_DIR = os.path.join(HERE, "logs")
for _d in (CONFIG_DIR, OUTPUT_DIR, VOICE_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

PROMPTS_DIR = VOICE_DIR                                    # built-in / reference voices
CATALOG_PATH = os.path.join(CONFIG_DIR, "models_catalog.json")
MODEL_PARAMS_PATH = os.path.join(CONFIG_DIR, "model_params.json")


def _find_bundle_root():
    """Locate the integrated bundle (holds cpu/ gpu/ models/ assets/ tools/).

    Works both in the dev tree (this file lives in webui/, next to
    audiocpp-portable/) and when webui/ is copied under the bundle for
    distribution (then the bundle is this file's parent dir). Override with
    AUDIOCPP_BUNDLE."""
    for c in (os.environ.get("AUDIOCPP_BUNDLE"),
              HERE,                                         # webui.py directly in the bundle
              PROJECT_ROOT,                                 # webui/ shipped under the bundle
              os.path.join(PROJECT_ROOT, "audiocpp-portable")):
        if c and (os.path.isdir(os.path.join(c, "gpu")) or os.path.isdir(os.path.join(c, "cpu"))):
            return c
    return os.environ.get("AUDIOCPP_BUNDLE") or os.path.join(PROJECT_ROOT, "audiocpp-portable")


BUNDLE_ROOT = _find_bundle_root()

def _detect_backend():
    """Which bundle build (gpu/ or cpu/) to launch. AUDIOCPP_BACKEND=gpu|cuda|cpu
    wins; otherwise auto-detect: gpu when an NVIDIA driver AND the gpu server
    build are both present, else cpu (the server runs fine on the cpu backend,
    just slower and with lower model coverage)."""
    env = os.environ.get("AUDIOCPP_BACKEND", "").strip().lower()
    if env in ("gpu", "cuda"):
        return "gpu"
    if env:
        return env
    if os.name == "nt":
        has_nvidia = os.path.isfile(os.path.join(
            os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvcuda.dll"))
    else:
        has_nvidia = shutil.which("nvidia-smi") is not None
    if has_nvidia and os.path.isfile(os.path.join(BUNDLE_ROOT, "gpu", "audiocpp_server.exe")):
        return "gpu"
    if os.path.isfile(os.path.join(BUNDLE_ROOT, "cpu", "audiocpp_server.exe")):
        return "cpu"
    return "gpu"


BACKEND = _detect_backend()
# The server's own backend name: bin dirs are gpu/ vs cpu/, but the server takes
# cuda|cpu|vulkan|metal, and its config default is "cuda" — which a CPU-only
# build rejects at startup, so the temp config must always spell it out.
SERVER_BACKEND = "cuda" if BACKEND == "gpu" else BACKEND
SERVER_EXE = os.path.join(BUNDLE_ROOT, BACKEND, "audiocpp_server.exe")
LOG_PATH = os.path.join(LOG_DIR, "audiocpp_server_webui.log")
LOAD_TIMEOUT = int(os.environ.get("AUDIOCPP_LOAD_TIMEOUT", "300"))

# model_manager.py is used to download not-yet-installed models in the background.
MODELS_ROOT = os.path.join(BUNDLE_ROOT, "models")


def _find_model_manager():
    for c in (os.environ.get("AUDIOCPP_MODEL_MANAGER"),
              os.path.join(BUNDLE_ROOT, "tools", "model_manager.py"),
              os.path.join(PROJECT_ROOT, "tools", "model_manager.py")):
        if c and os.path.isfile(c):
            return c
    return None


MODEL_MANAGER = _find_model_manager()


def _detect_vram_gb():
    """本机 NVIDIA 显卡显存总量（GB，多卡取最大）；无 nvidia-smi/无 N 卡返回 None。
    用于对照 catalog 条目的 min_vram_gb 估算值，提示“下载了也可能跑不动”。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        vals = [float(line) for line in out.stdout.split() if line.strip()]
        return round(max(vals) / 1024, 1) if vals else None
    except Exception:
        return None


LOCAL_VRAM_GB = _detect_vram_gb()


def _vram_shortfall(entry):
    """条目估算显存超过本机显存时返回 (需要GB, 本机GB)，否则 None。"""
    if BACKEND == "cpu":
        return None  # CPU 后端跑在系统内存里，显存对照不适用
    need = entry.get("min_vram_gb")
    if need and LOCAL_VRAM_GB and float(need) > LOCAL_VRAM_GB:
        return float(need), LOCAL_VRAM_GB
    return None

LANGS = ["", "english", "chinese", "french", "german", "italian",
         "japanese", "korean", "portuguese", "russian", "spanish", "Auto"]

# Which catalog task tokens each tab can drive. do_tts sends text + optional
# reference voice, which fits both plain TTS ("tts") and voice cloning ("clon").
TTS_TASKS = ("tts", "clon")
ASR_TASKS = ("asr",)
GEN_TASKS = ("gen",)   # music/SFX generation, served via the generic /v1/tasks/run route
VC_TASKS = ("vc", "svc", "s2s")           # 声音转换：源音频 + 目标音色，走 /v1/tasks/run
SEP_TASKS = ("sep",)                      # 音源分离：多轨 named_audio_outputs
ANALYZE_TASKS = ("vad", "diar", "align")  # 音频分析：segments / speaker_turns / words
VDES_TASKS = ("vdes",)                    # 声音设计：文字 + 音色描述，走 /v1/audio/speech

# Per-family behavior for the TTS tab, keyed by catalog `family`. This is how we
# cope with each model wanting different input formats / options: the shared UI
# stays simple, and each family gets its own hint, optional text transform, and
# default request options. A catalog entry can also carry its own `input_hint` /
# `default_options` to override the family profile without editing this file, and
# the "高级参数 (JSON)" box lets you pass ANY model-specific option at run time.
MODEL_PROFILES = {
    "vibevoice": {
        "input_hint": (
            "**VibeVoice** 多说话人脚本：每行 `Speaker N: 内容`（纯文字自动包装）；"
            "多音色用高级参数 `voice_samples`，不要传参考音频。"),
        "wrap_speaker_script": True,
        # VibeVoice has no internal text chunking. VRAM is bounded since the
        # layerwise-prefill/gallocr decode-graph fix, so chunks no longer need to be
        # tiny; 600 chars keeps each chunk's generation inside the default
        # max_tokens=1200 budget (~1.5 frames/CJK char) and the KV capacity tier
        # <= 2048 (~7.1 GB peak on 8 GB GPUs).
        "chunk_chars": 600,
        # Advanced-parameter widgets are sent only when the user edits them, so an
        # untouched num_inference_steps control (displayed 10) silently falls back to
        # the model config's ddpm_num_inference_steps=20 server-side. Send 10 (the
        # official demo default) explicitly; widgets/JSON still override.
        "default_options": {"num_inference_steps": 10},
    },
    "voxcpm2": {
        "input_hint": (
            "**VoxCPM2** 声音克隆：上传干净的单人参考音色并填『参考文本』；长文本自动分段。"),
        # 8G 4060 实测标定（2026-07-05，CLI --log + nvidia-smi 抓峰值）：
        # - 峰值 ≈ 固定基线 + audiovae 解码图。基线（权重+KV+生成图）与文本长度/
        #   max_tokens 无关：max_tokens 128/300/1200 峰值都是 7634MiB（生成会提前
        #   遇停止符，解码图按实际帧数而非 max_tokens 建）。所以 chunk_chars / max_tokens
        #   都压不动基线——真正的杠杆是**权重量化**（catalog session_options 里已设
        #   voxcpm2.weight_type=q8_0：bf16 权重 4.6G，峰值 7634→5549MiB）。
        # - audiovae 解码图仍随每段音频长度涨：容量取「≥latent_frames 的最小 2 的幂」
        #   (audiovae.cpp:724)，~1.28MB/帧。q8_0 下实测 ~53 字→cap512→6366MiB，
        #   ~106 字→cap1024→7722MiB（偏紧）。所以每段要短：60 字→cap≤512→~6.4G，
        #   留 ~1.7G 给其它占 GPU 的程序。想少接缝可上调，但注意 8G 边界。
        "chunk_chars": 60,
    },
    "qwen3_tts": {
        "input_hint": (
            "**Qwen3-TTS** 声音克隆：建议上传参考音色并填『参考文本』，否则可能提前截断。"),
    },
    "pocket_tts": {
        "input_hint": "**PocketTTS**：必须提供参考音色（上传/录制/内置）。",
    },
    "chatterbox": {
        "input_hint": (
            "**Chatterbox** 声音克隆：必须提供参考音色；语言仅支持英/西/法/德/意/葡/韩"
            "（**无中文**），留空=英语。"),
        # Chatterbox validates against 2-letter ISO codes (no zh/ja/ru, no auto),
        # so the shared dropdown's friendly names are translated here; names not
        # listed are genuinely unsupported by the model and rejected up front.
        "lang_map": {
            "english": "en", "spanish": "es", "french": "fr", "german": "de",
            "italian": "it", "portuguese": "pt", "korean": "ko",
        },
    },
    "qwen3_asr": {
        # Encoder cap: max_source_positions=1500 tokens at 13 tokens/second
        # (qwen3_asr_audio_encoder_token_count) -> ~115 s of audio per request.
        # 但 8G 卡上先撞显存：thinker prefill 图随时长超线性膨胀（4060 8G 实测
        # 65s 可过、70s 要 13.2GB、75s 要 14.6GB），所以客户端按 max_input_seconds
        # =60s 在静音处切段逐段转写再拼接，顺带解决 70~115s 音频原本的 OOM。
        "input_hint": ("**Qwen3-ASR**：长音频自动分段转写；"
                       "语种/上下文/对话模式见『转写选项』。"),
        "max_input_seconds": 60,
    },
    "ace_step": {
        "input_hint": (
            "**ACE-Step** 音乐生成/编辑：提示词写风格/乐器/情绪（英文最佳），可填歌词。"
            "编辑类 route 需上传源音频并建议先点『🔍 分析源音频』；参数详解见 webui/README.md。"),
        # 原版 turbo UI 默认 shift=3.0（C++ 端默认 1.0，仅 remix/extract 路由自带 3.0）。
        # 控件只发用户改过的项，所以这里显式发送，保证 UI 显示值=实际值。
        "default_options": {"shift": 3.0},
    },
    "stable_audio": {
        "input_hint": (
            "**Stable Audio**：提示词**仅英文**，不用歌词；"
            "上传源音频可做 init/inpaint（高级参数选 audio_input_kind）。"),
    },
    "heartmula": {
        "input_hint": (
            "**HeartMuLa**：高级参数 `tags` 必填（逗号分隔），『歌词』填唱词；"
            "峰值显存 ~25G，8G 卡跑不动。"),
    },
    "vevo2": {
        "input_hint": (
            "**Vevo2**：源语音 + 目标音色，默认只换音色（保留说话风格）；"
            "风格转换类 route 需 JSON 补 `style_ref` 等，详见 webui/README.md。"
            "长音频自动分段，参考音色自动截 ≤10s。"),
        # 8G 4060 实测标定（2026-07-04/07-05）：FM 图一次建图，序列长度 =
        # 目标音色(prompt) + 源(target) 帧数（均 50fps，见 fm.cpp:782 cond_frames =
        # prompt_frames + target_frames）。cond≈25s（源15s+参考10s）就把 8G 吃满、
        # 峰值溢出到共享显存（idle 已占 7.9G/8G）；cond≈20s 勉强、30s 必炸。所以按
        # (预算 − 参考时长) 反推每段源时长，并把参考截到 ≤ ref_max，令 cond 稳定
        # 落在预算内；各段源仍补零到等长以复用缓存图。带 target_text 的编辑类 route
        # 不适合分段（会把文本对不上），这类输入本身也放不进显存。
        # 权重加载后约占 5.5-6G，留给图的只有 ~2G；18s 源(cond≈21) 只剩 ~350MB，
        # 所以预算取 16（cond≈16，留 ~0.8-1.2G 给峰值/其它占用 GPU 的程序）。
        "vc_chunk_seconds": 15,          # 每段源时长上限（会被显存预算进一步压低）
        "vc_fm_budget_seconds": 16,      # 参考 + 每段源 的总时长预算（8G 安全线）
        "vc_ref_max_seconds": 10,        # 目标音色参考截断上限
        "vc_min_chunk_seconds": 6,       # 每段源时长下限，避免切得过碎
    },
    "seed_vc": {
        "input_hint": (
            "**Seed-VC**：源语音 + 目标音色参考（几秒干净人声）；"
            "默认 v2 路线，v1 旧路线在高级参数切换。"),
    },
    "miocodec": {
        "input_hint": "**MioCodec**：codec 重建式转换——源提供内容，参考提供音色。",
    },
    "htdemucs": {
        "input_hint": "**HTDemucs**：输出 drums / bass / other / vocals 四轨。",
    },
    "mel_band_roformer": {
        "input_hint": "**Mel-Band RoFormer**：输出人声轨 + 伴奏轨。",
    },
    "silero_vad": {
        "input_hint": "**Silero VAD**：检测音频中的语音段。",
    },
    "marblenet_vad": {
        "input_hint": "**MarbleNet VAD**：帧级语音活动检测，输出语音段列表。",
    },
    "sortformer_diar": {
        "input_hint": "**Sortformer**：说话人分离（谁在何时说话，≤4 人）。",
    },
    "qwen3_forced_aligner": {
        "input_hint": "**Qwen3 强制对齐**：『对齐文本』填音频原文，输出逐词时间戳（≤115 秒）。",
    },
}
# Qwen3-ASR 可强制的语种（模型 config.json 的 support_languages，prompt 里用英文名；
# 留空/Auto = 自动检测）。citrinet 等其它 ASR 族忽略该字段。
QWEN3_ASR_LANGUAGES = [
    ("中文", "Chinese"), ("英语", "English"), ("粤语", "Cantonese"),
    ("日语", "Japanese"), ("韩语", "Korean"), ("俄语", "Russian"),
    ("法语", "French"), ("德语", "German"), ("西班牙语", "Spanish"),
    ("葡萄牙语", "Portuguese"), ("意大利语", "Italian"), ("阿拉伯语", "Arabic"),
    ("印尼语", "Indonesian"), ("泰语", "Thai"), ("越南语", "Vietnamese"),
    ("土耳其语", "Turkish"), ("印地语", "Hindi"), ("马来语", "Malay"),
    ("荷兰语", "Dutch"), ("瑞典语", "Swedish"), ("丹麦语", "Danish"),
    ("芬兰语", "Finnish"), ("波兰语", "Polish"), ("捷克语", "Czech"),
    ("菲律宾语", "Filipino"), ("波斯语", "Persian"), ("希腊语", "Greek"),
    ("罗马尼亚语", "Romanian"), ("匈牙利语", "Hungarian"), ("马其顿语", "Macedonian"),
]

DEFAULT_PROFILE = {"input_hint": "", "wrap_speaker_script": False, "default_options": {},
                   # Families with internal chunking handle long text fine; the client
                   # split only exists to bound each HTTP request (no 900 s timeout)
                   # and surface progress, so the budget can stay coarse.
                   "chunk_chars": 1000}

# One "Speaker N:" line (any speaker index) is enough to treat text as a script.
_SPEAKER_RE = re.compile(r"^\s*Speaker\s+\d+\s*:", re.IGNORECASE | re.MULTILINE)


def profile_for(entry):
    prof = {**DEFAULT_PROFILE, **MODEL_PROFILES.get(entry.get("family", ""), {})}
    if entry.get("input_hint"):
        prof["input_hint"] = entry["input_hint"]
    if entry.get("default_options"):
        prof["default_options"] = {**prof.get("default_options", {}), **entry["default_options"]}
    return prof


def model_hint_for(model_id):
    entry = catalog_by_id(model_id) if model_id else None
    if not entry:
        return ""
    hint = profile_for(entry)["input_hint"]
    short = _vram_shortfall(entry)
    if short:
        warn = (f"⚠️ **显存提示**：该模型按默认设置估算需 **≥{short[0]:g}G** 显存，"
                f"本机检测到 **{short[1]:g}G**——能加载但会溢出到共享显存而变慢"
                + ("，下载前请三思。" if not entry["installed"] else "。"))
        hint = warn + ("\n\n" + hint if hint else "")
    return hint


def resolve_language(prof, language):
    """Translate the shared language dropdown into what the selected family
    expects. Most families (Qwen3-TTS, VibeVoice, …) take the UI's friendly
    names as-is, so they have no `lang_map` and the value passes through. A
    family with a restricted language set (Chatterbox: 2-letter ISO codes, no
    Chinese/Japanese/Russian, no auto-detect) supplies a `lang_map`; its names
    are converted and anything it can't do — including "Auto" — is rejected here
    with an actionable message instead of a raw server 500."""
    lang = (language or "").strip()
    lang_map = prof.get("lang_map")
    if not lang_map:
        return lang
    if not lang:
        return ""                       # 留空 = 用模型默认
    code = lang_map.get(lang.lower())
    if code is not None:
        return code
    raise gr.Error(
        f"所选模型不支持语言「{language}」。请改选：{' / '.join(lang_map)}，"
        "或“留空”用模型默认。")


def _as_speaker_script(text):
    """Wrap plain text into `Speaker 0:` lines when it isn't already a script."""
    if _SPEAKER_RE.search(text or ""):
        return text
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return "\n".join(f"Speaker 0: {ln}" for ln in lines) if lines else text


def _vibevoice_punctuate_script(text):
    """VibeVoice is much less stable on tiny lines without sentence punctuation."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SPEAKER_LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        prefix, body = m.group(1), m.group(2).strip()
        if body and body[-1] not in "。！？!?.,;；:":
            body += "。"
        out.append(f"{prefix} {body}" if body else prefix)
    return "\n".join(out) if out else text


def _vibevoice_text_max_tokens(chunk, ui_default=1200):
    """Estimate VibeVoice speech tokens from text, not reference-prompt length."""
    body = re.sub(r"(?im)^\s*Speaker\s+\d+\s*:\s*", "", chunk or "")
    cjk = sum(1 for ch in body if "\u4e00" <= ch <= "\u9fff")
    non_space = sum(1 for ch in body if not ch.isspace())
    non_cjk = max(0, non_space - cjk)
    pauses = sum(1 for ch in body if ch in "，。！？；：,.!?;:")
    estimate = int(cjk * 2.2 + non_cjk * 0.45 + pauses * 3.0 + 8)
    return max(18, min(int(ui_default), estimate))


# VibeVoice 1.5B 对超短脚本从第 1 帧起整段胡言乱语——模型级缺陷（长播客数据训练，
# 短文本 OOD），与参考音色/CFG/扩散步数/中英文/seed 全部无关（2026-07-08 A/B+ASR
# 转写实测：27 字必炸、40 字逐字正确；按上面估算公式 69 token 炸 / 87 token 过）。
# 低于阈值直接拦截；生成出来只会是垃圾，调参数救不了。
_VIBEVOICE_MIN_EST_TOKENS = 85


def _merge_short_vibevoice_tail(chunks):
    """分段时留下的过短尾段同样会胡言乱语：并回前一段。前段最多超预算几十字，
    仍在 max_tokens=1200 封顶的显存包络内。"""
    while len(chunks) > 1 and (
            _vibevoice_text_max_tokens(chunks[-1]) < _VIBEVOICE_MIN_EST_TOKENS):
        tail = chunks.pop()
        chunks[-1] = chunks[-1] + "\n" + tail
    return chunks


# --- client-side long-text chunking ------------------------------------------
# Long text is synthesized as one HTTP request per chunk and concatenated here.
# That keeps every request bounded (no 900 s timeout, works for families without
# internal chunking) and lets the UI show real per-chunk progress.
_SPEAKER_LINE_RE = re.compile(r"^\s*(Speaker\s+\d+\s*:)\s*(.*)$", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"[^。！？!?；;…]*[。！？!?；;…]+|[^。！？!?；;…]+$")


def _split_long_line(line, budget):
    """Split one overlong line at sentence ends into pieces of <= budget chars,
    re-attaching its `Speaker N:` prefix (if any) to every piece."""
    m = _SPEAKER_LINE_RE.match(line)
    prefix, body = (m.group(1) + " ", m.group(2)) if m else ("", line.strip())
    pieces, cur = [], ""
    for sent in _SENTENCE_RE.findall(body):
        if cur and len(cur) + len(sent) > budget:
            pieces.append(prefix + cur)
            cur = ""
        cur += sent
    if cur:
        pieces.append(prefix + cur)
    return pieces or [line]


def _split_tts_chunks(text, budget):
    """Group non-empty lines into chunks of <= budget chars. A line is never
    split across chunks unless it alone exceeds the budget (then it is split at
    sentence boundaries). Returns a list of chunk strings."""
    units = []
    for ln in (text or "").splitlines():
        if not ln.strip():
            continue
        units.extend(_split_long_line(ln, budget) if len(ln) > budget else [ln])
    chunks, cur, cur_len = [], [], 0
    for unit in units:
        sep = 1 if cur else 0  # the "\n" join separator counts toward the budget
        if cur and cur_len + sep + len(unit) > budget:
            chunks.append("\n".join(cur))
            cur, cur_len, sep = [], 0, 0
        cur.append(unit)
        cur_len += sep + len(unit)
    if cur:
        chunks.append("\n".join(cur))
    return chunks or ([text] if (text or "").strip() else [])


def _concat_wavs(blobs, out_path, keep_ratios=None):
    """Concatenate same-format WAV byte blobs into one file at out_path.
    keep_ratios[i]：每段只保留前一部分（分段转换把源补零到等长后，
    按有效占比截掉对应输出的尾部静音）。"""
    params, frames = None, []
    for i, blob in enumerate(blobs):
        with wave.open(io.BytesIO(blob)) as w:
            fmt = (w.getnchannels(), w.getsampwidth(), w.getframerate())
            if params is None:
                params = fmt
            elif fmt != params:
                raise gr.Error(f"分段音频格式不一致：{fmt} != {params}")
            n = w.getnframes()
            if keep_ratios is not None:
                n = max(1, min(n, int(round(n * keep_ratios[i]))))
            frames.append(w.readframes(n))
    with wave.open(out_path, "wb") as w:
        w.setnchannels(params[0])
        w.setsampwidth(params[1])
        w.setframerate(params[2])
        for data in frames:
            w.writeframes(data)


def _audio_duration_seconds(path):
    """Duration of a local audio file, or None when not measurable. wave covers
    WAV, i.e. Gradio mic recordings and the typical uploads here; other formats
    just skip the duration note instead of failing the request."""
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            return (w.getnframes() / float(rate)) if rate else None
    except Exception:
        return None


# 已转码文件缓存：gradio 的临时路径按内容哈希命名，同一上传重复运行不重复转码。
_WAV_CACHE = {}


def _find_ffmpeg():
    """转码用的 ffmpeg：随 webui 分发的 webui\\ffmpeg.exe 优先，其次 PATH。"""
    bundled = os.path.join(HERE, "ffmpeg.exe")
    if os.path.exists(bundled):
        return bundled
    found = shutil.which("ffmpeg")
    if not found:
        raise gr.Error("找不到 ffmpeg.exe（应随 webui 一起放在 webui 目录），无法转码非 WAV 音频。")
    return found


def _ensure_ascii_path(path):
    """若路径含非 ASCII 字符，复制到纯 ASCII 临时文件。
    C++ server 在 Windows 上无法打开含中文等字符的路径。"""
    try:
        path.encode("ascii")
        return path
    except UnicodeEncodeError:
        pass
    fd, out = tempfile.mkstemp(prefix="audiocpp_asc_", suffix=".wav")
    os.close(fd)
    shutil.copy2(path, out)
    return out


def _wait_file_stable(path, timeout=8.0, interval=0.08, stable_ticks=3):
    """Wait until a just-uploaded/recorded file stops changing on disk.
    On Windows, the browser can start/restart audio preview fetches while Gradio
    is still replacing a large temp file. Waiting for a few equal size samples
    before copying avoids half-written preview files."""
    if not path:
        return False
    deadline = time.time() + timeout
    last_size = -1
    ticks = 0
    while time.time() < deadline:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        if size > 0 and size == last_size:
            ticks += 1
            if ticks >= stable_ticks:
                return True
        else:
            last_size = size
            ticks = 0
        time.sleep(interval)
    return os.path.exists(path)


def _safe_audio_ext(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        ext.encode("ascii")
        if 0 < len(ext) <= 8:
            return ext
    except Exception:
        pass
    return ".wav"


def _stage_upload(path, force_copy=False):
    """上传/录制完成后，把输入音频换成短 ASCII 临时名再交回控件。

    关键点：长音频即使路径本身已经是短 ASCII，也强制复制到一个新的稳定文件。
    否则在同一个 gr.Audio 控件里点 X 清空再上传长音频时，旧预览 fetch 与新预览
    fetch 容易竞态，前端波形会卡住；短音频通常因为读取很快不明显。"""
    if not path or not os.path.exists(path):
        return path

    _wait_file_stable(path)

    # 已经是我们 staging 过的文件，不要再次复制，避免 .change / .upload 回写后循环。
    base = os.path.basename(path)
    if base.startswith(("audiocpp_up_", "audiocpp_rec_", "audiocpp_asc_")):
        return path

    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0

    must_copy = force_copy or size >= 4 * 1024 * 1024
    try:
        path.encode("ascii")
        ascii_short = len(path) < 180
    except UnicodeEncodeError:
        ascii_short = False
        must_copy = True

    if ascii_short and not must_copy:
        return path

    ext = _safe_audio_ext(path)
    fd, out = tempfile.mkstemp(prefix="audiocpp_up_", suffix=ext)
    os.close(fd)
    shutil.copy2(path, out)
    return out


def _stage_recording(path):
    """麦克风录音结束后总是换成一个新的稳定临时文件。"""
    return _stage_upload(path, force_copy=True)


def _ensure_wav(path, target_sr=None):
    """server 端只有 WAV 读取器（其它格式报 invalid WAV RIFF header），Gradio 上传
    的 flac/mp3/ogg/m4a 等在这里先用 ffmpeg 转成 16-bit PCM WAV 临时文件再发；
    已是 RIFF/WAVE 的原样透传。target_sr：模型 prepare() 硬校验采样率的族（音源
    分离要 44.1k）传入目标值，采样率不符的输入（含 WAV）顺带重采样。"""
    if not path:
        return path
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError as e:
        raise gr.Error(f"读不到音频文件 {path}：{e}")
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        if target_sr is None:
            return _ensure_ascii_path(path)
        try:
            with wave.open(path, "rb") as w:
                if w.getframerate() == target_sr:
                    return _ensure_ascii_path(path)
        except Exception:
            pass                    # 非 PCM WAV：wave 读不了，交给 ffmpeg 重写
    key = (path, target_sr)
    cached = _WAV_CACHE.get(key)
    if cached and os.path.exists(cached):
        return cached
    ffmpeg = _find_ffmpeg()
    ext = os.path.splitext(path)[1].lower() or "（无扩展名）"
    fd, out = tempfile.mkstemp(prefix="audiocpp_in_", suffix=".wav")
    os.close(fd)
    cmd = [ffmpeg, "-y", "-v", "error", "-i", path, "-map", "0:a:0"]
    if target_sr:
        cmd += ["-ar", str(target_sr)]
    cmd += ["-c:a", "pcm_s16le", out]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.getsize(out):
        try:
            os.remove(out)
        except OSError:
            pass
        err = (proc.stderr or "").strip()[-300:]
        raise gr.Error(f"ffmpeg 转码 {ext} → wav 失败：{err or '未知错误'}")
    note = f"，重采样到 {target_sr}Hz" if target_sr else ""
    _ui_log(f"输入转码：{os.path.basename(path)}（{ext}）→ 16-bit PCM WAV{note}")
    _WAV_CACHE[key] = out
    return out


def _to_16k_mono_wav(path, target_sr=16000):
    """VAD / 说话人分离 / 强制对齐族要求 16 kHz 单声道输入（Silero、Sortformer 对
    非 16k 直接报错），这里用 wave+numpy 把 PCM WAV 转换成 16k 单声道临时文件。
    已是 16k 单声道、或非 PCM WAV（wave 读不了）时原样透传，由 server 决定成败。"""
    try:
        with wave.open(path, "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except Exception:
        return path
    if sr == target_sr and ch == 1:
        return path
    if sw == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        i24 = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) |
               (b[:, 2].astype(np.int32) << 16))
        i24 -= (i24 & 0x800000) << 1  # sign-extend 24-bit
        data = i24.astype(np.float32) / 8388608.0
    elif sw == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        return path
    if ch > 1:
        data = data[: len(data) // ch * ch].reshape(-1, ch).mean(axis=1)
    if sr != target_sr and len(data) > 0:
        n_out = max(1, int(round(len(data) * target_sr / sr)))
        x_old = np.arange(len(data), dtype=np.float64) / sr
        x_new = np.arange(n_out, dtype=np.float64) / target_sr
        data = np.interp(x_new, x_old, data).astype(np.float32)
    fd, out = tempfile.mkstemp(prefix="audiocpp_16k_", suffix=".wav")
    os.close(fd)
    pcm = np.clip(data * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(target_sr)
        w.writeframes(pcm.tobytes())
    return out


def _split_wav_chunks(path, max_seconds, min_search_frac=0.6, win_ms=50,
                      pad_to_max=True):
    """把 PCM WAV 切成若干段临时 wav，返回 [(路径, 有效占比)]。用于 vevo2 歌声
    转换：FM 图按整段长度一次建图，8G 卡放不下长音频；且同一 server 上不同长度
    的请求会重建图并叠加占用显存（实测 20s 成功后 17s 反要 9.9GB），而形状相同
    的请求可复用缓存图（实测显存平稳）。所以**每段都补零到恰好 max_seconds**，
    占比供拼接时截掉补零对应的尾部输出。切点选在
    [起点+max*min_search_frac, 起点+max] 区间内能量最低的 win_ms 窗口中心
    （典型是呼吸/间奏处）；读不了的（非 PCM WAV）原样返回不分段。
    pad_to_max=False：不补零（ASR 分段转写用——补出的尾部静音只会浪费编码器
    token 甚至诱发幻听，转写也不需要各段图形状一致）。"""
    try:
        with wave.open(path, "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            n = w.getnframes()
            raw = w.readframes(n)
    except Exception:
        return [(path, 1.0)]            # 非 PCM WAV：不分段，交给 server
    if sw != 2 or n <= 0:
        return [(path, 1.0)]
    data = np.frombuffer(raw, dtype=np.int16)
    frames = data.reshape(-1, ch) if ch > 1 else data.reshape(-1, 1)
    mono = np.abs(frames.astype(np.float32)).mean(axis=1)
    win = max(1, int(sr * win_ms / 1000.0))
    env_len = max(1, len(mono) // win)
    env = mono[: env_len * win].reshape(env_len, win).mean(axis=1)

    max_f, lo_f = int(max_seconds * sr), int(max_seconds * min_search_frac * sr)
    spans, pos = [], 0
    while n - pos > max_f:
        w0 = min((pos + lo_f) // win, env_len - 1)
        w1 = min((pos + max_f) // win, env_len)
        wi = w0 + int(np.argmin(env[w0:w1])) if w1 > w0 else w1 - 1
        cut = min(n, wi * win + win // 2)
        spans.append((pos, cut))
        pos = cut
    if pos < n:
        spans.append((pos, n))
    outs = []
    for i, (a, b) in enumerate(spans):
        fd, out = tempfile.mkstemp(prefix=f"audiocpp_vcseg{i}_", suffix=".wav")
        os.close(fd)
        seg = frames[a:b]
        if pad_to_max and len(seg) < max_f:  # 全部段补零到等长，保证图形状一致
            pad = np.zeros((max_f - len(seg), ch), dtype=np.int16)
            seg = np.concatenate([seg, pad], axis=0)
        with wave.open(out, "wb") as ww:
            ww.setnchannels(ch)
            ww.setsampwidth(sw)
            ww.setframerate(sr)
            ww.writeframes(seg.tobytes())
        outs.append((out, (b - a) / float(max_f)))
    return outs


def _trim_wav_seconds(path, max_seconds):
    """把 PCM WAV 截到前 max_seconds 秒。vevo2 的音色参考超过约 10s 对音色几乎
    没有额外贡献，却会让 FM 图的 prompt 段变长、和源音频一起把显存吃满，所以
    转换前先截短。短于上限、或非 PCM WAV（wave 读不了）的原样返回。"""
    try:
        with wave.open(path, "rb") as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            keep = int(max_seconds * sr)
            if keep <= 0 or w.getnframes() <= keep:
                return path
            raw = w.readframes(keep)
    except Exception:
        return path
    fd, out = tempfile.mkstemp(prefix="audiocpp_ref_", suffix=".wav")
    os.close(fd)
    with wave.open(out, "wb") as ww:
        ww.setnchannels(ch)
        ww.setsampwidth(sw)
        ww.setframerate(sr)
        ww.writeframes(raw)
    return out


def _parse_adv_options(raw):
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception as e:
        raise gr.Error(f"高级参数不是合法 JSON：{e}")
    if not isinstance(obj, dict):
        raise gr.Error('高级参数必须是 JSON 对象，例如 {"num_inference_steps": 10}')
    return obj


# Map known server-error fragments to an actionable Chinese hint, so a raw 500
# like "requires a session voice via --voice-ref" becomes "请上传参考音色".
# Ordered specific -> generic; server_error() takes the FIRST match.
ERROR_HINTS = [
    (re.compile(r"failed to allocate .{0,40}graph|out of memory|cudaMalloc", re.I),
     "🧠 显存不足：这次请求的计算图放不进剩余显存，通常是音频/文本太长。"
     "请剪短或分段后重试。"),
    (re.compile(r"invalid WAV RIFF header", re.I),
     "🎵 server 只支持 WAV 音频：上传的文件 webui 会自动转码，"
     "手动填路径的参数（如 voice_samples）请先转成 .wav。"),
    (re.compile(r"unsupported Chatterbox language", re.I),
     "🌐 Chatterbox 只支持 en/es/fr/de/it/pt/ko（无中文/日文/俄文，也没有自动检测）。"
     "请在“语言”里改选受支持的语言，或“留空”用默认（英语）。"),
    (re.compile(r"Stable Audio.{0,80}(English|prompt)|prompt.{0,80}(English|Stable Audio)", re.I),
     "🎵 Stable Audio 的提示词只支持英文。请把“提示词”改成英文后重试。"),
    (re.compile(r"max_source_positions", re.I),
     "⏱ 音频过长：Qwen3-ASR 编码器上限 1500 token（约 13 token/秒），"
     "单次最多约 115 秒。请把音频剪短或分段后再转写。"),
    (re.compile(r"exceeds fixed graph capacity|session_len_sec exceeds", re.I),
     "📏 音频超过模型的固定图容量（Sortformer 默认 20 秒、上限约 120 秒）。"
     "webui 的说话人分离/对话模式会按时长自动重载；仍报错说明超过 120 秒上限，"
     "请先剪短音频。"),
    (re.compile(r"combine voice_samples|voice_samples.{0,20}voice_ref", re.I),
     "🔀 voice_samples 与单个参考音色不能同时用：多说话人时请不要上传参考音色。"),
    (re.compile(r"cached voice id", re.I),
     "🎤 需要参考音频文件（不是 voice id）：请上传/录制一段参考音色。"),
    (re.compile(r"no valid Speaker|Speaker\s+N", re.I),
     "📝 需要多说话人脚本：每行写成 `Speaker 0: 内容`（多角色用 Speaker 0/1/…）。"),
    (re.compile(r"reference[-_ ]?text", re.I),
     "🗒 需要参考文本：在“参考文本”里填参考音频里说的原话。"),
    (re.compile(r"voice[-_ ]?ref|voice[-_ ]?id|session voice|speaker reference|"
                r"requires .{0,40}voice|requires audio", re.I),
     "🎤 该模型需要参考音色：上传/录制一段参考音频，或选一个内置参考音色后重试。"),
]


def _extract_server_message(text):
    try:
        return json.loads(text)["error"]["message"] or text
    except Exception:
        return (text or "").strip()


def server_error(entry, status, text, extra=None):
    """Build a friendly gr.Error from a non-200 server response."""
    msg = _extract_server_message(text)
    hint = next((h for pat, h in ERROR_HINTS if pat.search(msg)), None)
    parts = [f"❌ server {status}"]
    if hint:
        parts.append("💡 " + hint)
    else:
        parts[0] = f"❌ server {status}：{msg[:400]}"
    if extra:
        parts.append(extra)
    if entry and not hint:
        ih = profile_for(entry).get("input_hint")
        if ih:
            parts.append("ℹ️ " + ih)
    return gr.Error("\n\n".join(parts))


def _msg_from_error(e):
    """Human-readable text from a gr.Error/exception, for inline (non-popup) display."""
    return getattr(e, "message", None) or str(e) or "未知错误"


# Fallback catalog if models_catalog.json is missing/unreadable.
DEFAULT_CATALOG = {
    "host": "127.0.0.1", "port": 18080, "device": 0, "threads": 1,
    "models": [
        {"id": "qwen3-tts", "display_name": "Qwen3-TTS 0.6B (tts)",
         "family": "qwen3_tts", "path": "models/Qwen3-TTS-12Hz-0.6B-Base",
         "task": "tts", "mode": "offline"},
        {"id": "vibevoice", "display_name": "VibeVoice 1.5B (tts)",
         "family": "vibevoice", "path": "models/VibeVoice-1.5B",
         "task": "tts", "mode": "offline"},
        {"id": "qwen3-asr", "display_name": "Qwen3-ASR 0.6B (asr)",
         "family": "qwen3_asr", "path": "models/Qwen3-ASR-0.6B",
         "task": "asr", "mode": "offline"},
    ],
}


def _load_catalog():
    if os.path.isfile(CATALOG_PATH):
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[webui] failed to read {CATALOG_PATH}: {e}; using defaults")
    return DEFAULT_CATALOG


def _load_model_params():
    """Per-family advanced-parameter specs for the TTS tab (configs/model_params.json).
    Returns {family: [param_spec, ...]}; a missing/broken file -> {} (no knobs shown)."""
    if os.path.isfile(MODEL_PARAMS_PATH):
        try:
            with open(MODEL_PARAMS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[webui] failed to read {MODEL_PARAMS_PATH}: {e}; no param controls")
    return {}


CATALOG = _load_catalog()
MODEL_PARAMS = _load_model_params()
HOST = CATALOG.get("host", "127.0.0.1")
PORT = int(CATALOG.get("port", 18080))
DEVICE = int(CATALOG.get("device", 0))
THREADS = int(os.environ.get("AUDIOCPP_THREADS") or CATALOG.get("threads", 1))
if BACKEND == "cpu" and THREADS <= 1:
    # catalog 里的 threads=1 是按 CUDA 调的（GPU 路径不吃这个值）；CPU 后端的
    # ggml 计算线程数就是它，单线程没法用 —— 默认全核减一，留一个核给 UI/系统。
    THREADS = max(1, (os.cpu_count() or 4) - 1)

# If the user points us at an existing server, keep everything consistent with it.
_ENV_SERVER = os.environ.get("AUDIOCPP_SERVER")
if _ENV_SERVER:
    _u = urlparse(_ENV_SERVER)
    HOST = _u.hostname or HOST
    PORT = _u.port or PORT
    SERVER = _ENV_SERVER.rstrip("/")
else:
    SERVER = f"http://{HOST}:{PORT}"

# --- managed server process state ------------------------------------------
_proc_lock = threading.Lock()
_server_proc = None      # subprocess.Popen we launched, or None
_loaded_id = None        # model id our managed server is serving
_loaded_session_options = None   # 随本次加载写进 server config 的 session_options


def catalog_models():
    """Catalog entries annotated with abs_path / installed / label."""
    out = []
    for m in CATALOG.get("models", []):
        rel = m.get("path", "")
        ap = rel if os.path.isabs(rel) else os.path.join(BUNDLE_ROOT, rel)
        entry = dict(m)
        entry["abs_path"] = os.path.normpath(ap).replace("\\", "/")
        entry["installed"] = os.path.exists(entry["abs_path"])
        entry["label"] = m.get("display_name") or m.get("id", "?")
        out.append(entry)
    return out


def catalog_by_id(model_id):
    for m in catalog_models():
        if m.get("id") == model_id:
            return m
    return None


def choices_for_tasks(tasks):
    """[(label, id)] for catalog models whose task is in `tasks`; missing ones flagged.
    未安装且估算显存超过本机的条目额外标注最低显存，防止白下载。"""
    out = []
    for m in catalog_models():
        if m.get("task") not in tasks:
            continue
        label = m["label"]
        if not m["installed"]:
            label += " · 未安装"
            short = _vram_shortfall(m)
            if short:
                label += f" ⚠️估算需≥{short[0]:g}G显存"
        out.append((label, m["id"]))
    return out


def builtin_voices():
    if not os.path.isdir(PROMPTS_DIR):
        return []
    return sorted(f for f in os.listdir(PROMPTS_DIR) if f.lower().endswith(".wav"))


def _load_voice_texts():
    """Built-in voice basename -> reference transcript, parsed from
    voice/prompt_text (each line is '<basename>|<transcript>')."""
    texts = {}
    try:
        with open(os.path.join(PROMPTS_DIR, "prompt_text"), "r", encoding="utf-8") as f:
            for line in f:
                name, sep, text = line.rstrip("\n").partition("|")
                if sep:
                    texts[name.strip()] = text
    except Exception:
        pass
    return texts


def refresh_builtin_voices(current):
    """刷新按钮：重新扫描 voice/ 目录的 wav 列表，并按当前选中项重读
    prompt_text 里的参考文本；选中项已被删除时回落到 '(none)'。
    必须在同一个 handler 里连带输出 wav/参考文本——拆成 .then 链会和
    下拉更新触发的 .change 并发执行，撞 Gradio get_config 的竞态
    （RuntimeError: dictionary changed size during iteration）。"""
    choices = ["(none)"] + builtin_voices()
    if current not in choices:
        current = "(none)"
    wav, ref = on_builtin_voice_change(current)
    return gr.update(choices=choices, value=current), wav, ref


def on_builtin_voice_change(name):
    """Selecting a built-in voice mirrors its wav into the upload widget and
    fills the matching reference text; '(none)' clears both."""
    if not name or name == "(none)":
        return None, ""
    path = os.path.join(PROMPTS_DIR, name)
    ref = _load_voice_texts().get(os.path.splitext(name)[0], "")
    return (path if os.path.isfile(path) else None), ref


# --- config-driven advanced-parameter controls (TTS tab) -------------------
def params_for(model_id):
    """Advanced-parameter specs for a model, looked up by its catalog family."""
    entry = catalog_by_id(model_id) if model_id else None
    if not entry:
        return []
    specs = MODEL_PARAMS.get(entry.get("family", ""), [])
    return specs if isinstance(specs, list) else []


def _make_param_component(p):
    """Build one Gradio control from a spec (type: slider|number|bool|text|choice).

    interactive=True is forced: inside @gr.render a control that is only wired to
    its own .change handler is otherwise inferred as output-only (read-only)."""
    t = p.get("type", "number")
    label = p.get("label", p.get("name", ""))
    info = p.get("info")
    if t == "bool":
        return gr.Checkbox(label=label, info=info, value=bool(p.get("default", False)),
                           interactive=True)
    if t == "text":
        return gr.Textbox(label=label, info=info, value=p.get("default", ""),
                          placeholder=p.get("placeholder", ""),
                          lines=int(p.get("lines", 1)), interactive=True)
    if t == "choice":
        return gr.Dropdown(label=label, info=info, choices=p.get("choices", []),
                           value=p.get("default"), interactive=True)
    if t == "slider":
        return gr.Slider(label=label, info=info,
                         minimum=p.get("minimum", 0), maximum=p.get("maximum", 1),
                         step=p.get("step", 0.01), value=p.get("default", 0),
                         interactive=True)
    return gr.Number(label=label, info=info, value=p.get("default"),
                     minimum=p.get("minimum"), maximum=p.get("maximum"),
                     step=p.get("step"), precision=p.get("precision"),
                     interactive=True)


def _adv_updater(name):
    """change-handler that writes one control's value into the shared advanced
    options state dict (keyed by the option name)."""
    def _fn(state, value):
        state = dict(state or {})
        state[name] = value
        return state
    return _fn


# --- server lifecycle ------------------------------------------------------
def _port_open(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def server_alive():
    try:
        requests.get(f"{SERVER}/health", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


def loaded_ids():
    try:
        r = requests.get(f"{SERVER}/v1/models", timeout=5)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", [])]
    except Exception:
        return []


def _write_temp_config(entry):
    model = {
        "id": entry["id"],
        "family": entry["family"],
        "path": entry["abs_path"],
        "task": entry.get("task", "tts"),
        "mode": entry.get("mode", "offline"),
    }
    for key in ("config", "weight", "load_options", "session_options"):
        if entry.get(key) is not None:
            model[key] = entry[key]
    cfg = {"host": HOST, "port": PORT, "backend": SERVER_BACKEND, "device": DEVICE,
           "threads": THREADS, "models": [model]}
    fd, path = tempfile.mkstemp(prefix="audiocpp_webui_cfg_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def _stop_server():
    global _server_proc, _loaded_id, _loaded_session_options
    _loaded_session_options = None
    proc, _server_proc, _loaded_id = _server_proc, None, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except Exception:
            pass
    for _ in range(24):  # let the OS release the port
        if not _port_open(HOST, PORT):
            break
        time.sleep(0.25)


def _read_tail(path, n=30, max_bytes=65536):
    """Last n non-empty lines of a file. Reads only the file's tail, and treats
    \\r as a line break so tqdm-style progress (one giant \\r-line) shows its
    latest state instead of nothing."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
        lines = [ln for ln in data.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                 if ln.strip()]
        return "\n".join(lines[-n:]).strip()
    except Exception:
        return ""


def _log_tail(n=30):
    return _read_tail(LOG_PATH, n)


# One shared append handle for the WebUI log: the pump thread (server output)
# and _ui_log (webui-side request events) both write through it, so lines
# interleave correctly instead of two handles overwriting each other.
_log_lock = threading.Lock()
_log_fh = None


def _open_log_file(truncate=False):
    global _log_fh
    with _log_lock:
        if _log_fh is not None:
            try:
                _log_fh.close()
            except Exception:
                pass
        _log_fh = open(LOG_PATH, "w" if truncate else "a",
                       encoding="utf-8", errors="replace")


def _log_write(text):
    global _log_fh
    with _log_lock:
        if _log_fh is None:
            try:
                _log_fh = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
            except Exception:
                return
        try:
            _log_fh.write(text)
            _log_fh.flush()
        except Exception:
            pass


def _ts():
    return time.strftime("%H:%M:%S")


def _emit_log_line(text):
    """One already-formatted line to BOTH the console and the WebUI log file."""
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        pass
    _log_write(text)


def _ui_log(msg):
    """Timestamped webui-side event (request start/finish, model load, ...) so
    the console/log show when things began and ended, not just server spam."""
    _emit_log_line(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [webui] {msg}\n")


def _pump_server_output(proc):
    """Tee the server's combined stdout/stderr to console + log file, prefixing
    every line with a timestamp and collapsing consecutive duplicate lines
    (e.g. the repeated `CUDA graph warmup complete`) into a periodic counter."""
    last, repeats = None, 0
    try:
        for line in proc.stdout:
            if line == last:
                repeats += 1
                if repeats % 50 == 0:
                    _emit_log_line(f"[{_ts()}]   ... 上一行已重复 {repeats} 次\n")
                continue
            if repeats:
                _emit_log_line(f"[{_ts()}]   ... (上一行共重复 {repeats} 次)\n")
            last, repeats = line, 0
            _emit_log_line(f"[{_ts()}] {line}")
    except Exception:
        pass
    finally:
        if repeats:
            _emit_log_line(f"[{_ts()}]   ... (上一行共重复 {repeats} 次)\n")


def _start_server(entry):
    global _server_proc, _loaded_id
    if not os.path.isfile(SERVER_EXE):
        raise gr.Error(f"找不到 server: {SERVER_EXE}（用 AUDIOCPP_BACKEND=gpu|cpu 指定）")
    cfg = _write_temp_config(entry)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    _open_log_file(truncate=True)
    # --log 会打开 engine 的 [TRACE]/[TIMING] 调试输出，日常太吵，默认关闭；
    # 排查推理问题时设 AUDIOCPP_SERVER_DEBUG=1 再启动 webui。
    cmd = [SERVER_EXE, "--config", cfg, "--host", HOST, "--port", str(PORT)]
    if os.environ.get("AUDIOCPP_SERVER_DEBUG") == "1":
        cmd.append("--log")
    _server_proc = subprocess.Popen(
        cmd,
        cwd=BUNDLE_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=flags, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    _loaded_id = entry["id"]
    extra = f"，threads={THREADS}" if SERVER_BACKEND == "cpu" else ""
    _ui_log(f"启动 audiocpp_server（backend={SERVER_BACKEND}{extra}），加载模型 {entry['label']} …")
    threading.Thread(target=_pump_server_output, args=(_server_proc,),
                     daemon=True).start()


def _wait_health(timeout):
    start = time.time()
    while time.time() - start < timeout:
        if _server_proc is not None and _server_proc.poll() is not None:
            return False  # process exited before becoming healthy
        if server_alive():
            return True
        time.sleep(0.5)
    return False


def ensure_model_loaded(model_id, expect_tasks=None, session_options=None):
    """(Re)start the server so `model_id` is loaded. Returns a status string.
    session_options：额外写进本次 server config 的 session_options（string→string，
    如 sortformer 的 session_len_sec）；与上次加载不一致时会重启重载。"""
    global _loaded_session_options
    if not model_id:
        raise gr.Error("请先选择一个模型")
    entry = catalog_by_id(model_id)
    if entry is None:
        raise gr.Error(f"catalog 里没有模型 id: {model_id}")
    if not entry["installed"]:
        raise gr.Error(f"模型未安装（目录不存在）: {entry['abs_path']}")
    if expect_tasks and entry.get("task") not in expect_tasks:
        raise gr.Error(
            f"模型 {model_id} 的 task 是 {entry.get('task')}，此处需要 {'/'.join(expect_tasks)}")

    with _proc_lock:
        managed_alive = _server_proc is not None and _server_proc.poll() is None
        if (managed_alive and _loaded_id == model_id and server_alive()
                and (session_options or {}) == (_loaded_session_options or {})):
            return f"✅ 已加载：{entry['label']}"

        if not managed_alive and server_alive():
            # A server we didn't launch is holding the port.
            if session_options:
                raise gr.Error(
                    f"当前 {HOST}:{PORT} 上是外部启动的 server，无法按需调整其"
                    f"session 配置（需要 {session_options}）。请先关闭它。")
            if model_id in loaded_ids():
                return f"✅ 复用外部 server：{entry['label']}"
            raise gr.Error(
                f"检测到一个非本 WebUI 启动的 server 正占用 {HOST}:{PORT}"
                f"（例如 run_server.bat）。请先关闭它，或设 AUDIOCPP_SERVER 指向它。")

        _stop_server()
        if session_options:
            entry = dict(entry)
            entry["session_options"] = {
                **(entry.get("session_options") or {}), **session_options}
        t0 = time.time()
        _start_server(entry)
        _loaded_session_options = dict(session_options) if session_options else None
        if not _wait_health(LOAD_TIMEOUT):
            tail = _log_tail()
            _stop_server()
            _ui_log(f"模型 {entry['label']} 加载失败/超时（{LOAD_TIMEOUT}s）")
            raise gr.Error(f"加载 {entry['label']} 失败/超时（{LOAD_TIMEOUT}s）。\n日志尾部：\n{tail}")
        _ui_log(f"模型 {entry['label']} 加载完成，用时 {time.time() - t0:.1f}s")
        return f"✅ 已加载：{entry['label']}"


def unload_model():
    """停止本 WebUI 启动的 server，释放全部显存（权重+常驻计算图缓冲）。
    下次生成/转写时 ensure_model_loaded 会自动重启重载（实测 ~5s），转写速度
    本身不受影响（计算图本来就按每次请求的音频长度分配/复用）。"""
    with _proc_lock:
        managed_alive = _server_proc is not None and _server_proc.poll() is None
        if managed_alive:
            label = _loaded_id or "(unknown)"
            _stop_server()
            _ui_log(f"已卸载模型 {label} 并停止 server，显存已释放")
            return ("🧹 已卸载模型并释放显存 — 下次点『加载模型』或直接生成/转写"
                    "会自动重新加载（约 5s）。", server_status())
        if server_alive():
            return ("⚠️ 当前 server 不是本 WebUI 启动的（如 run_server.bat），"
                    "请到启动它的窗口里关闭。", server_status())
        return "⚪ server 未运行，无需释放。", server_status()


def server_status():
    if server_alive():
        ids = ", ".join(loaded_ids()) or "(none)"
        return f"✅ server @ {SERVER} · backend={BACKEND} · model id={ids}"
    return f"⚪ server 未运行 @ {SERVER} — 选择模型并点『📥 加载模型』"


def _api_usage_md():
    """状态行下方折叠区的第三方调用说明。端点/字段以 app/server/README.md 为准；
    URL 取运行时的 SERVER，避免和 AUDIOCPP_SERVER / catalog 配置不一致。"""
    return f"""
第三方应用可以直接调用本 WebUI 启动的 `audiocpp_server`（OpenAI 风格 HTTP API），不经过本页面。

- **URL 怎么填**：API 地址是 `{SERVER}`；OpenAI 兼容客户端的 Base URL 填 `{SERVER}/v1`。
  生成语音：`POST {SERVER}/v1/audio/speech` · 音频转写：`POST {SERVER}/v1/audio/transcriptions`
- **模型名称怎么填**：`model` 填模型 id（与本页模型列表一致，如 `qwen3-tts`、`vibevoice`），
  且必须是**当前已加载**的那个 —— server 同一时刻只驻留一个模型，先在本页点『📥 加载模型』；
  可用 `GET {SERVER}/v1/models` 查看当前可用的 id。
- **TTS 请求示例**（响应默认是 WAV 音频；加 `"response_format": "json"` 改为返回 base64 的 JSON）：

```bash
curl {SERVER}/v1/audio/speech -H "Content-Type: application/json" -o out.wav \\
  -d '{{"model": "qwen3-tts", "input": "你好，audio.cpp。", "voice_ref": "D:/voices/ref.wav", "reference_text": "参考音频里的原话", "seed": 1234}}'
```

- **ASR 请求示例**：`-d '{{"model": "qwen3-asr", "audio": "D:/audio/in.wav"}}'`；
  可选 `"language"`（强制语种，如 `"Chinese"`）和 `"context"`（人名/术语偏置提示），仅 qwen3-asr 生效。
  注意 `voice_ref` / `audio` 填的都是 **server 所在机器上的文件路径**（不是浏览器上传）。
- **音乐生成（gen 模型）**：走通用路由 `POST {SERVER}/v1/tasks/run`，body 形如
  `{{"model": "ace-step", "request": {{"text": "提示词", "lyrics": "歌词", "duration_seconds": 30,
  "options": {{"tags": "pop,bright"}}}}}}`，响应 JSON 的 `audio` 字段是 base64 WAV。
- **其它任务（vc/svc/s2s/sep/vad/diar/align）**：同样走 `POST {SERVER}/v1/tasks/run`，`request` 里用
  `audio`（源音频路径）/ `voice_ref`（目标音色）/ `text`（对齐文本）等字段；分离多轨在响应的
  `named_audio_outputs`，VAD/说话人/对齐结果在 `segments` / `speaker_turns` / `words`。
- server 的生命周期跟随本 WebUI 的**命令行窗口**：只关浏览器页面不影响，server 仍可被第三方调用；
  关掉命令窗口（webui.py 退出）才会连带关闭它。也可单独启动 server（如 run_server.bat）
  供第三方应用调用。
"""


# --- background model downloads (via tools/model_manager.py) ----------------
_dl_lock = threading.Lock()
_downloads = {}  # model_id -> {"proc": Popen, "log": path}


def _dl_log_path(model_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", model_id)
    return os.path.join(LOG_DIR, f"download_{safe}.log")


def _dir_size_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _fmt_bytes(n):
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB"


def _download_progress_note(entry):
    """Bytes already on disk for a running download. model_manager stages into
    models/.engine_model_staging/<target>.partial/ and renames on completion;
    fall back to the whole staging root for packages with composite targets."""
    staging_root = os.path.join(MODELS_ROOT, ".engine_model_staging")
    base = os.path.basename(entry.get("path", "").rstrip("/\\"))
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", base)
    staging = os.path.join(staging_root, safe + ".partial")
    probe = staging if os.path.isdir(staging) else staging_root
    if not os.path.isdir(probe):
        return "尚未写入数据（正在连接/解析）"
    return f"已下载 {_fmt_bytes(_dir_size_bytes(probe))}"


def hf_token_present():
    """True if model_manager will find an HF token (env or cached login)."""
    if os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"):
        return True
    return os.path.isfile(os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "token"))


def download_model(model_id, hf_token="", proxy=""):
    """Kick off `model_manager.py install <download_id>` in the background."""
    if not model_id:
        return "❌ 请先选择一个模型"
    entry = catalog_by_id(model_id)
    if entry is None:
        return f"❌ catalog 里没有模型 id: {model_id}"
    if entry["installed"]:
        return f"✅ {entry['label']} 已安装，无需下载"
    dl_id = entry.get("download_id")
    if not dl_id:
        return f"⚠️ {entry['label']} 没有配置 download_id（内置资源/依赖），请手动安装"
    if MODEL_MANAGER is None:
        return "❌ 找不到 tools/model_manager.py（设 AUDIOCPP_MODEL_MANAGER 指向它）"

    # Pass a token to the child so gated/private HF repos don't 401.
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"    # progress lines land in the log immediately
    env["PYTHONIOENCODING"] = "utf-8"
    tok = (hf_token or "").strip()
    if tok:
        env["HF_TOKEN"] = tok
        env["HUGGING_FACE_HUB_TOKEN"] = tok
    # Route the child's downloads through a proxy (urllib reads these env vars).
    px = (proxy or "").strip()
    proxy_note = ""
    if px:
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[var] = px
        proxy_note = f"🌐 通过代理 {px}\n\n"
    warn = "" if (tok or hf_token_present()) else (
        "⚠️ 未检测到 HF token —— 受限/gated 模型会 401。请在上方填入 token"
        "（或先 `huggingface-cli login`），受限模型还需先到其 HF 页面点同意。\n\n")
    short = _vram_shortfall(entry)
    if short:
        warn = (f"⚠️ **显存不足警告**：该模型估算需 **≥{short[0]:g}G** 显存，本机只有 "
                f"**{short[1]:g}G**——下载后大概率溢出到共享显存、速度很慢甚至跑不动。\n\n") + warn

    with _dl_lock:
        rec = _downloads.get(model_id)
        if rec and rec["proc"].poll() is None:
            return f"⏳ {entry['label']} 已经在后台下载中…\n```\n{_read_tail(rec['log'])}\n```"
        log = _dl_log_path(model_id)
        logf = open(log, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            [sys.executable, "-u", MODEL_MANAGER, "install", dl_id,
             "--models-root", MODELS_ROOT, "--overwrite"],
            cwd=PROJECT_ROOT, stdout=logf, stderr=subprocess.STDOUT, env=env)
        _downloads[model_id] = {"proc": proc, "log": log}
    _ui_log(f"开始后台下载 {entry['label']}（{dl_id}），日志：{log}")
    return (f"{warn}{proxy_note}⏳ 已开始后台下载 **{entry['label']}**（{dl_id}），"
            f"下方进度每几秒自动刷新。\n完成后点旁边的『🔄 刷新列表』即可加载。\n日志：{log}")


def download_status(model_id):
    entry = catalog_by_id(model_id) if model_id else None
    if entry is None:
        return ""
    if entry["installed"]:
        return f"✅ {entry['label']} 已安装"
    rec = _downloads.get(model_id)
    if rec is None:
        return f"⚪ {entry['label']} 未安装，未开始下载"
    code = rec["proc"].poll()
    tail = _read_tail(rec["log"], n=12)
    if code is None:
        return (f"⏳ 正在下载 {entry['label']}… {_download_progress_note(entry)}"
                f" · 更新于 {_ts()}\n```\n{tail}\n```")
    if not rec.get("reported"):
        rec["reported"] = True
        _ui_log(f"{entry['label']} 下载进程结束 (exit {code})")
    if code == 0:
        return f"✅ {entry['label']} 下载进程完成，点旁边的『🔄 刷新列表』刷新可用列表。\n```\n{tail}\n```"
    return f"❌ {entry['label']} 下载失败 (exit {code})。\n```\n{tail}\n```"


def _download_running(model_id):
    rec = _downloads.get(model_id) if model_id else None
    return rec is not None and rec["proc"].poll() is None


def download_start(model_id, hf_token="", proxy=""):
    """Click handler: kick off the download and arm the auto-refresh timer."""
    msg = download_model(model_id, hf_token, proxy)
    return msg, gr.Timer(active=_download_running(model_id))


def download_status_tick(model_id):
    """Timer tick: refresh status; stop the timer once the download is idle."""
    return download_status(model_id), gr.Timer(active=_download_running(model_id))


# --- task handlers ---------------------------------------------------------
# Task handlers return (output, message): the reminder/status message is shown
# inline under the output widget instead of as a Gradio popup card.
def _merged_options(prof, adv_values, adv_options):
    """请求 options 合并：family 默认值 -> 生成控件（仅用户改过的项）-> JSON 兜底框。"""
    options = dict(prof.get("default_options", {}))
    if isinstance(adv_values, dict):
        options.update({k: v for k, v in adv_values.items()
                        if v is not None and v != ""})
    options.update(_parse_adv_options(adv_options))
    return options


def _run_task(entry, model, req, timeout, log_label):
    """POST /v1/tasks/run（通用任务路由）并返回响应 JSON；
    连接失败 / 非 200 统一转成带提示的 gr.Error。"""
    try:
        r = requests.post(f"{SERVER}/v1/tasks/run",
                          json={"model": model, "request": req}, timeout=timeout)
    except requests.RequestException as e:
        _ui_log(f"{log_label}失败：无法连接 server")
        raise gr.Error(f"无法连接 server @ {SERVER}：{e}\n💡 server 可能已退出，重新点『📥 加载模型』。")
    if r.status_code != 200:
        _ui_log(f"{log_label}失败：server {r.status_code}")
        raise server_error(entry, r.status_code, r.text)
    return r.json()


def _resolve_seed(seed):
    """seed=-1 → 每次请求随机抽一个。server/C++ 侧 seed 一律按无符号整数解析
    （多数族 u32，seed_vc/stable_audio u64），负数会直接报错，所以 -1 只能在
    客户端消化；随机范围取 u32 全集，对所有族安全。返回 (seed, 消息后缀)——
    后缀把实际用的 seed 回显在结果里，方便复现。"""
    s = int(seed)
    if s != -1:
        return s, ""
    s = random.randrange(0, 2 ** 32)
    return s, f"🎲 seed={s}"


def do_tts(model, text, language, uploaded_voice, builtin_voice,
           reference_text, seed, max_tokens, adv_values, adv_options,
           progress=gr.Progress()):
    try:
        if not (text or "").strip():
            raise gr.Error("请输入要合成的文字")

        entry = catalog_by_id(model)
        prof = profile_for(entry) if entry else DEFAULT_PROFILE

        if prof.get("wrap_speaker_script"):     # e.g. VibeVoice needs Speaker N: lines
            text = _as_speaker_script(text)
            text = _vibevoice_punctuate_script(text)

        # 超短文本拦截（见 _VIBEVOICE_MIN_EST_TOKENS）——放在模型加载之前，
        # 免得为一个注定被拒的请求重启 server。
        is_vibevoice = bool(entry) and entry.get("family") == "vibevoice"
        if is_vibevoice and _vibevoice_text_max_tokens(text) < _VIBEVOICE_MIN_EST_TOKENS:
            raise gr.Error(
                "VibeVoice 是长文模型，过短的文本会整段胡言乱语（模型特性，调参数救不了）。"
                "请把正文加长到 ≥40 个汉字（英文约 ≥35 词），"
                "或改用 qwen3-tts / voxcpm2 / pocket-tts 等对短句稳定的模型。")

        ensure_model_loaded(model, TTS_TASKS)

        # Model-specific knobs travel in a nested "options" object; the server merges
        # every key into the request options and each model reads what it understands.
        options = _merged_options(prof, adv_values, adv_options)

        voice_path = None
        if uploaded_voice:                      # gradio gives an absolute temp path
            voice_path = uploaded_voice
        elif builtin_voice and builtin_voice != "(none)":
            voice_path = os.path.join(PROMPTS_DIR, builtin_voice)
        has_voice_samples = "voice_samples" in options or "vibevoice.voice_samples" in options
        if is_vibevoice and voice_path and not has_voice_samples:
            options["voice_samples"] = _ensure_wav(voice_path)
            voice_path = None
        # voice_samples (multi-speaker) can't be combined with a single voice_ref.
        if has_voice_samples and voice_path:
            voice_path = None

        seed, seed_note = _resolve_seed(seed)
        payload = {
            "model": model,
            "language": resolve_language(prof, language),
            "seed": seed,
            "max_tokens": int(max_tokens),
        }
        auto_vibevoice_max_tokens = (
            is_vibevoice and int(max_tokens) == 1200
        )
        if voice_path:
            payload["voice_ref"] = _ensure_wav(voice_path)
        if (reference_text or "").strip():
            payload["reference_text"] = reference_text
        if options:
            payload["options"] = options

        # Long text goes out as several bounded requests (concatenated below), so a
        # whole chapter neither hits the per-request timeout nor runs blind.
        chunks = _split_tts_chunks(text, prof.get("chunk_chars", 1000))
        if is_vibevoice:
            chunks = _merge_short_vibevoice_tail(chunks)
        _ui_log(f"TTS 开始：model={model}，{len(chunks)} 段 / 共 {sum(len(c) for c in chunks)} 字")
        t_start = time.time()
        blobs = []
        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                progress((i, len(chunks)), desc=f"合成 {i + 1}/{len(chunks)} 段…")
            payload["input"] = chunk
            if auto_vibevoice_max_tokens:
                payload["max_tokens"] = _vibevoice_text_max_tokens(chunk, int(max_tokens))
            t_chunk = time.time()
            try:
                r = requests.post(f"{SERVER}/v1/audio/speech", json=payload, timeout=900)
            except requests.RequestException as e:
                _ui_log(f"TTS 失败：段 {i + 1}/{len(chunks)} 无法连接 server")
                raise gr.Error(f"无法连接 server @ {SERVER}：{e}\n💡 server 可能已退出，重新点『📥 加载模型』。")
            if r.status_code != 200:
                _ui_log(f"TTS 失败：段 {i + 1}/{len(chunks)}，server {r.status_code}")
                raise server_error(entry, r.status_code, r.text)
            blobs.append(r.content)
            _ui_log(f"TTS 段 {i + 1}/{len(chunks)} 完成（{len(chunk)} 字，"
                    f"{time.time() - t_chunk:.1f}s）")

        out = os.path.join(OUTPUT_DIR, f"audiocpp_tts_{int(time.time()*1000)}.wav")
        if len(blobs) == 1:
            with open(out, "wb") as f:
                f.write(blobs[0])
        else:
            _concat_wavs(blobs, out)
        elapsed = time.time() - t_start
        _ui_log(f"TTS 完成：{out}，总用时 {elapsed:.1f}s")
        parts_note = f"（{len(blobs)} 段）" if len(blobs) > 1 else ""
        return out, f"✅ 生成完成{parts_note}，用时 {elapsed:.1f}s。{seed_note}"
    except gr.Error as e:
        return None, _msg_from_error(e)
    except Exception as e:
        return None, f"❌ 生成失败：{e}"


def _asr_transcribe_wav(model, entry, prof, wav_path, extras, tag="ASR"):
    """转写一个 WAV 文件（调用方已加载好 ASR 模型）：超过单次上限（见 qwen3_asr
    profile：8G 卡显存实测取 60s）时在静音处切段逐段转写再拼接；短音频/非 PCM
    WAV（测不出时长）走单请求。extras 是可选的 language/context 请求字段。
    返回 (text, dur, 段数)。"""
    dur = _audio_duration_seconds(wav_path)
    dur_note = f"{dur:.1f}s" if dur is not None else "未知"
    max_s = prof.get("max_input_seconds")
    chunks = [wav_path]
    if max_s and dur is not None and dur > max_s:
        chunks = [p for p, _ in
                  _split_wav_chunks(wav_path, max_s, pad_to_max=False)]
        if len(chunks) > 1:
            _ui_log(f"{tag} 长音频分段：{dur_note} → {len(chunks)} 段"
                    f"（每段 ≤{max_s:.0f}s，静音处切分）")
    texts = []
    for i, chunk in enumerate(chunks):
        t_chunk = time.time()
        payload = {"model": model, "audio": chunk, **extras}
        try:
            r = requests.post(f"{SERVER}/v1/audio/transcriptions", json=payload, timeout=900)
        except requests.RequestException as e:
            _ui_log(f"{tag} 失败：无法连接 server")
            raise gr.Error(f"无法连接 server @ {SERVER}：{e}\n💡 server 可能已退出，重新点『📥 加载模型』。")
        if r.status_code != 200:
            seg_note = f"，第 {i + 1}/{len(chunks)} 段" if len(chunks) > 1 else ""
            _ui_log(f"{tag} 失败：server {r.status_code}（音频 {dur_note}{seg_note}）")
            extra = f"⏱ 本次音频时长约 {dur:.1f} 秒" if dur is not None else None
            raise server_error(entry, r.status_code, r.text, extra=extra)
        try:
            data = r.json()
            texts.append((data.get("text") or str(data)).strip())
        except Exception:
            texts.append(r.text)
        if len(chunks) > 1:
            _ui_log(f"{tag} 段 {i + 1}/{len(chunks)} 完成"
                    f"（{time.time() - t_chunk:.1f}s）")
    text = texts[0] if len(chunks) == 1 else "\n".join(t for t in texts if t)
    return text, dur, len(chunks)


def do_asr(model, audio_path, language="", context="", dialogue=False):
    try:
        if not audio_path:
            raise gr.Error("请上传或录制音频")
        audio_path = _ensure_wav(audio_path)
        # 可选转写参数：留空不发，请求体和原来完全一致（qwen3_asr 从 text_input
        # 读 context/language，其它族忽略；server 端 build_openai_transcription_request
        # 只在字段存在时才设置 text_input）。
        extras = {}
        if (language or "").strip():
            extras["language"] = language.strip()
        if (context or "").strip():
            extras["context"] = context.strip()
        if dialogue:
            return _asr_dialogue(model, audio_path, extras)
        ensure_model_loaded(model, ASR_TASKS)
        entry = catalog_by_id(model)
        prof = profile_for(entry) if entry else DEFAULT_PROFILE
        extras_note = "".join(f"，{k}={v[:20]}" for k, v in extras.items())
        _ui_log(f"ASR 开始：model={model}{extras_note}")
        t_start = time.time()
        text, dur, n = _asr_transcribe_wav(model, entry, prof, audio_path, extras)
        elapsed = time.time() - t_start
        dur_note = f"{dur:.1f}s" if dur is not None else "未知"
        parts_note = f"，{n} 段" if n > 1 else ""
        _ui_log(f"ASR 完成：音频 {dur_note}{parts_note}，用时 {elapsed:.1f}s")
        return text, f"✅ 转写完成（音频 {dur_note}{parts_note}），用时 {elapsed:.1f}s。"
    except gr.Error as e:
        return "", _msg_from_error(e)
    except Exception as e:
        return "", f"❌ 转写失败：{e}"


# 对话模式：同一说话人相邻发言段合并的最大间隔 / 每段前后补的余量（防止
# Sortformer 边界切掉字头字尾）/ 短于该值的发言段丢弃（多为口头禅、气口）。
DIALOGUE_MERGE_GAP_S = 1.0
DIALOGUE_PAD_S = 0.25
DIALOGUE_MIN_SEG_S = 0.3

# Sortformer CUDA 下是固定图容量：session_len_sec 定容量（默认 20s），上限来自
# tf_encoder.max_source_positions=1500 @ 12.5 编码帧/秒 = 120s。
SORTFORMER_MAX_SEC = 120


def _diar_session_options(dur):
    """说话人分离的动态 session_len_sec：≤20s 用默认容量（不重载）；更长的按
    30s 步进向上取整重载（避免每个文件都重启 server）；超过 120s 上限报错。"""
    if dur is None or dur <= 20:
        return None
    if dur > SORTFORMER_MAX_SEC:
        raise gr.Error(
            f"说话人分离单次最长约 {SORTFORMER_MAX_SEC} 秒"
            f"（Sortformer 固定图/位置编码上限），请先把音频剪短。")
    return {"session_len_sec": str(min(SORTFORMER_MAX_SEC,
                                       ((int(dur) // 30) + 1) * 30))}


def _fmt_mmss(sec):
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def _first_installed_model(task):
    for m in catalog_models():
        if m.get("task") == task and m.get("installed"):
            return m
    return None


def _asr_dialogue(model, wav_path, extras):
    """对话模式：Sortformer 说话人分离 → 按说话人合并/切段 → 逐段 ASR →
    带说话人标签和时间戳的对话稿。server 一次只驻留一个模型，所以先换载 diar
    再换回 ASR（每次自动换载 ~5s）。"""
    diar = _first_installed_model("diar")
    if diar is None:
        raise gr.Error("对话模式需要说话人分离模型：请到『🔍 音频分析』页"
                       "下载安装 Sortformer diar 后重试。")
    entry = catalog_by_id(model)
    prof = profile_for(entry) if entry else DEFAULT_PROFILE

    wav16 = _to_16k_mono_wav(wav_path)
    try:
        with wave.open(wav16, "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception as e:
        raise gr.Error(f"对话模式需要可读的 PCM WAV（切段用），当前文件读不了：{e}")
    samples = np.frombuffer(raw, dtype=np.int16)
    total_dur = len(samples) / float(sr)

    t_start = time.time()
    _ui_log(f"对话模式：先用 {diar['id']} 做说话人分离（音频 {total_dur:.1f}s）")
    ensure_model_loaded(diar["id"], ("diar",),
                        session_options=_diar_session_options(total_dur))
    data = _run_task(diar, diar["id"], {"audio": wav16}, timeout=900,
                     log_label="说话人分离")
    turns = data.get("speaker_turns") or []
    if not turns:
        raise gr.Error("没有检测到说话人发言段——音频里可能没有语音。")

    # 合并同一说话人的相邻发言段（间隔 ≤1s 且合并后不超过 ASR 单次上限），
    # 减少请求数并给 ASR 更完整的上下文。
    max_len = (prof.get("max_input_seconds") or 60) * sr
    merged = []                                     # [说话人, start, end] (样本数)
    for t in sorted(turns, key=lambda t: t["start_sample"]):
        spk, s, e = str(t.get("speaker_id", "?")), t["start_sample"], t["end_sample"]
        if (merged and merged[-1][0] == spk
                and s - merged[-1][2] <= DIALOGUE_MERGE_GAP_S * sr
                and e - merged[-1][1] <= max_len):
            merged[-1][2] = max(merged[-1][2], e)
        else:
            merged.append([spk, s, e])
    merged = [m for m in merged if m[2] - m[1] >= DIALOGUE_MIN_SEG_S * sr]
    if not merged:
        raise gr.Error("说话人发言段都太短，无法转写。")
    _ui_log(f"说话人分离完成：{len(turns)} 个发言段 → 合并为 {len(merged)} 段；"
            f"换回 {model} 逐段转写")

    ensure_model_loaded(model, ASR_TASKS)
    pad = int(DIALOGUE_PAD_S * sr)
    speakers, lines = [], []
    for idx, (spk, s, e) in enumerate(merged, 1):
        a, b = max(0, s - pad), min(len(samples), e + pad)
        fd, seg_path = tempfile.mkstemp(prefix=f"audiocpp_dlg{idx}_", suffix=".wav")
        os.close(fd)
        with wave.open(seg_path, "wb") as ww:
            ww.setnchannels(1)
            ww.setsampwidth(2)
            ww.setframerate(sr)
            ww.writeframes(samples[a:b].tobytes())
        text, _, _ = _asr_transcribe_wav(model, entry, prof, seg_path, extras,
                                         tag=f"对话段 {idx}/{len(merged)}")
        if spk not in speakers:
            speakers.append(spk)
        label = f"说话人{speakers.index(spk) + 1}"
        if text:
            lines.append(f"[{_fmt_mmss(s / sr)}-{_fmt_mmss(e / sr)}] {label}: {text}")
        _ui_log(f"对话段 {idx}/{len(merged)} 完成（{label}，{(e - s) / sr:.1f}s）")

    elapsed = time.time() - t_start
    _ui_log(f"对话转写完成：{len(merged)} 段发言、{len(speakers)} 个说话人，"
            f"用时 {elapsed:.1f}s")
    if not lines:
        return "", "⚠️ 说话人分离找到了发言段，但都没有转写出文字。"
    return ("\n".join(lines),
            f"✅ 对话转写完成（音频 {total_dur:.1f}s，{len(merged)} 段发言、"
            f"{len(speakers)} 个说话人），用时 {elapsed:.1f}s。")


def do_music_gen(model, text, lyrics, source_audio, duration, seed,
                 adv_values, adv_options):
    """Music/SFX generation via the generic /v1/tasks/run route. The request
    object uses the CLI request-JSON fields (text/lyrics/duration_seconds/
    task_route/audio + an options map); the response carries base64 WAV."""
    try:
        if not (text or "").strip():
            raise gr.Error("请输入提示词（想要什么样的音乐/音效）")
        ensure_model_loaded(model, GEN_TASKS)
        entry = catalog_by_id(model)
        prof = profile_for(entry) if entry else DEFAULT_PROFILE
        options = _merged_options(prof, adv_values, adv_options)

        seed, seed_note = _resolve_seed(seed)
        req = {"text": text, "seed": seed}
        # task_route is a top-level request field (not a model option); the
        # generated controls funnel everything through `options`, so lift it out.
        route = options.pop("task_route", None)
        if route:
            req["task_route"] = route
        if (lyrics or "").strip():
            req["lyrics"] = lyrics
        if duration is not None and float(duration) != 0:
            req["duration_seconds"] = float(duration)
        if source_audio:
            req["audio"] = _ensure_wav(source_audio)
        if options:
            req["options"] = options

        dur_note = req.get("duration_seconds", "自动")
        _ui_log(f"音乐生成开始：model={model}，目标时长 {dur_note}s")
        t_start = time.time()
        data = _run_task(entry, model, req, timeout=1800, log_label="音乐生成")
        b64 = data.get("audio")
        if not b64 and data.get("named_audio_outputs"):
            b64 = data["named_audio_outputs"][0].get("audio")
        if not b64:
            raise gr.Error("server 返回里没有音频数据（该模型可能不输出音频）")
        out = os.path.join(OUTPUT_DIR, f"audiocpp_gen_{int(time.time()*1000)}.wav")
        with open(out, "wb") as f:
            f.write(base64.b64decode(b64))
        elapsed = time.time() - t_start
        _ui_log(f"音乐生成完成：{out}，用时 {elapsed:.1f}s")
        return out, f"✅ 生成完成，用时 {elapsed:.1f}s。{seed_note}"
    except gr.Error as e:
        return None, _msg_from_error(e)
    except Exception as e:
        return None, f"❌ 生成失败：{e}"


# analyze 返回的字段 -> ACE-Step 高级参数（model_params.json 里的 name）
_ANALYZE_FILL_MAP = (("caption", "source_caption"), ("lyrics", "source_lyrics"),
                     ("bpm", "bpm"), ("keyscale", "keyscale"),
                     ("timesignature", "timesignature"))


def do_music_analyze(model, source_audio, seed, adv_values):
    """『🔍 分析源音频』（仅 ACE-Step）：task_route=analyze 把源音频编码成语义 code，
    再用 5Hz LM 反推 caption/歌词/BPM/调性/拍号，回填到高级参数
    （写进 state 并通过 prefill state 触发控件重渲染，让填进去的值可见可改）。
    返回 (adv_state, prefill, message)。"""
    adv_values = dict(adv_values or {})
    try:
        if not source_audio:
            raise gr.Error("请先上传源音频")
        entry = catalog_by_id(model)
        if not entry or entry.get("family") != "ace_step":
            raise gr.Error("只有 ACE-Step 支持源音频分析，请先在模型列表选 ACE-Step")
        ensure_model_loaded(model, GEN_TASKS)

        # 分析要可复现：seed=-1（随机）时固定为 1234，不跟生成共享随机性；
        # 用户显式填的固定 seed 仍然生效（可换 seed 重抽歌词转写）。
        analyze_seed = 1234 if int(seed if seed is not None else -1) == -1 else int(seed)
        req = {"text": "analyze", "task_route": "analyze",
               "audio": _ensure_wav(source_audio), "seed": analyze_seed}
        dur = _audio_duration_seconds(req["audio"])
        dur_note = f"{dur:.1f}s" if dur is not None else "未知"
        _ui_log(f"源音频分析开始：model={model}，音频 {dur_note}")
        t_start = time.time()
        data = _run_task(entry, model, req, timeout=1800, log_label="源音频分析")
        raw = data.get("text") or ""
        try:
            info = json.loads(raw)
        except Exception:
            raise gr.Error(f"server 返回的分析结果不是 JSON：{raw[:200]}")
        elapsed = time.time() - t_start

        for src, dst in _ANALYZE_FILL_MAP:
            v = info.get(src)
            if v is None or v == "" or v == 0:
                continue
            adv_values[dst] = v

        lines = [f"✅ 分析完成（音频 {dur_note}），用时 {elapsed:.1f}s，"
                 "已回填到『高级参数』（可改）。做 remix/cover 前请核对歌词。"]
        for key, label in (("caption", "描述"), ("bpm", "BPM"), ("keyscale", "调性"),
                           ("timesignature", "拍号"), ("language", "语言"),
                           ("genres", "流派"), ("duration", "时长(s)")):
            v = info.get(key)
            if v not in (None, "", 0):
                lines.append(f"- **{label}**：{v}")
        if info.get("lyrics"):
            lines.append(f"- **歌词**：\n```\n{info['lyrics']}\n```")
        _ui_log(f"源音频分析完成：用时 {elapsed:.1f}s")
        return adv_values, dict(adv_values), "\n".join(lines)
    except gr.Error as e:
        return adv_values, gr.skip(), _msg_from_error(e)
    except Exception as e:
        return adv_values, gr.skip(), f"❌ 分析失败：{e}"


def do_vc(model, source_audio, target_upload, builtin_voice, seed,
          adv_values, adv_options, progress=gr.Progress()):
    """声音/歌声转换（vc/svc/s2s），走通用 /v1/tasks/run 路由：`audio` 是源音频，
    `voice_ref` 是目标音色。seed_vc/miocodec 直接用这两个字段；vevo2 也接受它们
    （audio_input/voice speaker 是 source_audio/target_voice 选项的回退），
    风格转换类 route 的额外字段（style_ref 等）由“其它参数(JSON)”兜底。
    profile 带 vc_chunk_seconds 的族（vevo2 FM 图按整段建，8G 卡长音频必炸）
    超限时按低能量点分段逐段转换，同一 voice_ref 保证各段音色一致，最后拼接。"""
    try:
        if not source_audio:
            raise gr.Error("请上传要转换的源音频")
        ensure_model_loaded(model, VC_TASKS)
        entry = catalog_by_id(model)
        prof = profile_for(entry) if entry else DEFAULT_PROFILE
        options = _merged_options(prof, adv_values, adv_options)

        source_audio = _ensure_wav(source_audio)
        # 同一 seed 用于所有分段，保证各段结果一致可复现。
        seed, seed_note = _resolve_seed(seed)
        req = {"seed": seed}
        voice_path = target_upload or (
            os.path.join(PROMPTS_DIR, builtin_voice)
            if builtin_voice and builtin_voice != "(none)" else None)
        if voice_path:
            ref_wav = _ensure_wav(voice_path)
            ref_cap = prof.get("vc_ref_max_seconds")
            if ref_cap:                     # 参考音色截短，别让 prompt 段吃满显存
                ref_wav = _trim_wav_seconds(ref_wav, ref_cap)
            req["voice_ref"] = ref_wav
        if options:
            req["options"] = options

        # vevo2 的 FM 图一次建图，序列长度 = 参考音色(prompt) + 每段源(target)。按
        # 显存预算反推每段源时长（预算 − 参考时长），令 cond 稳定落在 8G 内；没有
        # 显存预算/参考时的族仍按 vc_chunk_seconds 上限或整段发送。
        chunk_cap = prof.get("vc_chunk_seconds")
        ref_sec = _audio_duration_seconds(req.get("voice_ref")) or 0.0
        if chunk_cap and prof.get("vc_fm_budget_seconds"):
            min_chunk = prof.get("vc_min_chunk_seconds", 6)
            budget = prof["vc_fm_budget_seconds"]
            chunk_cap = int(max(min_chunk, min(chunk_cap, round(budget - ref_sec))))
        pieces = (_split_wav_chunks(source_audio, chunk_cap)
                  if chunk_cap else [(source_audio, 1.0)])
        dur = _audio_duration_seconds(source_audio)
        dur_note = f"{dur:.1f}s" if dur is not None else "未知"
        seg_note = (f"，参考 {ref_sec:.0f}s，自适应分 {len(pieces)} 段（每段 ≤{chunk_cap}s）"
                    if len(pieces) > 1 else "")
        _ui_log(f"声音转换开始：model={model}，源音频 {dur_note}{seg_note}")
        t_start = time.time()
        blobs, ratios = [], []
        for i, (piece, ratio) in enumerate(pieces):
            if len(pieces) > 1:
                progress((i, len(pieces)), desc=f"转换 {i + 1}/{len(pieces)} 段…")
            req["audio"] = piece
            t_seg = time.time()
            data = _run_task(entry, model, req, timeout=1800, log_label="声音转换")
            b64 = data.get("audio")
            if not b64 and data.get("named_audio_outputs"):
                b64 = data["named_audio_outputs"][0].get("audio")
            if not b64:
                raise gr.Error("server 返回里没有音频数据")
            blobs.append(base64.b64decode(b64))
            ratios.append(ratio)
            if len(pieces) > 1:
                _ui_log(f"声音转换段 {i + 1}/{len(pieces)} 完成（{time.time() - t_seg:.1f}s）")
        out = os.path.join(OUTPUT_DIR, f"audiocpp_vc_{int(time.time()*1000)}.wav")
        if len(blobs) == 1 and ratios[0] >= 1.0:
            with open(out, "wb") as f:
                f.write(blobs[0])
        else:
            _concat_wavs(blobs, out, keep_ratios=ratios)
        elapsed = time.time() - t_start
        _ui_log(f"声音转换完成：{out}，用时 {elapsed:.1f}s")
        parts_note = f"（{len(blobs)} 段拼接）" if len(blobs) > 1 else ""
        return out, f"✅ 转换完成{parts_note}，用时 {elapsed:.1f}s。{seed_note}"
    except gr.Error as e:
        return None, _msg_from_error(e)
    except Exception as e:
        return None, f"❌ 转换失败：{e}"


# 分轨 id -> 中文标签；未收录的 id 原样显示。
STEM_LABELS = {"vocals": "人声", "drums": "鼓", "bass": "贝斯", "other": "其它",
               "instrumental": "伴奏", "accompaniment": "伴奏", "audio": "输出"}
MAX_SEP_STEMS = 4
# 分离族（htdemucs / mel-band-roformer）的 prepare() 硬校验 44.1kHz（模型配置
# sample_rate），不自己重采样；其它采样率的输入在 webui 侧先转到 44.1k。
SEP_SR = 44100


def do_sep(model, audio_path):
    """音源分离：响应里的 named_audio_outputs 每轨落盘成一个 wav，
    前 MAX_SEP_STEMS 轨直接放进播放器，全部轨放进文件下载列表。"""
    empty = [gr.update(value=None, visible=False) for _ in range(MAX_SEP_STEMS)]
    try:
        if not audio_path:
            raise gr.Error("请上传要分离的音频")
        audio_path = _ensure_wav(audio_path, target_sr=SEP_SR)
        ensure_model_loaded(model, SEP_TASKS)
        entry = catalog_by_id(model)
        dur = _audio_duration_seconds(audio_path)
        dur_note = f"{dur:.1f}s" if dur is not None else "未知"
        _ui_log(f"音源分离开始：model={model}，音频时长 {dur_note}")
        t_start = time.time()
        data = _run_task(entry, model, {"audio": audio_path},
                         timeout=1800, log_label="音源分离")
        stems = data.get("named_audio_outputs") or []
        if not stems and data.get("audio"):
            stems = [{"id": "audio", "audio": data["audio"]}]
        if not stems:
            raise gr.Error("server 返回里没有音轨数据")
        ts = int(time.time() * 1000)
        paths = []
        for stem in stems:
            sid = stem.get("id") or f"stem{len(paths)}"
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", sid)
            p = os.path.join(OUTPUT_DIR, f"audiocpp_sep_{ts}_{safe}.wav")
            with open(p, "wb") as f:
                f.write(base64.b64decode(stem["audio"]))
            paths.append((sid, p))
        updates = []
        for i in range(MAX_SEP_STEMS):
            if i < len(paths):
                sid, p = paths[i]
                zh = STEM_LABELS.get(sid)
                updates.append(gr.update(
                    value=p, visible=True, label=f"{zh}（{sid}）" if zh else sid))
            else:
                updates.append(gr.update(value=None, visible=False))
        elapsed = time.time() - t_start
        _ui_log(f"音源分离完成：{len(paths)} 轨，用时 {elapsed:.1f}s")
        note = ("" if len(paths) <= MAX_SEP_STEMS
                else f"，其余 {len(paths) - MAX_SEP_STEMS} 轨见下载列表")
        return (*updates, [p for _, p in paths],
                f"✅ 分离完成（{len(paths)} 轨）{note}，用时 {elapsed:.1f}s。")
    except gr.Error as e:
        return (*empty, None, _msg_from_error(e))
    except Exception as e:
        return (*empty, None, f"❌ 分离失败：{e}")


# VAD/diar/align 输入统一转成 16 kHz 单声道后再发（见 _to_16k_mono_wav），
# 所以响应里的 start_sample/end_sample 一律按 16000 换算成秒。
SR_ANALYZE = 16000


def _fmt_ts(samples):
    return f"{samples / SR_ANALYZE:.2f}s"


def do_analyze(model, audio_path, transcript, language):
    """音频分析（vad/diar/align）：格式化 segments / speaker_turns / words 为
    可读文本，原始 JSON 落盘供下载。"""
    try:
        if not audio_path:
            raise gr.Error("请上传或录制音频")
        entry = catalog_by_id(model)
        task = entry.get("task") if entry else ""
        req = {"audio": _to_16k_mono_wav(_ensure_wav(audio_path))}
        if task == "align":
            if not (transcript or "").strip():
                raise gr.Error("强制对齐需要在『对齐文本』里填音频中说的原文")
            req["text"] = transcript.strip()
            if (language or "").strip():
                req["language"] = language.strip()
        dur = _audio_duration_seconds(req["audio"])
        # diar（Sortformer）是固定图容量，>20s 的音频按时长动态重载。
        ensure_model_loaded(model, ANALYZE_TASKS,
                            session_options=(_diar_session_options(dur)
                                             if task == "diar" else None))
        dur_note = f"{dur:.1f}s" if dur is not None else "未知"
        _ui_log(f"音频分析开始：model={model}（task={task}），音频 {dur_note}")
        t_start = time.time()
        data = _run_task(entry, model, req, timeout=900, log_label="音频分析")

        lines = []
        if data.get("segments"):
            segs = data["segments"]
            lines.append(f"共 {len(segs)} 个语音段：")
            speech = 0
            for i, s in enumerate(segs, 1):
                lines.append(f"{i:3d}. {_fmt_ts(s['start_sample'])} → {_fmt_ts(s['end_sample'])}"
                             f"　置信度 {s.get('confidence', 0):.2f}")
                speech += s["end_sample"] - s["start_sample"]
            lines.append(f"语音总时长约 {speech / SR_ANALYZE:.1f}s")
        if data.get("speaker_turns"):
            turns = data["speaker_turns"]
            spk = sorted({t.get("speaker_id", "?") for t in turns})
            lines.append(f"共 {len(turns)} 个发言段、{len(spk)} 个说话人（{', '.join(spk)}）：")
            for i, t in enumerate(turns, 1):
                lines.append(f"{i:3d}. {_fmt_ts(t['start_sample'])} → {_fmt_ts(t['end_sample'])}"
                             f"　{t.get('speaker_id', '?')}　置信度 {t.get('confidence', 0):.2f}")
        if data.get("words"):
            lines.append(f"共 {len(data['words'])} 个词的时间戳：")
            for w in data["words"]:
                lines.append(f"{_fmt_ts(w['start_sample'])} → {_fmt_ts(w['end_sample'])}"
                             f"　{w.get('word', '')}")
        if data.get("text"):
            lines.append(f"文本输出：{data['text']}")
        if not lines:
            lines.append("（模型没有返回可显示的分析结果——音频里可能没有检测到语音）")

        json_path = os.path.join(OUTPUT_DIR, f"audiocpp_analyze_{int(time.time()*1000)}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        elapsed = time.time() - t_start
        _ui_log(f"音频分析完成：用时 {elapsed:.1f}s")
        return ("\n".join(lines), json_path,
                f"✅ 分析完成（音频 {dur_note}），用时 {elapsed:.1f}s。")
    except gr.Error as e:
        return "", None, _msg_from_error(e)
    except Exception as e:
        return "", None, f"❌ 分析失败：{e}"


def _align_fields_visibility(model_id):
    """音频分析页：只有 align 任务的模型才显示『对齐文本/语言』输入。"""
    entry = catalog_by_id(model_id) if model_id else None
    show = bool(entry) and entry.get("task") == "align"
    return gr.update(visible=show), gr.update(visible=show)


def do_vdes(model, text, instruct, seed, max_tokens, adv_values, adv_options):
    """声音设计（vdes）：文字 + 音色描述走 /v1/audio/speech（instructions 字段
    映射到模型的 instruct 选项），响应是 WAV 音频。"""
    try:
        if not (text or "").strip():
            raise gr.Error("请输入要合成的文字")
        if not (instruct or "").strip():
            raise gr.Error("请填写『音色描述』——声音设计模型靠它决定生成什么样的声音")
        ensure_model_loaded(model, VDES_TASKS)
        entry = catalog_by_id(model)
        prof = profile_for(entry) if entry else DEFAULT_PROFILE
        options = _merged_options(prof, adv_values, adv_options)

        seed, seed_note = _resolve_seed(seed)
        payload = {"model": model, "input": text, "instructions": instruct,
                   "seed": seed, "max_tokens": int(max_tokens)}
        if options:
            payload["options"] = options
        _ui_log(f"声音设计开始：model={model}，{len(text)} 字")
        t_start = time.time()
        try:
            r = requests.post(f"{SERVER}/v1/audio/speech", json=payload, timeout=900)
        except requests.RequestException as e:
            _ui_log("声音设计失败：无法连接 server")
            raise gr.Error(f"无法连接 server @ {SERVER}：{e}\n💡 server 可能已退出，重新点『📥 加载模型』。")
        if r.status_code != 200:
            _ui_log(f"声音设计失败：server {r.status_code}")
            raise server_error(entry, r.status_code, r.text)
        out = os.path.join(OUTPUT_DIR, f"audiocpp_vdes_{int(time.time()*1000)}.wav")
        with open(out, "wb") as f:
            f.write(r.content)
        elapsed = time.time() - t_start
        _ui_log(f"声音设计完成：{out}，用时 {elapsed:.1f}s")
        return out, f"✅ 生成完成，用时 {elapsed:.1f}s。{seed_note}"
    except gr.Error as e:
        return None, _msg_from_error(e)
    except Exception as e:
        return None, f"❌ 生成失败：{e}"


def _make_load_handler(tasks):
    """『📥 加载模型』按钮的处理器工厂：按标签页各自的 task 集合校验并加载。"""
    def _load(model):
        try:
            s = ensure_model_loaded(model, tasks)
        except gr.Error as e:
            s = _msg_from_error(e)
        return s, server_status()
    return _load


# 标签页顺序（与 refresh() 的输出、_refresh_outputs 列表一一对应）：
# TTS、ASR、音乐生成、声音转换、音源分离、音频分析、声音设计。
TAB_SPECS = [TTS_TASKS, ASR_TASKS, GEN_TASKS, VC_TASKS,
             SEP_TASKS, ANALYZE_TASKS, VDES_TASKS]


def refresh():
    """重读 catalog / 参数配置，刷新每个标签页的模型下拉和提示。
    返回顺序：各页下拉更新（按 TAB_SPECS 顺序）、状态行、各页提示。"""
    global CATALOG, MODEL_PARAMS
    CATALOG = _load_catalog()
    MODEL_PARAMS = _load_model_params()
    dropdowns, hints = [], []
    for tasks in TAB_SPECS:
        choices = choices_for_tasks(tasks)
        value = choices[0][1] if choices else None
        dropdowns.append(gr.update(choices=choices, value=value))
        hints.append(model_hint_for(value))
    return (*dropdowns, server_status(), *hints)


atexit.register(_stop_server)


CUSTOM_CSS = """

.audio-default { border: none !important; box-shadow: none !important; }

.mm-btn-row { gap: 10px !important; }
.mm-btn-row button { border-radius: var(--button-large-radius, var(--radius-lg)) !important; }

/* 长音频的波形出现横向滚动条时，WaveSurfer 的滚动层（58px 内容 + 滚动条）会
   溢出 Gradio 固定 58px 的 .waveform-container / #waveform，盖住下方的
   0:00/总时长标签。放开这两层的高度让标签随内容下移；短音频（无滚动条）时
   min-height 保证布局与原来一致。 */
.waveform-container, #waveform { height: auto !important; min-height: 58px; }

.hint-small { opacity: 0.7; font-size: 0.85em; margin-top: 2px; }

/* 内置参考音色行：Row 本身透明，会在按钮一列露出 gr.Group 的灰色面板底，
   和 secondary 按钮的灰底连成一片。把整行铺成和下拉 block 相同的白色卡片，
   按钮（灰底）落在卡片内自然形成对比；flex-end + margin 让按钮和下拉输入框
   本体底部对齐（下拉 block 自带 --block-padding 内边距）。 */
.voice-refresh-row {
  align-items: flex-end !important;
  background: var(--block-background-fill, #fff);
  border-radius: var(--block-radius, 8px);
}
.voice-refresh-row button {
  height: var(--size-10, 40px);
  /* 字面量，不能用 var(--block-padding)：该主题变量是双值 "10px 12px"，
     代入 margin 简写会让整条声明非法被丢弃。11px = block 下内边距 10px + 边框，
     让按钮和下拉输入框本体上下沿精确对齐。 */
  margin: 0 12px 11px 0;
  border-radius: var(--radius-lg) !important;
}
"""
_YUNLAI_BRAND_CSS = r"""
/* ===== YUNLAI_BRAND: 云来AI工作室 · expogent design language ===== */
:root, .dark {
  --yl-bg:#0a0e17; --yl-panel:#111827; --yl-panel2:#0d1420; --yl-line:#1f2b3d;
  --yl-tx:#e5edf7; --yl-dim:#8296b0; --yl-acc:#22d3ee; --yl-acc2:#a78bfa;
  --yl-ink:#eef4ff;   /* 麦图标描边：暗底近白（VP同款 2026-09-27） */
  --yl-ok:#34d399; --yl-warn:#fbbf24; --yl-bad:#f87171;
  --body-background-fill:var(--yl-bg) !important;
  --background-fill-primary:var(--yl-bg) !important;
  --background-fill-secondary:var(--yl-panel2) !important;
  --block-background-fill:var(--yl-panel) !important;
  --block-border-color:var(--yl-line) !important;
  --block-label-text-color:var(--yl-dim) !important;
  --block-title-text-color:var(--yl-tx) !important;
  --body-text-color:var(--yl-tx) !important;
  --body-text-color-subdued:var(--yl-dim) !important;
  --border-color-primary:var(--yl-line) !important;
  --border-color-accent:#1d4ed8 !important;
  --color-accent:var(--yl-acc) !important;
  --link-text-color:var(--yl-acc) !important;
  --link-text-color-hover:#67e8f9 !important;
  --checkbox-background-color-selected:var(--yl-acc) !important;
  --checkbox-border-color-selected:var(--yl-acc) !important;
  --slider-color:var(--yl-acc) !important;
  color-scheme: dark !important;
}
html, body, .gradio-container { background:var(--yl-bg) !important; }
.gradio-container { max-width:1600px !important; width:100% !important; margin-left:auto !important; margin-right:auto !important; }   /* 0927 Owen：6.19容器按内容收缩(1223px)不填满，补width:100%撑到1600对齐dub站；封顶1600必须配margin:auto居中(6.19无6.20原生居中)，否则宽屏贴左致右侧大空白 */

/* 品牌顶栏 */
/* ===== 三工作台导航 + logo外链（2026-09-27 Owen） ===== */
.yl-nav { display:flex; gap:6px; flex:none; align-self:center; margin-left:auto; }   /* 0927：标签删除后 nav 顶替其靠右角色，品牌区独占左，消除中间空白 */
.yl-nav a, .yl-nav .yl-navcur { font-size:.72rem; padding:3px 10px; border-radius:999px;
  border:1px solid rgba(34,211,238,.45); color:#22d3ee; text-decoration:none; white-space:nowrap;
  transition:.15s; }
.yl-nav a:hover { border-color:#22d3ee; box-shadow:0 0 10px rgba(34,211,238,.35); }
.yl-nav .yl-navcur { background:linear-gradient(90deg,#155e8a,#243b8f,#6d28d9); color:#fff;
  border-color:transparent; cursor:default; }
body.yl-light .yl-nav a { border-color:rgba(37,99,235,.55) !important; color:#1e3a8a !important; }
body.yl-light .yl-nav a:hover { border-color:#4338ca !important; color:#172554 !important; }
body.yl-light .yl-nav a:hover { border-color:#6d28d9; box-shadow:0 0 8px rgba(109,40,217,.3); }
/* 0927 Owen 顶栏对称：display:flex+左对齐——block 时 inline-flex 的 brandmark 会被 prose
   的居中上下文推出 (linkW-markW)/2≈31px 幻影左缩进（手机端 logo 左空档≫按钮右空档的根因）。
   padding 归零后 logo 左 inset == 按钮右 inset（都=brandbar 内边距）。 */
.yl-brandlink { text-decoration:none; color:inherit;
  display:flex !important; align-items:center; justify-content:flex-start; padding:2px 0 !important; }
.yl-brandlink:hover .yl-brandtext { filter:brightness(1.25); }

.yl-brandbar { display:flex; align-items:center; gap:12px; padding:14px 18px; margin-bottom:18px;
  background:linear-gradient(135deg, rgba(34,211,238,.07), rgba(167,139,250,.05) 55%, rgba(244,114,182,.04));
  border:1px solid var(--yl-line); border-radius:14px; }
.yl-brandmark { display:inline-flex; align-items:center; gap:12px;
  animation:yl-bloom 1.85s cubic-bezier(.25,.6,.3,1) .3s both; }
@keyframes yl-bloom {
  0%,22% { filter:grayscale(1) brightness(1.62) contrast(.8); opacity:.55; }
  62% { filter:grayscale(.28) brightness(1.22) contrast(.92) drop-shadow(0 0 5px rgba(34,211,238,.22)); opacity:.9; }
  100% { filter:grayscale(0) brightness(1.05) drop-shadow(0 0 13px rgba(34,211,238,.5)); opacity:1; }
}
.yl-brandicon { height:38px; width:auto; border-radius:9px; }
.yl-brandtext { font-family:'MiSansHeavy','Noto Sans CJK SC','PingFang SC','Microsoft YaHei',sans-serif;
  font-size:1.45rem; font-weight:900; letter-spacing:.045em; line-height:1.1;
  background:linear-gradient(96deg,#2ee6ff 0%,#38bdf8 20%,#60a5fa 40%,#a78bfa 62%,#e879f9 82%,#f472b6 100%);
  -webkit-background-clip:text; background-clip:text; color:transparent; white-space:nowrap; }
.yl-brandtail { font-family:'MiSansHeavy','Noto Sans CJK SC','PingFang SC','Microsoft YaHei',sans-serif;
  font-size:1.45rem; font-weight:900; letter-spacing:.045em; line-height:1.1;
  background:linear-gradient(96deg,#fff3c4 0%,#ffd76a 30%,#f5b32a 65%,#d98a12 100%);
  -webkit-background-clip:text; background-clip:text; color:transparent; white-space:nowrap; }
.yl-brandwave { width:1.6rem; height:1.6rem; flex:none; align-self:center;
  filter:drop-shadow(0 0 7px rgba(245,179,42,.5)); }
.yl-brandtags { margin-left:auto; display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.yl-tag { font-size:.74rem; color:var(--yl-acc); border:1px solid rgba(34,211,238,.35);
  background:rgba(34,211,238,.07); padding:3px 10px; border-radius:999px; white-space:nowrap; }
.yl-tag.v { color:var(--yl-acc2); border-color:rgba(167,139,250,.35); background:rgba(167,139,250,.07); }
.yl-tag.p { color:#f472b6; border-color:rgba(244,114,182,.35); background:rgba(244,114,182,.07); }

/* 主按钮 = expogent 青蓝渐变 + 霓虹 hover */
button.primary, .gradio-container button.primary {
  background:linear-gradient(135deg,#22d3ee,#38bdf8) !important; color:#04121a !important;
  border:none !important; font-weight:700 !important; }
button.primary:hover { box-shadow:0 0 18px rgba(34,211,238,.45) !important; filter:brightness(1.06) !important; }
button.secondary { background:var(--yl-panel2) !important; color:var(--yl-tx) !important;
  border:1px solid var(--yl-line) !important; }
button.secondary:hover { border-color:var(--yl-acc) !important; color:var(--yl-acc) !important;
  box-shadow:0 0 14px rgba(34,211,238,.18) !important; }

/* 卡片/手风琴/输入件 */
.block, .gradio-accordion, .gradio-group { border-color:var(--yl-line) !important; }
.gradio-accordion .label-wrap { color:var(--yl-tx) !important; }
input, textarea, select, .wrap-inner, .token-count { background:var(--yl-panel2) !important;
  color:var(--yl-tx) !important; border-color:var(--yl-line) !important; }
input:focus, textarea:focus { border-color:var(--yl-acc) !important;
  box-shadow:0 0 0 2px rgba(34,211,238,.18) !important; }
::placeholder { color:var(--yl-dim) !important; opacity:.8; }
.tabs > .tab-nav { background:transparent !important; border-bottom:1px solid var(--yl-line) !important; }
.tabs > .tab-nav > button { background:var(--yl-panel2) !important; color:var(--yl-dim) !important;
  border:1px solid var(--yl-line) !important; border-bottom:none !important; border-radius:9px 9px 0 0 !important; margin-right:4px; }
.tabs > .tab-nav > button.selected { color:var(--yl-acc) !important; border-color:var(--yl-acc) !important;
  background:var(--yl-panel) !important; box-shadow:inset 0 2px 0 var(--yl-acc); }
/* 表格/JSON/日志区 */
.table-wrap, .gradio-json, .gradio-code, pre, code { background:var(--yl-panel2) !important; color:var(--yl-tx) !important; }
/* 滚动条 */
::-webkit-scrollbar { width:10px; height:10px; }
::-webkit-scrollbar-track { background:var(--yl-bg); }
::-webkit-scrollbar-thumb { background:var(--yl-line); border-radius:6px; }
::-webkit-scrollbar-thumb:hover { background:#2d3f59; }
/* Gradio 自带主题切换器隐藏（已强制深色） */
#theme-toggle, .theme-toggle, button[aria-label*="Switch between dark and light mode"] { display:none !important; }
/* footer */
footer { display:none !important; }

/* ===== YUNLAI_LIGHT: 白天色（2026-09-27 从配音工作台移植）===== */
body.yl-light {
  --yl-bg:#eef2f8; --yl-panel:#ffffff; --yl-panel2:#f6f8fc; --yl-line:#d7dfeb;
  --yl-tx:#101c2e; --yl-dim:#5b6b81; --yl-acc:#0e7490; --yl-acc2:#7c3aed;
  --yl-ink:#101c2e;
  --yl-ok:#059669; --yl-warn:#b45309; --yl-bad:#dc2626;
  --body-background-fill:var(--yl-bg) !important;
  --background-fill-primary:var(--yl-bg) !important;
  --background-fill-secondary:var(--yl-panel2) !important;
  --block-background-fill:var(--yl-panel) !important;
  --block-border-color:var(--yl-line) !important;
  --block-label-text-color:var(--yl-dim) !important;
  --block-label-background-fill:var(--yl-panel2) !important;
  --block-title-text-color:var(--yl-tx) !important;
  --body-text-color:var(--yl-tx) !important;
  --body-text-color-subdued:var(--yl-dim) !important;
  --border-color-primary:var(--yl-line) !important;
  --border-color-accent:#0891b2 !important;
  --color-accent:var(--yl-acc) !important;
  --link-text-color:var(--yl-acc) !important;
  --link-text-color-hover:#0e7490 !important;
  --checkbox-background-color-selected:var(--yl-acc) !important;
  --checkbox-border-color-selected:var(--yl-acc) !important;
  --slider-color:var(--yl-acc) !important;
  color-scheme: light !important;
}
body.yl-light, body.yl-light .gradio-container { background:var(--yl-bg) !important; }
body.yl-light .yl-brandbar {
  background:linear-gradient(135deg, rgba(14,116,144,.06), rgba(124,58,237,.05) 55%, rgba(219,39,119,.04)) !important;
  border-color:var(--yl-line) !important; }
body.yl-light .yl-brandtext {
  background:linear-gradient(96deg,#0891b2 0%,#0e7490 20%,#2563eb 40%,#7c3aed 62%,#c026d3 82%,#db2777 100%) !important;
  -webkit-background-clip:text !important; background-clip:text !important; }
body.yl-light .yl-brandtail {
  background:linear-gradient(96deg,#d97706 0%,#b45309 50%,#92400e 100%) !important;
  -webkit-background-clip:text !important; background-clip:text !important; }
body.yl-light .yl-brandwave { filter:saturate(1.5) brightness(.85) drop-shadow(0 0 5px rgba(180,83,9,.3)) !important; }
body.yl-light .yl-tag { color:var(--yl-acc) !important; border-color:rgba(14,116,144,.4) !important; background:rgba(14,116,144,.08) !important; }
body.yl-light .yl-tag.v { color:var(--yl-acc2) !important; border-color:rgba(124,58,237,.4) !important; background:rgba(124,58,237,.08) !important; }
body.yl-light .yl-tag.p { color:#db2777 !important; border-color:rgba(219,39,119,.4) !important; background:rgba(219,39,119,.08) !important; }
body.yl-light button.secondary { background:var(--yl-panel) !important; color:var(--yl-tx) !important;
  border:1px solid var(--yl-line) !important; }
body.yl-light .tabs > .tab-nav > button { background:var(--yl-panel2) !important; color:var(--yl-dim) !important;
  border:1px solid var(--yl-line) !important; border-bottom:none !important; }
body.yl-light .tabs > .tab-nav > button.selected { color:var(--yl-acc) !important;
  border-color:var(--yl-acc) !important; background:var(--yl-panel) !important; }
body.yl-light ::-webkit-scrollbar-track { background:var(--yl-bg); }
body.yl-light ::-webkit-scrollbar-thumb { background:#c3cedd; }
body.yl-light ::-webkit-scrollbar-thumb:hover { background:#a9b7ca; }
body.yl-light h1, body.yl-light h2, body.yl-light h3, body.yl-light h4,
body.yl-light h5, body.yl-light h6 { color:var(--yl-tx) !important; }
body.yl-light .block-label, body.yl-light span[data-testid="block-label"],
body.yl-light .label-wrap span { background:var(--yl-panel2) !important;
  color:var(--yl-tx) !important; border:1px solid var(--yl-line) !important; }
body.yl-light .prose, body.yl-light .html-container { color:var(--yl-tx) !important; }
body.yl-light label.float, body.yl-light .block label[class*="svelte"].float,
body.yl-light label[data-testid="block-label"] {
  background:var(--yl-panel2) !important; color:var(--yl-tx) !important;
  border:1px solid var(--yl-line) !important; }
/* 亮色麦图标 → 深蓝紫渐变（云来LOGO色） */
body.yl-light .yl-brandwave rect { fill:url(#ylDeepGrad) !important; }
body.yl-light .yl-brandwave .yl-grille { stroke:#dceaff !important; }

/* ===== 右上角控制条（语言/主题切换）—— 0927 Owen"再精致点"：正圆 34px、径向渐变宝石底、
   hover 光环+浮起、active 回弹；主题钮换矢量月亮/太阳（emoji 跨端渲染不一致，大厂规范）===== */
#yl-ctrlbar { display:flex; gap:8px; flex:none; align-items:center; }
#yl-ctrlbar.fallback { position:fixed; top:10px; right:14px; z-index:99999; }
#yl-ctrlbar button {
  width:34px; height:34px; min-width:34px; padding:0 !important;
  border-radius:50% !important; cursor:pointer; flex:none; white-space:nowrap;
  font-size:.76rem; font-weight:800; line-height:1; letter-spacing:.02em;
  display:inline-flex; align-items:center; justify-content:center;
  margin:0 !important; align-self:center;
  color:#e8f6ff;
  /* 宝石质感三层：①左上集中镜面高光斑 ②大范围漫射青晕 ③底部深海蓝 */
  background:
    radial-gradient(30% 26% at 32% 20%, rgba(255,255,255,.55) 0%, rgba(186,250,255,.22) 60%, rgba(186,250,255,0) 100%),
    radial-gradient(75% 75% at 40% 26%, rgba(103,232,249,.2) 0%, rgba(103,232,249,0) 100%),
    radial-gradient(135% 135% at 50% 120%, rgba(37,99,235,.45) 0%, rgba(13,20,40,.95) 55%, rgba(8,12,26,.97) 100%);
  border:1px solid rgba(125,238,252,.7);
  box-shadow:0 2px 10px rgba(2,8,20,.6), 0 0 14px rgba(34,211,238,.16),
    inset 0 2px 2px rgba(255,255,255,.42), inset 0 0 0 1px rgba(165,243,252,.18), inset 0 -2px 5px rgba(2,6,18,.55);
  backdrop-filter:blur(10px);
  transition:box-shadow .2s, border-color .2s, transform .15s, filter .2s; }
#yl-ctrlbar button:hover {
  border-color:#cffafe; filter:brightness(1.1); transform:translateY(-1.5px);
  box-shadow:0 0 0 3px rgba(34,211,238,.2), 0 6px 18px rgba(34,211,238,.38), inset 0 2px 2px rgba(255,255,255,.45); }
#yl-ctrlbar button:active { transform:translateY(0) scale(.92); transition-duration:.06s; }
#yl-ctrlbar button svg { width:17px; height:17px; display:block; }
body.yl-light #yl-ctrlbar button {
  background:linear-gradient(135deg,#155e8a 0%,#243b8f 55%,#6d28d9 100%) !important;
  border:1px solid rgba(255,255,255,.7) !important; color:#ffffff !important;
  box-shadow:0 2px 8px rgba(21,94,138,.42), inset 0 2px 2px rgba(255,255,255,.3),
    inset 0 0 0 1px rgba(255,255,255,.14), inset 0 -2px 5px rgba(8,20,60,.3) !important; }
body.yl-light #yl-ctrlbar button:hover {
  border-color:#ffffff !important; filter:brightness(1.06);
  box-shadow:0 0 0 3px rgba(109,40,217,.18), 0 6px 16px rgba(36,59,143,.45), inset 0 2px 2px rgba(255,255,255,.34) !important; }

/* ===== 选择态强化：radio 左侧圆孔+勾号（VP同款）===== */
.gradio-container input[type=radio] {
  position:absolute !important; opacity:0 !important; margin:0 !important;
  width:13px; height:13px; left:9px; top:50%; transform:translateY(-50%); pointer-events:none; }
.gradio-container input[type=radio] + span { margin-left:0 !important; }
.gradio-container label:has(> input[type=radio]) { position:relative; padding-left:30px !important; }
.gradio-container label:not(.selected):has(> input[type=radio])::before {
  content:''; position:absolute; left:8px; top:50%; transform:translateY(-50%);
  width:15px; height:15px; border-radius:50%; box-sizing:border-box; pointer-events:none;
  border:1.8px solid var(--yl-dim,#8296b0); background:transparent; opacity:.65; }
.gradio-container label.selected:has(> input[type=radio]) {
  border-color:rgba(34,211,238,.75) !important;
  box-shadow:0 0 0 2px rgba(34,211,238,.75), 0 0 14px rgba(34,211,238,.30) !important; }
.gradio-container label.selected:has(> input[type=radio])::before {
  content:''; position:absolute; left:8px; top:50%; transform:translateY(-50%) scale(1.06);
  width:15px; height:15px; border-radius:50%; box-sizing:border-box; pointer-events:none;
  border:2px solid #22d3ee; background:linear-gradient(135deg,#22d3ee,#a78bfa);
  box-shadow:0 0 9px rgba(34,211,238,.8); transition:transform .14s; }
.gradio-container label.selected:has(> input[type=radio])::after {
  content:'✓'; position:absolute; left:8px; top:50%; transform:translateY(-52%);
  width:15px; text-align:center; font-size:10px; font-weight:900; line-height:1;
  color:#071018; pointer-events:none; }
body.yl-light .gradio-container label.selected:has(> input[type=radio]) {
  border-color:rgba(14,116,144,.8) !important;
  box-shadow:0 0 0 2px rgba(14,116,144,.8), 0 0 12px rgba(14,116,144,.25) !important; }
body.yl-light .gradio-container label.selected:has(> input[type=radio])::before {
  border-color:#0e7490; background:linear-gradient(135deg,#0e7490,#7c3aed);
  box-shadow:0 0 8px rgba(14,116,144,.4); }
body.yl-light .gradio-container label.selected:has(> input[type=radio])::after { color:#ffffff; }
body.yl-light .gradio-container label:not(.selected):has(> input[type=radio])::before {
  border-color:#64748b; }
.gradio-container input[type=checkbox] {
  position:absolute !important; opacity:0 !important; margin:0 !important;
  width:13px; height:13px; left:9px; top:50%; transform:translateY(-50%); pointer-events:none; }
.gradio-container label:has(> input[type=checkbox]) { position:relative; padding-left:30px !important; }
.gradio-container label:not(.selected):has(> input[type=checkbox])::before {
  content:''; position:absolute; left:8px; top:50%; transform:translateY(-50%);
  width:15px; height:15px; border-radius:4px; box-sizing:border-box; pointer-events:none;
  border:1.8px solid var(--yl-dim,#8296b0); opacity:.65; }
.gradio-container label.selected:has(> input[type=checkbox])::before {
  content:''; position:absolute; left:8px; top:50%; transform:translateY(-50%);
  width:15px; height:15px; border-radius:4px; box-sizing:border-box; pointer-events:none;
  border:2px solid #22d3ee; background:linear-gradient(135deg,#22d3ee,#a78bfa);
  box-shadow:0 0 9px rgba(34,211,238,.8); }
.gradio-container label.selected:has(> input[type=checkbox])::after {
  content:'✓'; position:absolute; left:8px; top:50%; transform:translateY(-52%);
  width:15px; text-align:center; font-size:10px; font-weight:900; line-height:1;
  color:#071018; pointer-events:none; }
body.yl-light .gradio-container label:has(> input[type=radio]),
body.yl-light .gradio-container label:has(> input[type=checkbox]) {
  background:var(--yl-panel) !important; color:var(--yl-tx) !important;
  border:1px solid var(--yl-line) !important; }
body.yl-light .gradio-container label.selected:has(> input[type=radio]),
body.yl-light .gradio-container label.selected:has(> input[type=checkbox]) {
  background:rgba(14,116,144,.10) !important; }
.gradio-container label:has(> input[type=radio]):hover,
.gradio-container label:has(> input[type=checkbox]):hover {
  border-color:rgba(34,211,238,.5) !important; }
body.yl-light .gradio-container label:has(> input[type=radio]):hover,
body.yl-light .gradio-container label:has(> input[type=checkbox]):hover {
  border-color:rgba(14,116,144,.55) !important; }
/* ===== /YUNLAI_LIGHT ===== */

/* ===== YUNLAI_MOBILE: 手机端布局（2026-09-27 Owen 两轮报障：①nav 溢出屏外 ②ctrlbar 掉行+按钮文字折行+界面偏窄）
   设计基准：8pt 间距节奏 / 触控目标 ≥40px / 标签单行不折 / 主按钮 3-across + 次按钮 2-across 层级
   实测根因：.main padding 16px 32px 在 390px 屏吃掉 64px；brandlink 256px + ctrlbar 76px > 顶栏可用 278px 致换行；
   按钮实宽 101px 而 "📥 加载模型"@16px+32px padding 需 ~142px 致折成 3 行(textH=64) ===== */
@media (max-width: 860px) {
  /* --- ① 拉宽可用界面：.main padding 无法在此改（Gradio 给所有选择器加 .contain 前缀，
     而 .main 是 .contain 的祖先→永不匹配）。已移到 head <style> 注入，见 _YUNLAI_BRAND_HEAD。 --- */
  .gradio-container { font-size:14px !important; }

  /* --- ② 顶栏第一行：品牌区(可压缩) + 主题/语言按钮(固定) 同行；nav 独占第二行 --- */
  .yl-brandbar { flex-wrap:wrap !important; gap:8px !important; row-gap:8px !important;
    padding:10px !important; margin-bottom:10px !important; }
  /* 品牌区允许收缩，min-width:0 是 flex 子项能被压缩的前提（否则内容撑破导致 ctrlbar 掉行） */
  .yl-brandlink { flex:1 1 0 !important; min-width:0 !important; padding:2px 0 !important; }
  .yl-brandmark { gap:6px !important; min-width:0 !important; }
  .yl-brandtext, .yl-brandtail { white-space:nowrap !important; }
  .yl-brandmark > span { gap:6px !important; }
  .yl-brandmark img.yl-brandicon { height:26px !important; width:auto !important; flex:none !important; }
  .yl-brandtext { font-size:1rem !important; }
  .yl-brandtail { font-size:.95rem !important; }
  .yl-brandwave { font-size:1.15rem !important; flex:none !important; }
  /* ctrlbar：order:2 排到品牌区右侧同一行；32px 方形按钮满足触控且不占宽 */
  #yl-ctrlbar { order:2 !important; flex:none !important; margin-left:auto !important; gap:8px !important; }
  #yl-ctrlbar button { width:32px !important; height:32px !important; min-width:32px !important;
    padding:0 !important; font-size:.78rem !important; }
  /* nav：整行三等分，视觉重量均衡（大厂 tab 式分段控件观感） */
  .yl-nav { order:3 !important; width:100% !important; margin-left:0 !important;
    display:flex !important; gap:6px !important; flex-wrap:nowrap !important; }
  .yl-nav a, .yl-nav .yl-navcur { flex:1 1 0 !important; min-width:0 !important;
    text-align:center !important; justify-content:center !important;
    font-size:.68rem !important; padding:6px 2px !important; white-space:nowrap !important;
    overflow:hidden !important; text-overflow:ellipsis !important; }
  .yl-brandtags { order:4 !important; width:100% !important; margin-left:0 !important;
    justify-content:flex-start !important; }
  .yl-tag { font-size:.68rem !important; padding:3px 8px !important; }
  /* Header 对齐：品牌HTML块被Gradio包成 block(pl10)→html-container(pl12) 双层缩进=32px，
     其它卡片只有10px → 上下边缘不齐（vision 7分项）。:has() 归零这两层，Header 与内容同基线。 */
  .block:has(> * .yl-brandbar), .block:has(.yl-brandbar),
  .html-container:has(.yl-brandbar) { padding:0 !important; }

  /* --- ③ 模型管理按钮组：单行标签 + 3-across 主操作 / 2-across 次操作 --- */
  .mm-btn-row { flex-wrap:wrap !important; gap:6px !important; }
  .mm-btn-row button { flex:1 1 calc(33.333% - 4px) !important; min-width:0 !important;
    height:40px !important; padding:0 6px !important; gap:3px !important;
    font-size:.78rem !important; line-height:1.1 !important;
    white-space:nowrap !important; overflow:hidden !important; justify-content:center !important; }
  .mm-btn-row button span { white-space:nowrap !important; overflow:hidden !important;
    text-overflow:ellipsis !important; font-size:.78rem !important; line-height:1.1 !important; }
  /* 第4、5个（下载进度/释放显存）改二等分，铺满第二行，避免右侧留空洞 */
  .mm-btn-row button:nth-child(4), .mm-btn-row button:nth-child(5) {
    flex:1 1 calc(50% - 3px) !important; }

  /* --- ④ 内容块与文字节奏：手机上收紧留白，保持呼吸但不浪费 --- */
  .block, .gr-block { padding:10px !important; }
  .gradio-container .label-text, .gradio-container span[data-testid="block-label"] span {
    font-size:.82rem !important; }
  .gradio-container button:not(.mm-btn-row button):not(#yl-ctrlbar button) {
    font-size:.84rem !important; min-height:38px !important; }
  .gradio-container textarea, .gradio-container input[type="text"],
  .gradio-container .wrap-inner, .gradio-container .secondary-wrap { font-size:.84rem !important; }
  /* 手风琴（API说明/下载设置）标题行收紧 */
  .gradio-container .label-wrap, .gradio-container span[data-testid="block-label"] {
    font-size:.84rem !important; gap:6px !important; }
}
/* 超窄屏（iPhone SE / 320-360px）：再压一档品牌区，按钮组退化为 2-across 保证单行 */
@media (max-width: 360px) {
  .yl-brandmark img.yl-brandicon { height:24px !important; }
  .yl-brandtext { font-size:.94rem !important; }
  .yl-brandtail { font-size:.86rem !important; }
  .yl-brandwave { font-size:1rem !important; }
  .yl-nav a, .yl-nav .yl-navcur { font-size:.62rem !important; }
  #yl-ctrlbar button { width:30px !important; height:30px !important; min-width:30px !important; }
  .mm-btn-row button { font-size:.76rem !important; }
}
/* ===== /YUNLAI_MOBILE ===== */
/* ===== /YUNLAI_BRAND ===== */
"""
_YUNLAI_BRAND_JS_BODY = r"""
  // YUNLAI_BRAND runtime — 深色由 :root CSS 变量强制（不依赖 .dark class，Gradio 6 水合会抹掉它）
  if (!document.querySelector('link[rel*=icon][href^="data:"]')) {
    const l = document.createElement('link');
    l.rel = 'icon'; l.type = 'image/png';
    l.href = 'data:image/png;base64,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';
    document.head.appendChild(l);
  }
  const setT = () => { document.title = '快速音频克隆、ASR转写、音乐生成 | 云来AI音频工作台'; };
  setT(); setTimeout(setT, 300); setTimeout(setT, 1200);
"""
_YUNLAI_BRAND_HEAD = r"""<meta name="theme-color" content="#0a0e17"><meta name="description" content="云来AI音频工作台：快速音频克隆、ASR转写、音乐生成，本地推理一站式搞定。"><link rel="apple-touch-icon" href="data:image/png;base64,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">"""

# ===== YUNLAI_TOGGLE 装配（VP同款双注入：launch js= 箭头fn + head 自执行 IIFE，幂等守卫）=====
# 注意 r""" 原始串：JS 正则里的 \[ \d 等不可被 Python 转义（否则 SyntaxWarning）
_YUNLAI_FULL_JS = _YUNLAI_BRAND_JS_BODY + r"""
  // ===== YUNLAI_TOGGLE（2026-09-27 从配音工作台移植：语言EN/中文 + 主题🌙/☀️）=====
  const ZH2EN = {"输出音频": "Output Audio", "高级参数（只发送改动过的项）": "Advanced (only send modified)", "其它参数（JSON，覆盖控件）": "Extra params (JSON, overrides UI)", "seed（-1=随机）": "seed (-1=random)", "内置参考音色": "Built-in reference voices", "要合成的文字": "Text to synthesize", "上传/录制音频": "Upload/record audio", "说话人分离": "Speaker diarization", "音乐生成": "Music Generation", "源音频分析": "Source Audio Analysis", "声音转换": "Voice Conversion", "音源分离": "Source Separation", "音频分析": "Audio Analysis", "声音设计": "Voice Design", "HF token (下载受限模型时用)": "HF token (for gated models)", "代理 (仅下载模型时使用)": "Proxy (model download only)", "上传/录制参考音色（可选）": "Upload/record reference voice (optional)", "参考文本": "Reference text", "语言（留空=模型默认）": "Language (empty=model default)", "强制语种（留空=自动检测）": "Force language (empty=auto detect)", "上下文提示": "Context prompt", "转写文本": "Transcribed text", "上传源音频（编辑类 route 用）": "Upload source audio (for edit routes)", "时长(秒)，-1=自动": "Duration (s), -1=auto", "提示词（英文效果最好）": "Prompt (English works best)", "歌词（可选）": "Lyrics (optional)", "上传/录制源音频": "Upload/record source audio", "上传/录制目标音色": "Upload/record target voice", "上传要分离的歌曲/音频": "Upload song/audio to separate", "全部音轨文件": "All track files", "对齐文本（align 必填：音频原文）": "Alignment text (required for align: transcript)", "语言（可选，如 English / Chinese）": "Language (optional, e.g. English / Chinese)", "结果（时间单位：秒）": "Result (time unit: seconds)", "原始 JSON 结果": "Raw JSON result", "音色描述（用文字描述想要的声音）": "Voice description (describe the voice you want)", "hf_xxx —— 或先在命令行运行 huggingface-cli login": "hf_xxx — or run huggingface-cli login first", "参考音频里说的原话（克隆时建议填写，越准越好）": "Exact words in reference audio (recommended; more accurate = better)", "人名/术语等，帮助识别专有名词": "Names/terms etc., helps recognize proper nouns", "转写结果将显示在这里……": "Transcription will appear here...", "语音段 / 说话人 / 逐词时间戳将显示在这里……": "Segments / speakers / word timestamps will appear here...", "例：低沉磁性的中年男声，语速偏慢，带播音腔": "e.g. deep magnetic middle-aged male voice, slow, broadcaster tone", "🗣️ TTS / 声音克隆": "🗣️ TTS / Voice Cloning", "📝 ASR / 音频转写": "📝 ASR / Transcription", "🎵 音乐生成": "🎵 Music Generation", "🎭 声音转换": "🎭 Voice Conversion", "🎚️ 音源分离": "🎚️ Source Separation", "🔎 音频分析": "🔎 Audio Analysis", "🎨 声音设计": "🎨 Voice Design", "🔌 API 调用说明：第三方应用如何用本 server 生成语音": "🔌 API guide: how third-party apps generate speech via this server", "🔐 下载设置：HF token / 代理（可选，不保存）": "🔐 Download settings: HF token / proxy (optional, not saved)", "🎛 转写选项（可选）": "🎛 Transcription options (optional)", "📥 加载模型": "📥 Load Model", "🔄 刷新列表": "🔄 Refresh List", "⬇️ 下载模型": "⬇️ Download Model", "📊 下载进度": "📊 Download Progress", "🧹 释放显存": "🧹 Free VRAM", "🎵 生成语音": "🎵 Generate Speech", "📝 开始转写": "📝 Start Transcription", "🔍 分析源音频（自动填入描述/歌词/BPM/调性）": "🔍 Analyze source (auto-fill description/lyrics/BPM/key)", "🎵 生成音乐": "🎵 Generate Music", "🎭 开始转换": "🎭 Start Conversion", "🎚️ 开始分离": "🎚️ Start Separation", "🔎 开始分析": "🔎 Start Analysis", "🎨 生成语音": "🎨 Generate Speech", "🗣 对话模式（≤120s，需 Sortformer）：输出带说话人和时间戳的对话稿": "🗣 Dialogue mode (≤120s, needs Sortformer): transcript with speakers & timestamps", "模型": "Model", "状态": "Status", "加载中": "Loading", "已加载": "Loaded", "未加载": "Not loaded", "停止": "Stop", "运行中": "Running"}, EN2ZH = {"Output Audio": "输出音频", "Advanced (only send modified)": "高级参数（只发送改动过的项）", "Extra params (JSON, overrides UI)": "其它参数（JSON，覆盖控件）", "seed (-1=random)": "seed（-1=随机）", "Built-in reference voices": "内置参考音色", "Text to synthesize": "要合成的文字", "Upload/record audio": "上传/录制音频", "Speaker diarization": "说话人分离", "Music Generation": "音乐生成", "Source Audio Analysis": "源音频分析", "Voice Conversion": "声音转换", "Source Separation": "音源分离", "Audio Analysis": "音频分析", "Voice Design": "声音设计", "HF token (for gated models)": "HF token (下载受限模型时用)", "Proxy (model download only)": "代理 (仅下载模型时使用)", "Upload/record reference voice (optional)": "上传/录制参考音色（可选）", "Reference text": "参考文本", "Language (empty=model default)": "语言（留空=模型默认）", "Force language (empty=auto detect)": "强制语种（留空=自动检测）", "Context prompt": "上下文提示", "Transcribed text": "转写文本", "Upload source audio (for edit routes)": "上传源音频（编辑类 route 用）", "Duration (s), -1=auto": "时长(秒)，-1=自动", "Prompt (English works best)": "提示词（英文效果最好）", "Lyrics (optional)": "歌词（可选）", "Upload/record source audio": "上传/录制源音频", "Upload/record target voice": "上传/录制目标音色", "Upload song/audio to separate": "上传要分离的歌曲/音频", "All track files": "全部音轨文件", "Alignment text (required for align: transcript)": "对齐文本（align 必填：音频原文）", "Language (optional, e.g. English / Chinese)": "语言（可选，如 English / Chinese）", "Result (time unit: seconds)": "结果（时间单位：秒）", "Raw JSON result": "原始 JSON 结果", "Voice description (describe the voice you want)": "音色描述（用文字描述想要的声音）", "hf_xxx — or run huggingface-cli login first": "hf_xxx —— 或先在命令行运行 huggingface-cli login", "Exact words in reference audio (recommended; more accurate = better)": "参考音频里说的原话（克隆时建议填写，越准越好）", "Names/terms etc., helps recognize proper nouns": "人名/术语等，帮助识别专有名词", "Transcription will appear here...": "转写结果将显示在这里……", "Segments / speakers / word timestamps will appear here...": "语音段 / 说话人 / 逐词时间戳将显示在这里……", "e.g. deep magnetic middle-aged male voice, slow, broadcaster tone": "例：低沉磁性的中年男声，语速偏慢，带播音腔", "🗣️ TTS / Voice Cloning": "🗣️ TTS / 声音克隆", "📝 ASR / Transcription": "📝 ASR / 音频转写", "🎵 Music Generation": "🎵 音乐生成", "🎭 Voice Conversion": "🎭 声音转换", "🎚️ Source Separation": "🎚️ 音源分离", "🔎 Audio Analysis": "🔎 音频分析", "🎨 Voice Design": "🎨 声音设计", "🔌 API guide: how third-party apps generate speech via this server": "🔌 API 调用说明：第三方应用如何用本 server 生成语音", "🔐 Download settings: HF token / proxy (optional, not saved)": "🔐 下载设置：HF token / 代理（可选，不保存）", "🎛 Transcription options (optional)": "🎛 转写选项（可选）", "📥 Load Model": "📥 加载模型", "🔄 Refresh List": "🔄 刷新列表", "⬇️ Download Model": "⬇️ 下载模型", "📊 Download Progress": "📊 下载进度", "🧹 Free VRAM": "🧹 释放显存", "🎵 Generate Speech": "🎵 生成语音", "📝 Start Transcription": "📝 开始转写", "🔍 Analyze source (auto-fill description/lyrics/BPM/key)": "🔍 分析源音频（自动填入描述/歌词/BPM/调性）", "🎵 Generate Music": "🎵 生成音乐", "🎭 Start Conversion": "🎭 开始转换", "🎚️ Start Separation": "🎚️ 开始分离", "🔎 Start Analysis": "🔎 开始分析", "🎨 Generate Speech": "🎨 生成语音", "🗣 Dialogue mode (≤120s, needs Sortformer): transcript with speakers & timestamps": "🗣 对话模式（≤120s，需 Sortformer）：输出带说话人和时间戳的对话稿", "Model": "模型", "Status": "状态", "Loading": "加载中", "Loaded": "已加载", "Not loaded": "未加载", "Stop": "停止", "Running": "运行中"};
  let lang = localStorage.getItem('yl-lang') || 'zh';
  let theme = localStorage.getItem('yl-theme') || 'dark';
  function applyTheme() {
    if (!document.body) return;
    document.body.classList.toggle('yl-light', theme === 'light');
    document.body.classList.toggle('dark', theme !== 'light');
    document.documentElement.classList.toggle('dark', theme !== 'light');
    const tb = document.getElementById('yl-theme-btn');
    // 0927 精致化：emoji→矢量 SVG（月亮带星点，太阳带光芒），跨端渲染一致
    if (tb) tb.innerHTML = theme === 'light'
      ? '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="4.4" fill="#fff7d6" stroke="#f59e0b" stroke-width="1.3"/><g stroke="#fbbf24" stroke-width="1.7" stroke-linecap="round"><path d="M12 2.8v2.1M12 19.1v2.1M2.8 12h2.1M19.1 12h2.1M5.5 5.5l1.5 1.5M17 17l1.5 1.5M18.5 5.5L17 7M7 17l-1.5 1.5"/></g></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M20.5 14.8A8.7 8.7 0 0 1 9.2 3.5a8.8 8.8 0 1 0 11.3 11.3z" fill="#fbbf24" stroke="#fde68a" stroke-width="1.1" stroke-linejoin="round"/><circle cx="17.2" cy="6.4" r="1" fill="#fde68a" opacity=".85"/><circle cx="19.9" cy="9.8" r=".6" fill="#fde68a" opacity=".6"/></svg>';
    let m = document.querySelector('meta[name=theme-color]');
    if (m) m.content = theme === 'light' ? '#eef2f8' : '#0a0e17';
  }
  function skip(el) {
    return el.closest && (el.closest('.yl-brandbar') || el.closest('#yl-ctrlbar'));
  }
  function trText(t) {
    const map = lang === 'en' ? ZH2EN : EN2ZH;
    const s = t.trim();
    if (!s || s.length > 120 || /^[\d\s.,:%/\-–—+*#@()[\]{}<>|]*$/.test(s)) return null;
    if (map[s] !== undefined) return t.replace(s, map[s]);
    if (s.endsWith(':') && map[s.slice(0, -1)] !== undefined)
      return t.replace(s, map[s.slice(0, -1)] + ':');
    return null;
  }
  function walk(root) {
    if (!root) return;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: n => (n.parentElement && !skip(n.parentElement) && n.nodeValue.trim())
        ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP });
    let n, batch = [];
    while ((n = w.nextNode())) batch.push(n);
    for (const tn of batch) {
      const nv = trText(tn.nodeValue);
      if (nv !== null && nv !== tn.nodeValue) tn.nodeValue = nv;
    }
    const scope = root.querySelectorAll ? root.querySelectorAll('[placeholder],[aria-label],[title]') : [];
    for (const el of scope) {
      if (skip(el)) continue;
      for (const a of ['placeholder', 'aria-label', 'title']) {
        const v = el.getAttribute(a);
        if (!v) continue;
        const nv = trText(v);
        if (nv !== null && nv !== v) el.setAttribute(a, nv);
      }
    }
  }
  function applyLang() {
    walk(document.body);
    const lb = document.getElementById('yl-lang-btn');
    if (lb) lb.textContent = lang === 'en' ? '中文' : 'EN';
  }
  function mount() {
    let bar = document.getElementById('yl-ctrlbar');
    if (!document.body) return;
    if (bar) {
      const bbNow = document.querySelector('.yl-brandbar');
      if (bbNow && bar.parentElement !== bbNow) { bbNow.appendChild(bar); bar.classList.remove('fallback'); }
      return;
    }
    bar = document.createElement('div');
    bar.id = 'yl-ctrlbar';
    bar.innerHTML = '<button id="yl-theme-btn" title="亮色/暗色"></button>'
                  + '<button id="yl-lang-btn" title="中文/English"></button>';
    const bb = document.querySelector('.yl-brandbar');
    if (bb) { bb.appendChild(bar); bar.classList.remove('fallback'); }
    else { document.body.appendChild(bar); bar.classList.add('fallback'); }
    document.getElementById('yl-theme-btn').onclick = () => {
      theme = theme === 'light' ? 'dark' : 'light';
      localStorage.setItem('yl-theme', theme); applyTheme();
    };
    document.getElementById('yl-lang-btn').onclick = () => {
      lang = lang === 'en' ? 'zh' : 'en';
      localStorage.setItem('yl-lang', lang); applyLang();
    };
    applyTheme(); applyLang();
  }
  if (window.__ylRuntimeStarted) return;
  window.__ylRuntimeStarted = true;
  if (document.body) mount(); else window.addEventListener('DOMContentLoaded', mount);
  setInterval(() => { mount(); walk(document.body); }, 800);
"""
_YUNLAI_BRAND_JS = "() => {" + _YUNLAI_FULL_JS + "}"
# 0927 Owen 手机端拉宽：.main 的 padding 改不了 launch(css=) —— Gradio 给所有选择器加
# .contain 前缀，而 .main 是 .contain 的祖先（连 .gradio-container .main 也被改写成
# .contain .gradio-container .main）→ 永不匹配。改走 head <style> 注入（Gradio 不重写
# head），裸 .main + !important 压过 adopted 的 .embed-container>.main{padding:var(--size-4)}。
_YUNLAI_BRAND_HEAD += (
    "<style>"
    "@media (max-width:860px){ .main{ padding:10px 10px !important; } }"
    "@media (max-width:360px){ .main{ padding:8px 8px !important; } }"
    "</style>"
)
_YUNLAI_BRAND_HEAD += "<script>(function(){" + _YUNLAI_FULL_JS + "})();</script>"


# Gradio 的 Audio 播放器（WaveSurfer）换音频时复用同一实例，旧的播放进度会
# 原样带到新音频上（生成完成后进度条停在上一次的位置）。value=None 清空也挡
# 不住。修法：每个播放器宿主挂一次 loadedmetadata（每次换源触发、用户 seek
# 不触发），换源后开一个 ~3s 守护窗，暂停状态下把 shadow DOM 里 wavesurfer 的
# <audio>.currentTime 清回 0；窗口内用户 pointerdown（捕获阶段，躲开内部
# stopPropagation）立即取消守护，不干扰手动 seek/播放。
_RESET_AUDIO_SEEK_JS = """
() => {
  if (window.__audiocppSeekReset) return;
  window.__audiocppSeekReset = true;
  const seen = new WeakSet();
  const arm = (host) => {
    if (seen.has(host)) return;
    seen.add(host);
    const attach = () => {
      const a = host.shadowRoot && host.shadowRoot.querySelector('audio');
      if (!a) { setTimeout(attach, 250); return; }
      const guard = () => {
        let tries = 0, cancelled = false;
        const cancel = () => { cancelled = true; };
        host.addEventListener('pointerdown', cancel, { once: true, capture: true });
        const tick = () => {
          if (cancelled) return;
          if (a.paused && a.currentTime > 0.05) { try { a.currentTime = 0; } catch (e) {} }
          if (++tries < 12) setTimeout(tick, 250);
          else host.removeEventListener('pointerdown', cancel, { capture: true });
        };
        setTimeout(tick, 100);
      };
      a.addEventListener('loadedmetadata', guard);
      if (a.readyState >= 1) guard();
    };
    attach();
  };
  const scan = (root) => root.querySelectorAll('#waveform > div').forEach(arm);
  new MutationObserver((muts) => {
    for (const m of muts) {
      for (const n of m.addedNodes) {
        if (n.nodeType !== 1) continue;
        if (n.matches && n.matches('#waveform > div')) arm(n);
        else if (n.querySelectorAll) scan(n);
      }
    }
  }).observe(document.body, { childList: true, subtree: true });
  scan(document);
}
"""



# YUNLAI_BRAND theme: expogent dark palette (light=dark，双模一致，杜绝白闪)
_yl_theme = gr.themes.Base().set(
    body_background_fill="#0a0e17", body_background_fill_dark="#0a0e17",
    body_text_color="#e5edf7", body_text_color_dark="#e5edf7",
    body_text_color_subdued="#8296b0", body_text_color_subdued_dark="#8296b0",
    background_fill_primary="#0a0e17", background_fill_primary_dark="#0a0e17",
    background_fill_secondary="#0d1420", background_fill_secondary_dark="#0d1420",
    block_background_fill="#111827", block_background_fill_dark="#111827",
    block_border_color="#1f2b3d", block_border_color_dark="#1f2b3d",
    block_label_text_color="#8296b0", block_label_text_color_dark="#8296b0",
    block_title_text_color="#e5edf7", block_title_text_color_dark="#e5edf7",
    border_color_primary="#1f2b3d", border_color_primary_dark="#1f2b3d",
    border_color_accent="#22d3ee", border_color_accent_dark="#22d3ee",
    color_accent="#22d3ee", color_accent_soft="#164e63",
    link_text_color="#22d3ee", link_text_color_dark="#22d3ee",
    link_text_color_hover="#67e8f9", link_text_color_hover_dark="#67e8f9",
    button_primary_background_fill="linear-gradient(135deg,#22d3ee,#38bdf8)",
    button_primary_background_fill_dark="linear-gradient(135deg,#22d3ee,#38bdf8)",
    button_primary_background_fill_hover="linear-gradient(135deg,#38d9f0,#4cc5f9)",
    button_primary_background_fill_hover_dark="linear-gradient(135deg,#38d9f0,#4cc5f9)",
    button_primary_text_color="#04121a", button_primary_text_color_dark="#04121a",
    button_secondary_background_fill="#0d1420", button_secondary_background_fill_dark="#0d1420",
    button_secondary_border_color="#1f2b3d", button_secondary_border_color_dark="#1f2b3d",
    button_secondary_text_color="#e5edf7", button_secondary_text_color_dark="#e5edf7",
)
_yl_theme.font = gr.themes.GoogleFont("Inter")


with gr.Blocks(title="快速音频克隆、ASR转写、音乐生成 | 云来AI音频工作台",
               fill_width=True) as demo:   # 0927 Owen：补满宽（对齐配音站），消除左右多出空间
    gr.HTML("""<div class="yl-brandbar"><a class="yl-brandlink" href="https://www.yunlaiai.com" target="_blank" rel="noopener" title="云来AI官网"><span class="yl-brandmark"><img class="yl-brandicon" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAABgCAYAAADimHc4AAAKMWlDQ1BJQ0MgUHJvZmlsZQAAeJydlndUU9kWh8+9N71QkhCKlNBraFICSA29SJEuKjEJEErAkAAiNkRUcERRkaYIMijggKNDkbEiioUBUbHrBBlE1HFwFBuWSWStGd+8ee/Nm98f935rn73P3Wfvfda6AJD8gwXCTFgJgAyhWBTh58WIjYtnYAcBDPAAA2wA4HCzs0IW+EYCmQJ82IxsmRP4F726DiD5+yrTP4zBAP+flLlZIjEAUJiM5/L42VwZF8k4PVecJbdPyZi2NE3OMErOIlmCMlaTc/IsW3z2mWUPOfMyhDwZy3PO4mXw5Nwn4405Er6MkWAZF+cI+LkyviZjg3RJhkDGb+SxGXxONgAoktwu5nNTZGwtY5IoMoIt43kA4EjJX/DSL1jMzxPLD8XOzFouEiSniBkmXFOGjZMTi+HPz03ni8XMMA43jSPiMdiZGVkc4XIAZs/8WRR5bRmyIjvYODk4MG0tbb4o1H9d/JuS93aWXoR/7hlEH/jD9ld+mQ0AsKZltdn6h21pFQBd6wFQu/2HzWAvAIqyvnUOfXEeunxeUsTiLGcrq9zcXEsBn2spL+jv+p8Of0NffM9Svt3v5WF485M4knQxQ143bmZ6pkTEyM7icPkM5p+H+B8H/nUeFhH8JL6IL5RFRMumTCBMlrVbyBOIBZlChkD4n5r4D8P+pNm5lona+BHQllgCpSEaQH4eACgqESAJe2Qr0O99C8ZHA/nNi9GZmJ37z4L+fVe4TP7IFiR/jmNHRDK4ElHO7Jr8WgI0IABFQAPqQBvoAxPABLbAEbgAD+ADAkEoiARxYDHgghSQAUQgFxSAtaAYlIKtYCeoBnWgETSDNnAYdIFj4DQ4By6By2AE3AFSMA6egCnwCsxAEISFyBAVUod0IEPIHLKFWJAb5AMFQxFQHJQIJUNCSAIVQOugUqgcqobqoWboW+godBq6AA1Dt6BRaBL6FXoHIzAJpsFasBFsBbNgTzgIjoQXwcnwMjgfLoK3wJVwA3wQ7oRPw5fgEVgKP4GnEYAQETqiizARFsJGQpF4JAkRIauQEqQCaUDakB6kH7mKSJGnyFsUBkVFMVBMlAvKHxWF4qKWoVahNqOqUQdQnag+1FXUKGoK9RFNRmuizdHO6AB0LDoZnYsuRlegm9Ad6LPoEfQ4+hUGg6FjjDGOGH9MHCYVswKzGbMb0445hRnGjGGmsVisOtYc64oNxXKwYmwxtgp7EHsSewU7jn2DI+J0cLY4X1w8TogrxFXgWnAncFdwE7gZvBLeEO+MD8Xz8MvxZfhGfA9+CD+OnyEoE4wJroRIQiphLaGS0EY4S7hLeEEkEvWITsRwooC4hlhJPEQ8TxwlviVRSGYkNimBJCFtIe0nnSLdIr0gk8lGZA9yPFlM3kJuJp8h3ye/UaAqWCoEKPAUVivUKHQqXFF4pohXNFT0VFysmK9YoXhEcUjxqRJeyUiJrcRRWqVUo3RU6YbStDJV2UY5VDlDebNyi/IF5UcULMWI4kPhUYoo+yhnKGNUhKpPZVO51HXURupZ6jgNQzOmBdBSaaW0b2iDtCkVioqdSrRKnkqNynEVKR2hG9ED6On0Mvph+nX6O1UtVU9Vvuom1TbVK6qv1eaoeajx1UrU2tVG1N6pM9R91NPUt6l3qd/TQGmYaYRr5Grs0Tir8XQObY7LHO6ckjmH59zWhDXNNCM0V2ju0xzQnNbS1vLTytKq0jqj9VSbru2hnaq9Q/uE9qQOVcdNR6CzQ+ekzmOGCsOTkc6oZPQxpnQ1df11Jbr1uoO6M3rGelF6hXrtevf0Cfos/ST9Hfq9+lMGOgYhBgUGrQa3DfGGLMMUw12G/YavjYyNYow2GHUZPTJWMw4wzjduNb5rQjZxN1lm0mByzRRjyjJNM91tetkMNrM3SzGrMRsyh80dzAXmu82HLdAWThZCiwaLG0wS05OZw2xljlrSLYMtCy27LJ9ZGVjFW22z6rf6aG1vnW7daH3HhmITaFNo02Pzq62ZLde2xvbaXPJc37mr53bPfW5nbse322N3055qH2K/wb7X/oODo4PIoc1h0tHAMdGx1vEGi8YKY21mnXdCO3k5rXY65vTW2cFZ7HzY+RcXpkuaS4vLo3nG8/jzGueNueq5clzrXaVuDLdEt71uUnddd457g/sDD30PnkeTx4SnqWeq50HPZ17WXiKvDq/XbGf2SvYpb8Tbz7vEe9CH4hPlU+1z31fPN9m31XfKz95vhd8pf7R/kP82/xsBWgHcgOaAqUDHwJWBfUGkoAVB1UEPgs2CRcE9IXBIYMj2kLvzDecL53eFgtCA0O2h98KMw5aFfR+OCQ8Lrwl/GGETURDRv4C6YMmClgWvIr0iyyLvRJlESaJ6oxWjE6Kbo1/HeMeUx0hjrWJXxl6K04gTxHXHY+Oj45vipxf6LNy5cDzBPqE44foi40V5iy4s1licvvj4EsUlnCVHEtGJMYktie85oZwGzvTSgKW1S6e4bO4u7hOeB28Hb5Lvyi/nTyS5JpUnPUp2Td6ePJninlKR8lTAFlQLnqf6p9alvk4LTduf9ik9Jr09A5eRmHFUSBGmCfsytTPzMoezzLOKs6TLnJftXDYlChI1ZUPZi7K7xTTZz9SAxESyXjKa45ZTk/MmNzr3SJ5ynjBvYLnZ8k3LJ/J9879egVrBXdFboFuwtmB0pefK+lXQqqWrelfrry5aPb7Gb82BtYS1aWt/KLQuLC98uS5mXU+RVtGaorH1futbixWKRcU3NrhsqNuI2ijYOLhp7qaqTR9LeCUXS61LK0rfb+ZuvviVzVeVX33akrRlsMyhbM9WzFbh1uvb3LcdKFcuzy8f2x6yvXMHY0fJjpc7l+y8UGFXUbeLsEuyS1oZXNldZVC1tep9dUr1SI1XTXutZu2m2te7ebuv7PHY01anVVda926vYO/Ner/6zgajhop9mH05+x42Rjf2f836urlJo6m06cN+4X7pgYgDfc2Ozc0tmi1lrXCrpHXyYMLBy994f9Pdxmyrb6e3lx4ChySHHn+b+O31w0GHe4+wjrR9Z/hdbQe1o6QT6lzeOdWV0iXtjusePhp4tLfHpafje8vv9x/TPVZzXOV42QnCiaITn07mn5w+lXXq6enk02O9S3rvnIk9c60vvG/wbNDZ8+d8z53p9+w/ed71/LELzheOXmRd7LrkcKlzwH6g4wf7HzoGHQY7hxyHui87Xe4Znjd84or7ldNXva+euxZw7dLI/JHh61HXb95IuCG9ybv56Fb6ree3c27P3FlzF3235J7SvYr7mvcbfjT9sV3qID0+6j068GDBgztj3LEnP2X/9H686CH5YcWEzkTzI9tHxyZ9Jy8/Xvh4/EnWk5mnxT8r/1z7zOTZd794/DIwFTs1/lz0/NOvm1+ov9j/0u5l73TY9P1XGa9mXpe8UX9z4C3rbf+7mHcTM7nvse8rP5h+6PkY9PHup4xPn34D94Tz+6TMXDkAAE73SURBVHjalf1nnG1bVeeNf8ecK+xYu3LVqXPOvdxEzoiAJAFFxYBPB+ARMaK0CQOmts10t1kbQwdpbVskiICtgqLSjSIIgoRLvJebz6mca+cV5hz/F3PuqkP/Xz3nc9Znrx1W7bXHGHPE3xgT773u7+/pwcGBqvd6enqi29tb6r3XyXisW5vXtSwKretat7e2dDgcqvded7a39fjoSFVVj44OdXdnV1VVB4OBbl6/rs45LYtSNzev63gyUeecbm9ta//sTNV73d/b1/29fVWvOuj3dWtrS+u61mI61a3NTZ2cX7Olg0Ffvfe6vxfu03uvpyenuruzo957HY2GurW5qWVZqov3eXZ2pqqqB/v7enR4pN57PT4+1r293XjN6Pyasix18/p1HQ6Hqqq6u7t7/tsODvZ1bzf8tn6/r1tbm+qc06IodGtrSyfjcaDHzo6eHB+r914PDw/O7/Ps7Ey3t7fVe6/jyUS3rl/XYlpoVde6u7Ojsre7qwuLS6g6jg6PmF9YIMsyDvb3abVadLpdTo6PUVUWl5YYDgaMx2NWVlcpy5Kz01MWFxex1nJ4eEir3aLVanN8fIQxCfPzPfr9M6qqZnlpmfF4xGAwYHl1BfWe46Nj5no9Go2cw8MjsjRlrtfj7PSU2jmWlpYYDoeMRyOWV5ZRVQ4PDpnr9Wg2mxzs79NsNul0uxwfH4MqC0tLTCdjBoMBKyurVGXFyckxS0tLGGs5ODig2+3E+zzGiNBbWGA0GDKdTlhaWqKqa85OT5lfWMBYw/HhEe1Oh2azycnxMdZaevPznJ6dUhUVK6srTKdTzk5PWVldwXvl6PCQ+YUF8jznYH+fRrzP05MTfPxtiUksztUAJGmCquKcI8tSEKGqKqy1eO+pqwpjDEma4OoaUNIspaqrcH2SoF6pqyqcK9R1TZIkeOcp6xKbJKRZSl1WGGuwiUXVU1U1aZriVSnj9c57ytl3Jgl17RAR0jxDVSnLiizPz+8zSSzOu3hvYI2lrmsQSNOUuq6xYsjzHOdcuLcbfluSGIyxVHWFqmCThLquSSWNv8fjXI219vw70yQ9/1uqen6fEL7Texc+l2UYEeqyIjEWB+Fzqqrb29vny+zk+FivX7umqqrj8UgffPABrapKvXp96MEHdDgcqKrqtYce0qOjw7hMD/T6tWvqVfVsMNT7H3hAK+e1qGq99uCDWkwLVa+6ee2anp2cqKrq4d6e7m1vh6V9cqrXH3xQnfNalqVee/ABHQ9H6p3q5kMPav+0H1TD1rbu7eyoqurp8Yluxfucjid67f4HtC4qdbXXzQcf0tPjE1Wvuru1pUf7h6pe9XD/QHeub6p61fFootcefECrotKqrPWhBx7QYT+ooO3NTd3f31evqnt7+7q1uaXOqR4f9/XBBx5S51Wnk6lee+ghLYqpqqpuXr+uJ8fH4T53dnR3J/y209MTvfbQg6qqOplM9IH779OyKFS9183r11VOT041b+Z455lOJjSbTYwYhqMhWZ6TZRmTyQQB8kaDqiqpq5pWu03takbjCa1Wm6Y16HhAkiWQN2EyAgEabSinoA7N24ivoCzQvAHGINUETXIUg/FTMBZHiqUE9XjTAK0w1DjJEREMBaoWlQxjpqDgfI41JYjH+QbWePATVNooDiMl0MArGJkCKV4TjBSAQTVDpAQc0CD8GwEtwAJ9IAdyhqMhrkrIsiZlOcG5mk63Q13VTIuCVquFes94PKHZamKtZTQakaZhJZVlAQiNRk4ynU5pd9qo8ZyeTmm226RZRnVakqQJWZYxHo0oqpK53jxFUTCdTmnPL2Cdp+1K2nmHkU35/Innmja4q8i569hzqoYizRlMDdQ1SZbjpxY/EbKkCU6oJx5rcqxY6kKgFoxN8LVF1COkqE/wRUWaNFCvVFMlSXPwBl8rIpYky6gKA96TZk28r3FqsCYHVXyRYCRF1KBTA7WlQUo1tlBAO8+oC4ufOJqmAc5DKbSTnF4qzDcbPObhGY96eM7qUp+1jRqwOPUMhyXz8xkuCvHc3BwKTI6PaLWbZFnG6ckJAnQ6HaaTCWVV0e12EVXVvb09EmtZWl7m5OSEyWTCxsYG48mYo4ND1tbXSZKE7a0teouLtFpt/N4WdNq8L5/jbds17+lbHppYqhoQpZEKLQHrPdYYAOpaqTwUKkwnCgXgBaYKpUJlwBPOawnnhYJKEMxSoQYQqOO5j+95Da97oPLhmtnnAERAARf/ngLeIxhSB8Z7EjXYCnSimAKkFhgrMoWkFphAWnuurAhPe7LjOV864kX/okGzm7J/sEtiWiwuzbO/v4eIYWVlhbOzM0bDIZc2NiiKgoP9/UBPa9nb20NGo5F6V4MKxppwj3U0NMZQlSVpmmJEmJQVPQO2mfC/+47Xnrb5+2ND7uCJHXjGvHBLS2knSumgX8Gph9MK9grYLOCgEgYVDL1SVwIlSA1aAjMN4IjEnZ0r6gRxF59RwmekJBITtFYkElq9Ih5wkdgogmAFEoEsgdQoqYXcQIqSAakTbAEyATsBMxWSClIFmSh1X5ieKWcHhuGh8rhHwnf/ALzwJQNowWSYAjXqPUma4rxHnSNNU1SVqq5JbIIYcLVDrl+7ppevXMHVjq3tTdbX18nzBg899BBz3S4Li4scHh5QFCWXL19mZzLiJ7YNbzhtclWUb19RnrsMCxns1rAzhesTZa8QdqfwQF/YHMP+GFwRCRslV5xG6Q1EFQc6Y8BMWmfMqGdSHx+9hPf9DYya/S00XD9jjGpYIT6sTjBYMSRAZiG1gZHVGPwYbAUZ0DZKQ8EUgq2hCbQMzOUwl4GpYfch+PzdyjOfavkPvz3l8hcf4soNjHi2Nq+zur5Gq9lia3OTZrvJ4sIShwcHVFXFpY0NpKoqPTs9xVhLp9NhMh5TlgW9+QWqsmIwHNCa69FNhY8OS759r8v9A/iuJc/3XxFKo3y2hM2xsj2F3alwOIFrZ8L9Z3B0g1SbKhIzPtdzSdcgqTOC30jMG1dEfJwxSer/i1F+pooUVR80D4rOVoD34T1AEEye4EYCp4I0DZfnhJVc0VI5PFH2txz1CSz0DEsNg07CCmlaoWmExSY8bFVppfAPH4STQ/i9/+l4+jf0OTiC+bk5JuMR02nBwuIidVVxGmMLay1np6ck8W5QDctXRPCecG4E7z3dRHl31eA7Hkpp1Y6/ebjw9DnhA4XyyT6cFnA4FQ6m8OAZ3HsgnBYwcSDVhWRrfSMho0TOXlP9QkK7//v92XmU5sgImRHdaZB0Dc4X4hBRVAITwlJw4DxGFKzg7nVcXs95xcvbvPhLDA+/AnkmCFAWyr33Wt75t47/+YcFn7/H87ANi6sMrjDUYilGwsmpcPMSfMv/A/fuwUtfnvLLn2ny0v8wYVoAmGiC5Ibjhueb16/rpcsbeOfZvH6d9fV1Gs0mW5vXaXa6LM7P896TMS+5r8GGgb9+rGEhUz48VT7ZVx4cCPsFHE/h/n1h51SZOgm281yvBwmXOujpL9DvLj7/Aum/4bzW8+fqNNiB+gbVc6N6UR8Oo6jxiESVYwDvwNeId4ivcVsF3/r1y/zM9yxw85qNHI7qC2EKDBGaKMebyi/9wpT/+gcT1tcNmRqsS8gxNE1CQxIWm55X/mvY7Qu/8huG33pdzfO+e4tyskyn22bz+nXa7TYLi4scHR5QVzVrly4h4/FIqypEi9ZYvHN47zBZThvHZ1zCi+9vUowdf/8kYSmDD4zhgRHceQT9StgdwEO7cDKB0kPlIvGj9GqtQV3M1E4JEqX2XKJLvfBYZh6PRsl2IHVQJVJfSDx+put91Pv+guDGR+/nBiNRV1hq6s8c8zM/fpWf/b4rgOJrKAX2gE2FAwcnHpxTujUsN4RnN4T/+roxP/BjfS5tGKwzZLUlk4SGpLSSlK4I3/8tykOH8PO/ZXjHWwqe9tKSo0NPu5NT146qKsmyBiJQFEVwQw8PDkCE5eVl+mdnDEcjNi6tM6xLvuYzhg+fpLzhScoTOsr7z4T9qXJvX9ifCDsncH0fRlNl6oWy4pzYVArVjTp8Ju0+uIqzz2iU6soHGt7ImHP9ruBm77sLfa/R2gqB0KKIBZOCJpGbzoPWWFtT3nvMS5/f5S3/+XHUtceI0Ddwv8IDNVx3yq6H/Tqo0KaHKzU8MhO+YVH40R8941d/44SNqzlMDBkJmRgaJMznTTrW8vXP87z3Q7B/YHnbh/tUnSELc5dwvuRg/4CVlVWSNGFvdxfZ2trS1dVVvPfs7e6ysrqKSTOy4x1eN13iB+5u8G2XPa+8DT52ApsjuP8MhjVsHQvXd5TSCZMaXHWDhNZB0rWKBI1E1fgotUeroFK09tELikS+4fPio0/vPHh/wRTVaFiYuT5hFeeeulQYOihrsBqYUBTQH9JIRnzqn57P7RtNtPacGeEeVR6ohTOUQ4XrlXC9VDYL2CthzcDXpPC8nvBMlDsed53DE2W+nWDqhAxLqoa2SekmLeabhmc+XnnjX8A3vsTwI3+obG/v0Wo2mV+Y5+jwAOd9YESr2aQqK1SVTqdDWdV0qLkv7/LauzJuy5UXbsCHjpT7BsL1vjKqhK1DYecAShWmZVA5UkV1M5P4Kqqe2p9LstQOrRRqH42zR6qo471H4mfFK9QOvEafPqqY+KgK4l20AR6SYITrrZK5OeHJT23x5EfPc/OljFbDMRo4Pv6xQ9aXlrj1UoPKKyMj3Ong/kooUc4UrpWwU8JxJRwXcFgG13rYVm5uwHMWDC9/WYtf/KVDur0mrvaEKMIgDixTZNTgU/cYnvoo5Q1vgBe9sub2pzeZjiyTyYQsy3HeM51OSOYXFsJSMIbV1VUOj0/QcsQfTi5zdAhf/yjlqFI+dSLsjmBcCZsHwvZe8BzLKur0KhLcxejVBZXCjAH1zMuJ6sdrUCeVR2sX1U54PaiYmWpy4OpAcI3Edz54NjHIManiTyuS04pXvWyV7/mudR71iBb/f/++6RIAU1Uqgc+WykdGUCK0OyEg1xpGBu6fKCcTwdVgRnDvSLizo9h54TnPyfilX6moSTGZRT3UzkCt2MogVtg5anD7hmBFectveF77p/O4uuBgf4dLG5exScLO9hbJweEhvfl51Ct7e7ss9uYY2g5v/KTSToUrHfjUkbDZh2EBW8ewfRjd7yISvgxGU+sZI3xcCRfGVesg3ep8MKi1j7bRR0n38XPunNBB3bighpw/N7ZBLXlwDmM8elaxVHt+6/cfyfNetMRHJvCGBz2Vh0fPwVcuwpIGbeQM3FcJBxV8Yix8qlLWuvCJ+2HrBKwRjFMeMyd8VpWdMyFxoGP4UEfZ34DeUobWU453HCRdkrpmrm3pNaAowTglk4TjQcrlS8r7399g774TejcLa2sbjIZDVJXVlVUSX9dYY1Cj1F5JDXx8ZLn/QPiiy9Av4METGBXBxbq2Hz21KhK7UCgFLRWqSNiZEa01uJ2lO18F4nxghvMxfeCicQ3Exs3UTiQ6PqgfXwfmaYy41AfX0nmSoxH/8U1P4cpXLfFTd9XceWp5cAhHI6Hhldc+RfjBmxRRYaBw71S5awD3O2H7RPjrDzj2HrDwGYElD483XOopj1+C+QQ+dwiosNMX7lK4ds8JnfaIpzznJuYWOuxvFdz9qSkPbBdc7nVo5IZpXdCfGJaWLFufUj7+D5avuM3hNEVRnPcYa0nW1tfZ39vDGOHypUswOeLt9zvUd7ml67k+CO7lyQSu7wXCm1nupiIk1MqoVsqoRlwkWvRsqNy5etHKh5WhUS1F6RfvUVdfMMPH17WOfr5D1CE48A5fO5KG4K6d8MwXr/HCr1rmPfuOvhMqp7RFKBz0d4XmNES+jqDft0awXcBdfeXDfypM+wLbni/OYS33vOdjnp11Q/lFhse1PSc57A2ErTG87xi+8ak9PvOZp3HTTRdq7u57Ct76lj6v+/U+h2eey/OW/jRjqWuRvOJ9fz3HV35LyfbOdS5dukSSJOzsbJMMhwNarRZelWG/T6eR8Y9nLTKreIXtMxhWwuY+1OPgVmsxI/4si+mRygeXMup4dT4Y2sqdez7qguSr9+dxgNQuEtuFVeFq1NXBq1SP+voihVDX6GQaPts01KdjGI541SsfTgvlI1vKR++z7AwVsZ4qNaw2la/bEADOPBzWsDOGLeCuDwnTe5WGN/ynVygv/0owacJ9n3P88K84/uYe+OcXw01Lht0DxzO68J09mFtq04y2xACpwCPuyPmpn1rhec9t843fsM/xseNS1zEuod2yPPQpR1WUzC/2mE4mAHS7cyRnp2dsXL5MVTv6+zvUzRXu61uWjGcwhcOxcDSA8SCu/BKk0vBYapD+IrqOtUNKH9VNJH7036l88IC8Bw26Hq/nki8zlVLX4Oro33tEPVrXMJiQpDWLXYNWjoPtPkuJ8nO/81S+7EuX+XefgT+9L2HBwdOeKKiBOz/nudyES21wKP1a2B8rAwcHI5h8XpD7PN/8Es+rvtbytlPlI1PlqbdZ3vzb8MyvO+OuP5jn/pfD0pLyM482fN4pO0OoVTEGmgn0EuG2RElL5VnPafEXf36ZF3zpDsMiZb5q0MoMhw/VHG47Lt3S4/DgEOfqkJZev3QpFN1FWNq4zMeORgxOUxZ6lsHUcTYRTo5j8mtmcIsL4mvhofTBc6kUKhf1emTITPKjrp+5jhqNq7oLFYOvI2Pq87SCViXtVLj1S68yf+s867e0aa7n3LE44fY72mQrOd/wEdh/cMLq33yOw489CFdyXvqDT+BhT7rC3mc8Zwr39kOscjqFpkInE7LUoX1lec7waeD3J8J92/CePY99vOUHXlHyb354h2m1yMt/MWUrE/7mfkURTA5tgS7Qayh3N4TbEuHeE88znpzyY6/p8dqfm3B5SUkST18tbtqkP9il3VkkTYW9vR0S731IxgG4klEJvjBglf5U6PfBT2JEX0a1M/VoFQlfhHOtArFxLkh+tAXBqIYVEYg+893DagjGOBLd1dHrqaO7WdPIDO2VLrt373PPR7eYjiY0Fhs8/Msv07ylx/5xAZ8/5PQf7+Zkpw+NhL27Cn7lL/6Jb379S3jUcx/LA/uO+0vhsychi306BdtQHvc84e8+ZPi1N3gObxHuO/aku4p8neEdO/DSmzwUd5Pf92RuIuftW45P7BiSFuRDQ7upLC8onQFsNmGzpWwomFT59h+Y449+r+TwsGJ9sUGFMpk4FhLBO4czQpKkJPt7e2xcvkxR1gyPt5B0HSkFVysnFUwHF14PZUjVUsbgqQyEZ3buouRX7pzwYTW4Cymfnbs6uJveg6+C7vfB58c7UI/gKaua/Xs3oapDWtV4ppsnfPJ19wMVJq1CHNxpIWutUJAxKdMxvOnH3s2L/2iF+gVr2Icqts4sByWc1DASuPlRwte/xnPn2+FPfkN53FXlKd8vvHMAJz14oD+GyQ75iuHBBnziUNh+EHibwqnDPiZh7emw+Bhlf6Bst2HchkyV268anvccy9veVHJ5oYn6itHY026usb93gGrN2volksWY//EIvcV1pjseKT1OhcFEziNcpkH3myjVOnMpXVA/Go2oRgnWmWTXDnXhkJnqccGjUZ0FXfGItiDYhOBu+jqkE0wqqJoQvCUJptNGNQRoJrGEJLKGbL/zJN15Bgfb/Ol3/zHrb/o2vuPpHT5xpJyOlUxBjHDf/crNTxG+40uFxkSp5ix/sQt793m+5FnwgfcegUD/2jF/+Xcd9haES59WfuqrYHNzzC+/Ycj2/hrVvOWk7VmcCM0YiD5zA259tGWqJXUVahB5aplOj2m12xgDJ8cnJI0sZzQY4MVgki6uHiPApBLGo5hKqMLhi+hKTICpC1JZOSjqc7cRd4Mx9SHvbK2C8bgiEFo1Rr6R8OpvUEMafX3vUNGQUtZgR85LXD4wz1qLU8VPYxrZSKj9kuLLEjrzjHb7/Opzf5utVz+bF3/fF5MvppwdeT5z4ihq4YG7hM93FDKhf48y2ldue7jlYfcc87o3fBa6Xa4u1/zkI+DjOJaer/w/X5NyjTnuOhzyjr86ZfL0JaaPDLe2KZB2QiLPZA5HRe0ciQiJgbIek5g2WWY5Ozsl2d3dYW19naJyjI43yfIVxBkmRfxhs4ylg3Si3LoS6qq2CppDS/ATwZcGXzh8GQitlacceQZHJaNhCYlHliwiIUpWF3LTIc8Tz2cp5VnEGyVaZsUUFFGHTYV6XOFPxoDQXsxoNRtYG65ADJCBGmR9Dpme8e7ffzf3feozfNX3fzVPe9ZlLvcNd+3DtUPPwbEyUVgEnv5Yy/qC478/+/9wQgtj1njFT/fYfhYs7Qt39Cw/fpfjTrVcuSODdwb3PCuE8QSqDMoEjmvY3CzxlHgfXO/x1DHXucLhwTHDYcWlSxskrXab6WRCrdBs9/AjxddKOSO+KokI1YHy2pcIr35+ipWUNLmohTin0d6GaLquFe+FaansH5V8/r4Jf/LH2/zZuw/RlmDnLa66wddXFwslHsUhEhyDgFq4sT7pMcZTb53RWW7yiu96HC/4iodx001d5nsZSWJCVUwkpqejjTGGNDdMDwf84u98hLf84jt5+auew7O++CqnD+swsbDvobLw4OfHvOVbP8LJaQKXL9MrOky/aIHf/AAMjw1LFo72DE96uHL/P1l4ZJvuhmImQU13S8g1BIEff9+EDkpZ1giWNLHUbkKWZ6ABK5T0ej329nZRDHNrq9TVMFatTHAhKwnPNVS8mqlQu/DcEFZ9ksx+sDmPDKOy4Ob1lKc+ps3Lv26ZP/urA37k336Oex4cYjdy/LQ+9/VDatnHcmJcCdSRES6uAo/bO+ElL7mDn33tc3jUw3v8f/l3vdXi/3xwiwc/vsNPf+DtzG/Mc+szbsZuLHDWWmT/SDl93ynYDO64g2S3w8t+foWPG2FyoJgDODqDpWWoPui5R+ZJnyxcmff0x8JKDmu15+GXLNt/c8o/3nnCzXPLlJUjIaWRe4ajPZrpJdI8wHySo6NDlpaWmRYV08EuWTqHzABPUf+7Kdh55Xf/zPG4S5bv/TJLVSuJjfHSjOJoyK+p4pFgOmKtdi4RXvxVKzzjqT1e/vIP8Z5/3Ce51KKe1gge1Zjh5CLXrz4wR8Rh1FPvH/Iff/kF/NsfeQoAkzqkLawJalFvKCyeC4IqojAylq996Tt48OMHpLdepi48p4dTPvbWuyMSbgGaS/CwDUTmWZuUPPcXlhh/TYtP/KPHTQUKWJ6D/BA+d80gtwhPfowy34TOGJ7dVrJFw5Pbys/9/ENYazBGMRpqJONpzcM6lzk57sPIc2ljg0S94r3HWBOcZEzM5XNeiVJC0cQseL7/N8aszrV5yRen1E6xhi+QegVUBK+KIRoeJxxX0K49q8sZf/Smp/H8L3s3n733mGSlhStK5Hz1XNgBIVTyjQj13hG/8Msv4Md/5ClUtcMh5FYQC7XIeVn4QhhCgUackqWWn/jVD3PnX36c5OarVGWFSVt0n3kznSddZv5hXTIjdOp5uomjN2+4/bld9i9Z/vkux3pXWBKwLaU7EDb3YK4rPOUOeMpj4b574dktz5feYundBv/h1XfziU8OeVhvAVcHGhkPRgyIYoxBBbz3ERm3uwvGsLa6yrs/P+RF/6OFzhvMVNFRrM9OHeIq9HBE62zMu353g+fenlLXPhq/G8AMosSYjDrS0ykUHppGWWta7r63zzOe9secIkirEQK3G1ZS8HYqbJpQb+/wb773afyX3/oyJqVDrNAw4JCQw9eZPVJmGAiJf6yXGv7sfz/A13/V72CXL+M1hWODvWmexhOukmUJ2YJw9fk3c9MLlmnMp/SB/R043fQwVeZqoT2G/h4cH8FiQ/iS2+Bpdyi7u8ojW4aveYSwfez4vh+8h3e844DLC13SOmGeLnPVAg3a/LcPlMzdtkc7v0yWJ2xtbSGDfl+NNVQemrbiH/ZTXvhf2vieYqYEBkw8lDU6KbHVBHf9mCvdmn94y6N42LzBadDbATclwRBH4ruYDJ1VGQsXmHBbz/Ln77yPf/kv3oou9FCx6AxOGOsBJhHc4THPfOYl/uqdLyfJLE6gkQgeGLsbqpReb8A0CM57VpvC6VnFU77ol7i+X2CSNjqu0B/+cp78olvZ+cZ3s329gMSCS8nX21x91mUe9eKb6D5yjWwuQUo4OIRqpFxNhVt6yiOW4AkrylwC4gzSV/7wrcf89us2eeDBIatLTdLS0vAZPe0wXy+Q+Aa/+fcVtz5FGY9qrA1IxKTfP2P90gamclTjY4xZ/kJg1AxzqSDe44uadDll89OHfNNrPsvf/t7jSLwPQDUjOA2l2MpfBMVlrIvP6uzHE+F44vi6r7mNn/jpZ/DzP/UukrUrIf0vNng/ovjRhF6n5k1/+C9od1L6haeRBK94WMWQQ5VZbUdjbd6r0koVayyvfs1buHb/dezcKt6MSN7+CqqveSQemL9lke2TA+ylHlobipHn3j9/iIf+7Bqrtyzw7C9f41FftMyTr3RZv5rTaxraNiAiHto17O97/uqtJ7zz7UecXO9je8rKcgcpwaolUYNRiUANj3OeNG1TVoe4ScX6pUsk6+uXOD4+AizLy5cpD6cBVebNDXibWE5UAyrUkxp7S4t/eOdDvPpnU/7bzz0y5PGBqtbzquMMolLWsSYTywNTBwdD4XDi+Lmf/FLu+sQ13vr2fyJdvYm6igAxVfxgh//8lm/jppvmOJo68tQE4pcB5Bsz3ucIlZnex3tunUv4ndd/gDf/wd+QtpZwyRj7Fz+Kf+wl2PeMlgQ3mUBdo1OHd4LJM1jPqZywtTflLf/5fpAHabYa9Not0ryJtx2mSZcTBN+v4WwErYre1ZykAFMpuRcyb0kwWAEjgsXQzA390Tad9jJJJuzt7ZLUzsVuFPB1RQAyywX6TAPAVfRCwlDBl0pya4ff/bU7ed6XLPCyr1iDaBzrGeF9CJKLyIRpXA2lC+Ct62dQ1Mp/+q//kk996rN87v4t7NwCYiz14Rav/oGv5Bte+gSOJjVJYvAKgxImMYE6S0P56O5bo5S15xErKXffc8iP/PDrkbyJq4/g134cd/US5pqD2y2JBYMLUI5ZQcg7kBRRi3QypGfBGyYFTMr4I0wAjxoVGqmSLBmSwpJOhdQLCZaUAAC2akmwWJFziKSVDO9r1FnSJMUc7O/T683TbneZDvZIkpDzCkDWcB5KgtFL8iaqJINKirnS4ju/87184u4+7STkaiofiF5UMKmUcQXjGoZlOPrTcIgI77vP84FBmz948/fQa4xQ16c+vsZTv3iDX/yFr2VYh0DKq3BawiAycljBoIJRBeMqGPh+oXSahrTwvOKbf41Jv8RM+/Bj344+43HIdYesGLCh1SIVf46ZVO8i4Df+ztogU4OUhlRS8iynmVvaGbStD8DdUsjHhry25JqQaUqOJROLVRPg7hgSFRINmZt2a5nxeMjR8RGLS0uYpeVlTk9PmUzGtHqrVNUFxvIGvzJmByQaSgtiQ6zQyRmMp3zrK/83w2kwTE6VMqqaaQ3jShlVQXLHFYwqYVTBYAqpNfzlR2v2r17lp37l5fizT9Npj3j9f/tWkkbCpA6Js0EJZ1NlUt1A/BomTpnG75p4eEzP8rM/92Y+8sHPkDJFX/zl6Nc8H32ohmWD5OH2G0AWsaK4WHVTMApGBeME4w3WWxKfkNQWWwhpYcinhkZpaNSGhhNyZ0mcuTjUkIolkcAAg2DEkCUwmR7SanWZn18IDYJ5luNcTV2VYPJQwTqHdl/AwCW+LmKCoURQMfjSk1ye5xPvf4gf+ncfoJkYchOWW+mVwguFE8YlTEplXMK0VCalMiqUolIUw5v+tqb1vOfzpK98Ib/xq9/EE554E2fTGmOFfqEcTZVJrQxKZVDCqAwra1JD4ZSjiecJVxP+9J2f4Fd//W3B6D3jyfD9r0R2PGbBIC2QLPhKKZDOahUafp9xDqse68E6JfGEwwnWXZxn3pA7S+osKYZMhdwLqUICpFhSSbDElYCQIFhjKOsKESGLTYPJ3u4uq+trTMua0ekWabqIuCjpM1DUzLfWaJAJq0CwQIIrPPaOJV7/m+/naU9Z5tu/4VEcThylC5JZ1BpswA3HpAxMGU9hWoKrhd97l/Kvf+hHeOWXQb/yiLEMS+VwElWaE6ZOqbxQawj2EiNMCs9t65bx1in/5jt+HVNPkCtX8a95NRyDzAt0QsArEYe7ABzFoE8ixNHoLPLWuBIUi2BUETVBn6uc/3KrglGDVRPPw+sGIREhExOYCaTeMZlW3NS5xP7eAWdnoT/AdHtzjIZDqqqg0ZrDuYinj608otyARJ4h7uWcCZBEEKbFLnd4zff+L+76/BlX5m1oSXLKtA46uqhhUiuTUpiUMJ4qoyKAu+7fF1oevuuZisOjIoxr2BvDuA4qbFjBxElgqlecCuNaSRvCI9vwzd/2OvZ3tzDdefRnfhJlDqyHBQm9dhmUJjRfPAZwRs8jb/EOia629T4QX8F4xXpICbiiRCFRxbrweuKFFEOCIVEwGs7PmRMZaERopJaiPKPVbNGdm+Ps7AzTbrcZTyZMxxNsNod6E0X9hg4UnUl/ML6iJhI9ARMOXwu02pyVjpd/45tplY6lTkjc1T4Y5GkN40IYTpThBEZTYTINntzuEfzC18N8Syl8xJ4OlWGpDGf6vlbGNTesAjirPc+6yfLzP/dWPvA37yU1Kfp9P4Au3IyZuqD3W4rJwSWQCzxR4GoE6aEO46PE46P+D3yzPhDbqGIjI4wqiYIlEDdBgmpSIRFDSnQ/1WI8JGJCLUAMWSKMJ2ekWUqn02Y8HgUvaHFpkU53jnKwQ5L64O/HjOe51vE3GGQ1UfptXNMWJAn2YHmRj33kHn74h/+cp6xYUuFc/UwKGE3gbCycDWEwDNJ/5/3wg88Tnnl7IOzEC9uj4PGM6pmxhYkPKqj0ofZ1MHE869aUv/vbT/Prr/0jLJb6G78d//inIwd1qD+0Qqepz8EaeIzCwyXk/vEzpF1I2AVGeAwzdaSY6MHYeCQx55uoBqLPngOpGNJoeBOEVEz4WyLYWA9Y6G0w6Pc52D9gbW0dY6wJel5MSAf4WICX0FkoxBjASESCS0icSWTCbCVIApLhKiVdXeF3f/e9vOOPP80X35HgamVaBddzOIHTERwNoCrh0w8Jz71V+MEvB++FCbA7VfqlBuI7GDth5GDqQ47QG+GsVG5ZtdQ7J7z6Vb+F4Qh5/r9CXvivYd/BqkG7Cm3wDSCFOwzcKtDzwQvSmDEyBNUjUfUYjSrIBXVkkeDJoOe63iCkSPR4LqQ/FUPKzAsKzxMfaiqJNXh12CTBWoNzDrO0tMzx0RH9/il5Z5WqlNhfFVs+XTTE5xmJWchpLqSfDJUMjaB8Z3K0s8B3vPL1nN19xKOuJlQljCbCYAxnQ5hO4foh6BR+42WCCQ2TbE+U42lQQeNag873UHml0lAfm9ZK3hK+aM3wym/9TQ4f+BTmjmfhXvZK/IHCvMBcxIy0QFO4LMqtRmmLklyYMyAWg9QHFeQ9xkeJlwC8mr1uNeSZrCpJZEpo9gtpBzs7ovrKkMgMIRNLM4GT/i7tVofFpSUODg4w/bMzer0ejUaLenoUVBAmJsTCXUpsoJgFaDPJCQ6RISaeEcIqUDUkzSb9iee7v/X3ecKCZ6ELo4lyOoRBH0ZDeOAh5XdeDlcXldIrW9Pgbg6dMqqVcfTtQ3twqPdWXimt8qJbLP/uNW/m4+/9a5K1R+Jf9W/Rfop0FFkwwetpgs9g3cKtVshvzKqEftzzgpDEusG5J6TR1dBoHwSsyLmqCW6HYlWjMg42IRjqiyDMRNuQo1Re6bSXGE9GnJ2dsbCwgBmNx+SNBlmWUkzHGGNiwXwm7RqrszLrLYz2wAS1FWMDFXuhitRS15AsrfKP//QpfunH/oRnPdoyLZXBSJgUwp2fhlc92/C1Tw6e0nYJ+wUMa+GsFMY+QJAq1ZBuiF879J4XPSLhjf/5vfzhr/8uSXMB/w0/hNcFxDhkUWBOkah6ehZustCI2RWdddvEoAskGuBAaEEwEurQcsPrRk10S2OwRnRHxZB4EwkfXM/MCqkJEbDFBENMyBDnSQdX10zGExqNBmZtfZ2DgwNGoxHt+atUUx8iYSPnRvfGQtXMI5qVy0VmKeQEJQEJMxwgw1eeZOEy/+k3/xefe9fHeNQjEoYjz+Z1eMoG/OzLoPLCbg1bk9DYfVLCqNbg98+kVUKl7cg5vvaxKR95+8f5iR/4DaxY9CX/Fl1/LFIGj0fbQBdcB1op3JxAw8RagQtgAikhqUFqG/J3sRQayp5B+ILqCSpIIkPOM5szn98bjI+qyEfpn6kdlaiCgu1IjaGdGY4Hm3S6c6yurrK3t4cpplMajUaAqPsJNo0YGw+i8THK/nn7vwT+oyYMzcDG10IcKCZDTIJKgpoUaczz73/0f3DVn9FdEIqB53WvErKmslPAQxMY1spZBWOnQfrjFAINkQ198Xz1Y1Lu+rNP8F3f/JuYqoTnvgL/mGehQ4esGmROkDnwbUhyuJwGJszUqdRgS8jG0JmCxLE2Rmz0flw0wor4WbAFxgspNmY1owGWaHyjl5RIUMLGCzamIqyERFxKAGt5p+R5K8zbKAqajQbm9OSEXq9Hu9Nh0j/EmJiU8jfkePWi4BiqT3FVzLykaJCDGkov3FSThp7j1gIPXt/jHb/4R7Tahld9tfCER2mAiZfKoFIGtQS30wsVgjcCNnTh9BPlXzw84fpbP8E3vfRXmY7OkDueg3/By2HgkVVB54AFRedBGrCWQDe5oWu+CtCRxhSafcjPQKvoBc1KajOVgz9XQWjoqrdqSGbGedb9KkIigfgzhqTnfv/MS4qvqaeolHa2SFFMODs9pTc/j1lcWuL4+IjRcEBzbh1XhwFKsZv4C4u90SDL+Q2HfNB5wUaCeyqzNsXoJatzmPYa73vPB+nsXuM1/9qwPfCcOqFfw9gLI6+UN0wlwMIUwTeVV9ya8E+/+35e9rJ/T2XnsQL+KU+CRgJthUVBFoEeaBMWc1hMwZjgUic1NCfQHCrNM2j1oTkAW0UENj7k7ZHzn3zuigI2uqZWBatyQ6QrpDJLTQTJzxAyDanplPB+iic1lmZD6I8P6LS7LC4tcnx0iMnSFO893itibMyJzyT8hqqY1wvjrD7mimJgFtWTznzr8+DMBAdcE2wjoy4KHj3+Z5ZasDeFqXoKhYlCIUJpwFmwScBuLi4pr7xsecsbR3znd70BlzTBWrwKOteGDsiM+EvgO9DOYDkFa0NW11RCPha6Y5gbQucMOmdC+8zjyxiI6cWB+nP30kY7ID4EZEYEE4mfICEy9sEWWBUyEw2umpkyJpHgYqcRpOA0TP1KkgTnHMne3i5r65coSsfodJs0X0K8oBoapGewjlkPNNzAlAgHmtVyRQLRg2c0yxUJYsMsoKyR8BVf9nDumYa3YncTzoTo1hnBGDhz8KhL8KwT+KFfE97wV3diGaD5It6FYUemIehCVNILgu9AswFLWeC7V0hryKbQnCrNEXRG0OlDewKdxEcggF6g7gjZUHPuaEswvrMMWIwVBA0uKIqR4IpmEF1PwkQWDZ6R9cEwG/UUheNSe52DvQN834X+gGazxXg8wjloNbt4FxVcxGQGj0fOM6IzFxQRxMfudZVzUFYgfjDQgUMJIgl+VPKIxz+ahz3z0dzb9zQyEwIso5Qa20wc9FX4ksuw/inPV7/a88mnW5q9u5h6dz72BxTtAMsxXuxB1oTVHJpJSKLlFTSm0JoorTG0RkEFtU6V7ihhIanirLyARVVfE0jlMVIjGs4lQiOFGoPBiMeqiwZWsXiSWAFLxJNoMouIYkrCkBjCyklgWg/I8xxEGQ6GmN78PMPBkOl0TJL3qKv6fJ7IuW6fRY03gLBmxJ/ZuNmJnGdKzXlcICYDnfCi599OlRomGixZZaA2YYBPDVRNeMEl2HmT5yu+ET65YOl9EaTlJH6HO0dASxtkGfwipC243IC5RGmq0KqgNYG5MXSG0O5DdwDdM2gPhN4UGttneFvGFR1bpG6AQEoorgbPCI8RMOrCQShnJjElHQzzzD6EjKkVIZGACcoNIRo2CYPxCVnWoNvrMRj0SQ4PDlhaXqYsHZP+Llk2F1QQN8ximBVjYjCg3ADGmXEBc+4tqYY1E2yBwXsBGjzx+Y/h0AXb6a1GYycUJdCBZ6TCX/+M5z/8D4FngrkDbAcS4+hHhaAzzFBH8YvQGMFNDehZJfWhgTAroDUOKqc9jI+DYAOafWE5Vdg8wKcRKBXDHXxoOzIRFGbExfqAD69JaPWz4jBqEPVY0UBo1ZAripWSZFY3kGALmuopnWOle4WToxNGY2VtbY1EjKGqKryHLE0v6KkXheQZvSWWJuUGj+hGZqjKue48Xz3W4IcFVy8t8Ygn38b9U7icQ2GCn13U0FmAjVP45Zd73vURQV5gkMse3wU7D4n13BgNCh7NlWYXbnHKSgLWC6ZSbBmY0h5CaxgJP4JuH9pDpeEtN+1vwd4e2m0gO6eIhOjWimJ8jYgJEHlTY7AYHwuLYs7rB0H3B8ZYG4ifRo/JEjKvqcp5pjSLRZxaS2yS4lwVxnMuLS2xv7eHiqWzukxdj2OoKzckrAJM5KIQc5GnlmiAZwOSVA1iEnDBlBlJoPTcdts83XYDVyutTuBZrdDrgL8ffuLblI/uGezzwS8Hw8qckjTDaLEbq0IKXE5grQFLqdByiou9a9lEIwOU9kDoDqAzCNjN5ExYWlAW/u4jsCHMzwejZiLA1WqMenUW+c4McmC8JUh8aJWtMZKQSIDXhwJkIHxI1t2QukZjPcByPNxjPr9E1uiyvbWFOT4K03Lb7TbF8IAkISReYmCiftYXHYwuKOrlCxCEeoPuF7HoLIg3CcZYqA2rl9bwc5aOVeYikvZSE0YfFb73X8JHBxb7tKDTWQ1uJV2wOaSiNwyKC5ObbrZwu4VVVXql0Ixqpn2mdM6Ubl/oDpTuUOmOod335Knljq0t7CcehDV4we3hXk3sO5i5pAYfPBwF0RrRKtgBCFLvfWAGwSak4knR8yNAUiIjdLYClNo7FjtrjMZDTk9OWF1bw9RVjTESAKNahRyQOQdXxuL7jajlmXt20UJxgYQNaYrAkZhBjzHB4uUVJgKLiWKcsJEJ93wMvvd7lL2ewT5W8fPAGsgCSDdkM00KxtcXN6RhFs66UTaAxQpaI6XZh/YZtPpCK6qb7ljojITOSJGJ5fZWyfKffpB6zkD/kJc+MaGVWCrnMOJDuDybR6H1bPRLJH5MVRBXgjoMFcbXGBdWihUlDc02JLMkHnL+3DslNRmoUtc1xljM6voax0fHDAcDGt0N6tJHkLnEmnCcPngeeCkiNzBCJKab5BwQK2LOz2dmaf1KFwIInKsJfPiTnu/+cegvC+YWxc0DK/ED80H6acZZHU4icYrQS4BnDcd6HfR8dqI0TpTmidI90xBwDYIdmJsoSR8etiE84o//CXetj1lOqfsFD+8d8n3PNexXQkKFNQ6kREyNMQ7RMOROxYEpgClGKywVIhVWaqytsCbMIhJxwZ5EQ5yKkKCkArkxNPKE3f512q0OS8vL7O5uk0zGY5qtFrVTXDWKfrbE/4qKhCFIM/N6XqOM8q9yjpRQzBe8D6HaBsrKYpMcuDWHOw/gVb8gDOfALIR0g8zUzkIkfiO0xfohuMrfMMcy6O159SyV0D9WsolBRpAVSnMSAq32FOYKpXEKV65aHvHBu6k+skt6tY2mU6Qs8cOUn3mZZ6dl+cO/KOlgmLcJKgUeh8fjUZyWUbk08Thqzch8hkcoSSijQlpqQmKC2glR8Ez9QBPFeUcr61IVJY6K3lyP5PT0lPVLIRKeDK6TNdYxRnAyS7xp9H78xfzT89hALjweMTF1PYOH25ARdRUkCcsLbVY01Hf/3/+inGBINhRnFVmWMM4k5nNohioWU9AT4nffOJ7M0bOGxilkJ4IWip2E4Ks1gXYBvSm0BnBlw3L7hzYp//gh2p0u2ANoOWQ8hptuIrfb/M/vV57zFZd4/X8/4/r9jrMqrFqLIfGOTAxNSciNJzNT2sbTtJ52VrOSDtm4cohWS+z982XarUh4hcxIkH4RUu+pi5rFxgJHgyOqcRnAuSurq2GcOsLi/GXK/QqvitgLh1LPtX3M+5xXwTh/Pfj90RWVJA6LE1QSbCNloddgSeAV/wvuekBIroa+MnrAYiC+9IB2SB8JgQFmELOV515QEIRmoTT3ITmNM2imSrMMkt8tYW6sbKwmXP7QEZM/2WKp2cYkp/jUBXyQOPj0NvrMBv6ua3z7xjbf/tMld++f0fc5MtdBGg0kbWIbTWyWkeQpqW2QJUIjczRaQrPdJFlcR8l4x3dWfOIPLOtzIQGYKKQmMKRhLI0842i8T7vdwyTC4cEByXlTSYShhya3kHyaqf5zG4u/KEVGPz8w4kINiTERPxpBpj7B2pRLqykfOYU/+qhgL8dJNC1C/XZBkQWBrkLGeQmUE6irmB2dfXF0e5OJ0jqCrB/dvBLyEjpl0PuriwkrHxoxfPs+a42cXCp8opCDJA7tgNw/gkcFpe3uOUOSEY+YG0fDUkCjhq4NRYU0gTyDrAVZJ/Si5quQ3kRVt0ib11i8PKLmjtBALglWiDkiJUNQE4LU4LKHYlCyu7PDxuXLlKVjfLpF3lzHJgYxGlAb/kLd6PlMg1kfncQ4gBua9G5MRVhwBZ1Gh1vWO3z13ym0BWlGQe6BLGrQ/XN6PrTclyBTgSOlTqHys+J/zC+jJENPZwj5KKSQTQmtGrpTWJpPWPh0xeAdfZbzlK6UqDVhdeWBEJIKflogn/dwxSJjCzalHjVChrDMkaoOk2FaKdJqQrOFNtqI6yB1B61bkFQkrbspTg55/9v/FRkj8ClGfEBKmNBKlVShfXe5vcbB/gFu6FhfXydZjDtUKEK3u0p9GLwge6MK0hsGYMMNS2JWtDM3eEJy7jKKBKjDxkbOJ/OUDx4JcklxLlqmuWh45wUa0bDMRl2eAWfgelCpjRekwQPDcrLbp7sB+SjWcWulWUE7E1rbnv7bSpbThLnEIiJoy4cRiJkGc4UGQXhgFCo3mUWKAOCSxEBqIBHUCpIGpa5SI1IiOo1CWOFrkGafh/7+iWzeY7mpMcK7HsY4EklIDeSJ0rZCkiWcFSe0Wx1UPKenp5hWq0VZFpRFiUlbeOfC7NNULmKB80cJM/8xQdcbG6seJmY/TWRKtA7GQKHcstbiD/ZSylyxSzGLuRwCLl0E31U0j+kkBzIGOQmRbWWgdNGXMHm0L00+/fl92iNoT5RsrGQjyCbQNkL1LqVRJvRyQwNBm0DbI40QFakEtSliAubwvgLxGTTTEMoaAzasCLExsJzlwnwVlqifjeI5hiTlf/3yzag7DVMB8LESJqReSb3SMYJNYTAdkFhLs9lkMhlj9na2YyTcYXx6nawZqp5Jotgs1mXOC/Qx72nCWGPi+N1AdHvOgBlEdQZvcfMNPj6OMJGVEGyxGpnQE0zM4aNhVLz2gUFsBBFwmoTNE3SW6J3nw5/6PI1qypyzJFMlHSlZImR3Q/qQMNdJaGmCyUBbIE2BXAPB0yA4GnNVIgrbDqocsSmSGWSWRzCCMXKeB7qoBjq0dNhLjg+++fH88996VtIwQyNVTxZrwSlKU8AaTzmt2OhdYTgccHR4wPr6JUzeaFGWJc7XJFknmAYbxv7a5g0paEOkklzYBAklSRGLWBMgKSRBgjABJYFht5WxlYUAS1dBV4BlDR5QFlLTqgqFwDgQX4vYd2zAa9i5AslxmpLmPe7Z2uK9n/wMN+eGZAxpbZBSkQ8rrQy6IuQJ+KZCS6GhaAYSEvdhdcbfo5kJ9zFQGKfgmygNMDkkeUQMzppTDNiQejOLU073bud3fnCdJSlJjSXHkouN6GhoWKGVCkkm2MxS1AVplpLlDabTKWZ+YZ5hf8BkPCZrLWB8GAmDE5LmrM4Si+9x6PQFTshcpCBm6edZOsJE3Khazhbb9LvAkiJLYOaD5NOIMCIhQOKnoJO4J0AYx4lPwPn0nAFoCqaBaoc/eP97WDJgRoIpIT02dAbQzYVOAtJSaCuSKWQubAJgoxpNEiRPIBNklj/IFaxDCwdVAnUCtQGfxDE4BtIENRne1sjiBr/96ocz2ZuylOZYZ2lIQmZSUjHkInQMdFLFtIWkm3A4OKDRaDI3N8fx0RHm4OCA3vw8rXaH4ekuS/OWtJHgSyVtMQNRcoMDdG4Lggoy5zD1wIeLOnCoKScUSy2KuYu6rcY0A1m8dDZltwCZRuLbqHUy8D5DaIcLJMf5hCRf4713f56/v+fTPKZp6R87WmdKx0Ivg6wB2gyelaY+lKyMhNsyoEYvEPYW1CokLlTm8hnDZih8E9RWJmAyvELysCZvfu3T+Mc/VW7LEqzPadmcnJQGlqYYcqvBi00E1/FMZcLa/DrD0ZDj46OQjEND57a1BlfDSktotoL/3Wwq0pjZ16j3bfD1Z+PXA2YvGi2xIGn4hSKopKSaYzsdqjYkC9BNgzttZxGI1zBxdxrTLXFamViQXFALWjcQuiAdQimsiUgDm67wQ3/zRibugEs+5bj0LFropOBbijYVyUAyibpf4goAMRqIHkfpiHWxrmjjEfMIuQ0eUZrgaeLEk9yR8a7/+kze8NPw8MxgtUlLGrRp0rQpuU1oGkPTCL00FOV9T9F5wao9nyNijMGsrK5yfHTEaNint7zOXDrlUntKUQhZJphWEOiIYDon7oW3M/N+JGS+Z94QCZgUFUMjg9QrD8uC/e1GvokClYSJu7O500SJTM89z1jOaGKkiaGFIdxUmnbZH9Z8y3t+n1vWhyzlCeM8jjFuKpK5GI4GNzJ2VnHe5pYqmuhF94UNgaAmUVUlBjKDWoOrBdubkjyiw9t+83m87rvmudVCToOGZrTIaZqMpmQ0SchEaavS7YbJAem6pTnfYHtri3YngHN3d7Yxg/6ATqdD3mhwenJKYhOe9QiLnkUyRnw9YqLRjWrGGMTaoOuRGPnGYozJwKQYtdRpSufOEVessGxgzQV4i6svNnc434gHLrJXGWiuwThKYADaAFoYaWOkg/qMdmONzxwM+dp3/xeGzeusPCcJK3RU48RRJw617hxhRyJIGlWOiT1jKfjUoIlBTWCaN4LXBHVg5mqSx9YcJbfyy698Ia///i63J46ubdPSNnOmTdM0aJqchmQ0MbQsLArYBUWahuL2KQUFC3OLTIspg8GA3vwCZjDo02y1aTYanPb7QMpXPykF7yn60JoLdkCbcdcHG7yAc0NrE7BJAGjZLBwmGEx1CazO8cAfH3Ll9/ZYTQwuM5zmBieCq8CVGgY/uVhZSKLdaYF0BF0WxOQILYw0sTQx2sFoD8sc+Jz55ir3HVd8/V+9kR/d/Dvue84A86gGSXee1KTBVVBF1aExw+nx1Di8Kn4GeE1rTFZg8hK7OMU+YoR5PBw1L/Env/dlfM+zv5S/+72cR2RCV7o0tEnXtumYNh3boCU5TUloJkIvhW4GuhEYPn7GiIqSTq9DWRRMJmM6nQ7inNPjoyOssczNL1BPTtkuap7+S8sMarjySLjnUyAD4ITQ6lLNpiCGjmyJA7bDmLIqNj871JWI1GhRkO8PWXxih5MXLjL/glXWHtPiZCVlrGHPt+kkpJ4pQQcgwyCl5s4zsp/8AFQVNrEY7wJQKkJFoEK0xIriXMVxdcrqco8XPPIqL0xu5kndNjd1KnrNPsYcQXYC2SnMDaA3DLXK3gS6E+iW1G1l1IDd3UXu/vQaH75zg8+9a53DvZSrwEqekfsWbZq0TIOObTInDbrSoGNyuomhl8KGhdYVwT9BkY8pvMdQdKac7J6wtBL2tDw6PESmxVQnoxEgtNptJpMpnVbBS962yJ++y/LY53i29oXje0OEqv2L6RsyG7xdufNhfGH+cxXmv/nIEK1QX8LxCIYVDQsrdyQsXW7iVxocGDhwHj9SmDhkpJhRmDmdXBthXdhwJzROaIR7zRrraoQyJsACRmdUjuj7ESXKXMtyc6PBSiNjMRXauSNNKkyqmFQRUyOJ4o1SloZR2eJ0knK81UKHTRoYNjAsZQkJOQ3N6dKkLQ3aJqctOV3boGtz2tbSTeBSBvMV+FcA/wzuCTX+Pyl+6BhOhrTbHYwxjEYjZHPzum5sXMY5z/bWJsur67SaOX/+mSNe/MuLLF2CO54I//R/4s5+wzA9Ras6jpyfzYWOq6J2iKsv5oDiwJXgS8SE2XC+rGLb/PQclXaRxgvoYzFhY80kz7EmI1Ebmh1meLtZV4sqQomROqqYCiMOi8drSeVKpt5Re3duZmyM1IPzHJrqchKapAQll4YGmyyhIzmJz8g0pUVKS3LaNOmYjLbJwiogoZtYOgms5LDkQJ8G/pmK/R2BvzTsXN2hVTboLS1weHiAqwMyTiaTiRZFASh53qAsS+q6pjff4Ov/qMk7/wIe+xzFW/js+2O3zBB0HMfK1/oFDAiro45zZOJq0JlaquJ4stkQvirODp0VwCPq2AcMpvFC4i2JZiQksSkutAiJugAlQcPMB3UodVBLqojUcXXEDcpwpDNYYexyTLERWBvGzTfCt5CqJdeMjJSMhBYZDUloSUpbUlpktKRByyS0bMKcNXSNstKA+TTkt/RHBPPbUD25YPi6Kc1JAx83bWg0mwgwLQqSPM8ZDAYYI8z1GhRFwXg8ZXG+x3/86pr3/qPw+Q8nPPOrlZNHKjufltBx7k1oup2VH2dd53aWmItENoJ4G+Hr5jyPEibmpgE/4WctQRLdcnMOgrWakEogSmJCLxaqJBG5kAoXA/2IQCoTqmaqgQmqYSxaEmu1ggbksiYRTAupmnMmNOK8h8wk5N6QS0LLpLQ0pSUJLZPRkpSWCB0jLKbKcgatHGgr+mOC/K2HU8vkJ0qm/THzzTlKXzIej2l3OmEfsbNTZGd7W1fX1nDOsb+3x/LKMnneYGtzi8trbX7lI/P86K8pvSXl8c+GT94pnD0YPDqdxnHGFRezabyeD+9mNqT1fCirC6kO5zGzvQBmYwK8nsO8xdvYg2tINKiIdDb4IraAGucj8XwsIIWVMAPRStyELExed8F2SABKpedF89DJco7jJ2zGk2PJEHJjacbnDbE0sLSMpW0SWgY6CSwmsNIM8Ei1iv6EgSOP+UXL2S8eY16e0a077B7s0Gg0mF9Y4PAg7CGztraGnJ6eapqE/dyqKuwbKQhFVWMxdOeFb3pTgz96U8LiFeXq45UH7xHOrsX0bBmG2UmhgRk+dEBLPMJeMWFMsfFhyq7xLkzgjY1wJjIgiRhL403sOA9MyGOvlWDIo+42PmIwZ/EhGueyBWk3PtgVK2HFGWLztYSwLonQ8RlqLYnw8lRNYIBKSCkYQ0MlMMFAU4R2AnMprFiYb4FpgK4p+m3APph/D/VP1IxeM8GOEowR6qo+zyCEqlgYPBvGVu7uYoxhVh8eDodcuXqV8XjC7uY+//MbNugP4c/f7plMDJfuCJrmZCs2bAhInBtHRZi346MhldB1GXR8MI4CGBPgLkZ9mCwIoRud0F1u1ZASCJ+ZoCLkBklNYol0Fl/ZWUZE0sBsG3E5sUfXaNiyKjOB8DaCaM8ZIBJSyCLkamiIkEsorDcUGqq0JKQ5VlJYyiHPQdugj1X0aQKfUMxbDcV3T+h/34AV1uhXZ5yO+ly5cpViWrC3u8Oly5exiWVnewfZ39/XhYV5nPOcnpzQm58nTVOOjo5oNRvkzTbD/glZV/jBNy7x+jcBLWX51uDoFH1gErKYfjLbTyYM+pMq4DVNredSblxEn83GAzg9n8FgCQgM4yGPxJaoJrI4OMNKPJ+hkmPdKInYzoj5DQxBsS6CZaP3lBIK5QbBxC1bspiNyCR0OuZeaRqhYYRMoSFKJjBv4VIqzCeKdEAvAY+Oj38P5kOe8c9OMa9JkRpO9k/oLfTIspyjo0PyPKfT6dDvn+G9Z35+gUQE3GxXCwl7RzrnMMbgVUPHDJbizPG731Lw+MvCj70+5fCjQrqmdOYg6YJYxQl4kTBAz13MXbUI1sTVYgizGCLk24qcS6LMxgGIxlb/AAxLkFDcjjhLG/W2FT3v3U1mlWgzm2ISGJbagMC2BlKn5/DxRAKcII3En2VAUoVcDA2UhldyoGNg1QYXM2srXAK9QuhLewDkjcCtwuTNJcMXjegV86EjMzWRnjXGhIKOd/68cOtmYyt3d3ZIk4SllRVOTk4YjUZcuXKF6XTK/t4+G5c3EGPZ2bzGlZtW+ORui5/872P+6n1N6qEgbaXbg3YCaa0Bz1PEoso05HxMHbCl1oX6rdWwW9Fs0MVsEMaNqyGN0L7gpcyMJ3EqyYVEJxok2USQWBrBxEGdycXnZode2I9ZiSAN9XoaGh7bFtomlIuXcuh0QZfAt+IGFgcC9yqmJ/AK4eA7DrC3WxZZZH93D7HCysoqp6enDAZ9rl69iaIomIEgkiSOrTw9PdUsS0GVoizJsxxEmE4mpFlKYhPKsgSBNM2YTCs6mSdrp7zvLuGNf6n88f9pcXYtdtclkCRBZ6YKqQ9L3dQa4NpxW9vMSxj74gPBEq+YOo4BUImSHjdYjrW1xMeMcjTAmYS/kWpogAiDMTgfrvEFageNfbuz18KqSm0w/u04i7RhlFYitFLIMiUzoZHbVwGjlBWh15ibPeabLeN/NWL66IImTfRMKX1B3myCD9ub540GEjfGttZgrA1NMEqg73g0otMJbujg8IjGaoNGo8nx8TFtY+h0uozGI1ztmJvrUZYF+6djLjU2eM4jxzzn4af83CstH/hYyvvvnHL3tZztBwyTPSiH4Kog5Yawy55FSdJA8FkhKo0eTCYXrf+z9zIB60KXSS6CdRqMskDiguTnIsGAq9KwsYla9Vynp3KeYA1qzhIGaMR5vamJqyoysFJlGibzhLJAqphlR/IMwT7OMHzCEPcMR2+lR1mUFNtTFtcWmWQTRkcjunNzeO8ZHh2RN5s0m03OTk/J85yFxS5H40PqqqY7t4547/XgYB9rLItLy/TPTplOJ6ytX2IymXB8fBT2PEwSdra3z6Hsu7s7JGmTXm+eenqMsSV5cxUIcxBq2aCYeiaTPfJsmcTkTCe7JLZFns4xLQ4QhWa+QlUNqeshrcY66muKYp8sXSSxDYpyh8T0yJMW0+oAUUMzX6KozqjrMd3GJZyfMiqP6aZrWCucFXu07Bx50qZfHJDalE4yz6DqU/kJi801inrKWXXEUrqOGOGg2KGXLtK0TQ6n+6Q2o5fNc+wO8ZlneWmVge0z8EPWzCo1jqPrBywuLtNoNdjd26HZaNGb73FwcIDMNkc9PWM4GrK+sUFZFBweHLK6toq1lv29PWQ8HmtZlghhSdS1w9U1jWYT7xzTYkqj0cQYCfsgphk2TZmOxxhryLKc6TSkL5rNNt6XqCtodzqgnrKakKYNEKGuCmySIGJxrgIBYzO8q3GuIs2aqCrFdEyWN7A2YTodkSQpxqZU1RQRg01S6rpCvSfLG9Supq5rsjSLs0srrAmfK8opAmRpg6qu8N6RpTmoMq2mNLImCEyLsMdjYhKm5QQjhjTNmRYTxEOeNKmmFXVZ0WqHbb+m5ZQ8yxBrKaZTrLWkSUJRht6zPMuonaOqKpqNBh6lmEzJ8xxjDEVRhAaNubk5Wu0Wu7u7pEnC/MIC+3v7FGXJ4uISo+GQk5MTFhYWqeuaw/195hcWyPOcvd0dms2c5eVFTo72wqT0+WX29k84PB7SbC1xcjple/uYRnOBYmq4fm0PYzpY02Xr2h7VVGg1F9nbPuL0aEi3s8yoX7G3fUSntUQxhp3r+7SyBTLTYffaAVJndBoLHGweUfRr5poLnB0MOdk9Y64xj58I+9f2mcsWaGiLvWv7ZNqgly9wuHVI0S9ZaC3R3+8z2B+w0FqiHrhwTbpAQ5vsX9ujY7v0mgvsb+8iRlhYWeTw9IjhaMDCwgLTouBwf4/5+fkg1ft7dLtdOp0Ou3shvlpYWODo+IjpZMLC4iLj8YiT6PJLWRQ6GA4xxtBqtSimBUVZ0O3O4VzNaDSk251DROifndFqtUizjLOzM/IsI280mE4nuNrR6XaYTgum0ym9+Xm8c4yGQ9qdDkaEwXBAo9Eky1JGo7BVSqvdpphOKcqCubk5XO3C4JB2G2sTzs5OaLXapFnGoN/HWkur02EyHuPqmk63GwscU+Z6c6gqw+GQRp6TZlnYJCGxNPIG4/EkbNE+16WqSsajEd3uHAoMBv3z7xwNR1hraDSbjEcjFKXT7lAUJWVV0O12qeua0XBEp9vFWkO/H9pP8zxnPBoB0Gq3wjVFQXdujrquGQ4HzM31wj7GgwHGWBs65V2NtTaMinOOJLGIgKtDZ7e1Fh+76G28xnlPkiR4r3j1WBvgG865MC3WCHXcFc/GznDvHdYmqHqcd1gboINVDNWttZRVGQDuNsQiXjVeE9p8bAzpq6rCWhs8i/g9SZLg6hrvPWma4J2jqmpsEsY01XUVRvIgVDf8Nlc7vNf4e8IeNUmShO93PlwvobPFGos1FufCrFFrk7BNSUwvzGhjbfxO57DGhtjKhaS4sXG3j92dba3rWouy1K2tLZ2Mx+q91+2tLT09PdG447bu7++pqurp6Ylub2+r914nk4lubW3qtCjUOafbW1va7/c1jsTXg4MDVVU9OT7S3Z0dVVUdDYe6tbmpVVVpVVW6tbmpw9FQvfe6u7urR4eH6r3X4+Nj3d3dVVXV/tmZbm+F+/y/r9nZ3tbTk3CfBwf7urcb7nMwHOj29pY653Qymer169e1mN3n5qaenZ6Ga/b39eBg/4bv2VTvnE6nE93e2tSyLNW5OnzncKiqqru7O3p0dBR+20mgh6rqeDw6/21lWermtes6Go/DNTsX93l0eKh7e3vqvdf/H8tBFI/TvSUnAAAAAElFTkSuQmCC" alt="云来AI"><span style="display:inline-flex;align-items:baseline;gap:10px"><span class="yl-brandtext">云来AI</span><svg class="yl-brandwave" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><defs><linearGradient id="ylGoldGrad" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe9a8"/><stop offset=".45" stop-color="#fbbf24"/><stop offset="1" stop-color="#d97706"/></linearGradient><linearGradient id="ylDeepGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#155e8a"/><stop offset=".55" stop-color="#243b8f"/><stop offset="1" stop-color="#6d28d9"/></linearGradient></defs><rect x="8" y="2" width="8" height="11.5" rx="4" fill="url(#ylGoldGrad)" stroke="var(--yl-ink,#eef4ff)" stroke-width="1.8"/><g fill="none" stroke="var(--yl-ink,#eef4ff)" stroke-width="1.8" stroke-linecap="round"><path d="M5.5 7 V10.5 Q5.5 15 12 15 Q18.5 15 18.5 10.5 V7"/><path d="M12 15 V18.5"/><path d="M8.7 18.5 H15.3"/></g><g fill="none" stroke="#123049" stroke-width="2.2" stroke-linecap="round"><path d="M9.2 5.2 H12.7" class="yl-grille"/><path d="M9.2 7.8 H12.7" class="yl-grille"/><path d="M9.2 10.4 H12.7" class="yl-grille"/></g></svg><span class="yl-brandtail">音频工作台</span></span></span></a><span class="yl-nav"><span class="yl-navcur" title="当前所在工作台">AI音频工作台</span><a href="https://video.yunlaiai.com" target="_blank" rel="noopener" title="打开AI视频工作台">AI视频工作台</a><a href="https://dub.yunlaiai.com/" target="_blank" rel="noopener" title="打开AI配音工作台">AI配音工作台</a></span></div>""")  # YUNLAI_BRAND header
    status = gr.Markdown(server_status())
    with gr.Accordion("🔌 API 调用说明：第三方应用如何用本 server 生成语音", open=False):
        gr.Markdown(_api_usage_md())
    with gr.Accordion("🔐 下载设置：HF token / 代理（可选，不保存）", open=False):
        with gr.Row():
            hf_token = gr.Textbox(
                label="HF token (下载受限模型时用)", type="password",
                placeholder="hf_xxx —— 或先在命令行运行 huggingface-cli login")
            proxy = gr.Textbox(
                label="代理 (仅下载模型时使用)",
                placeholder="http://127.0.0.1:7890")

    # ---- 每个标签页共用的“模型管理”卡片、接线与高级参数渲染 ----
    def _model_manager_block(task_label, tasks):
        """标准“模型管理”卡片：模型下拉 + 加载/刷新/下载/进度按钮 + 状态区。
        返回组件 dict；接线见 _wire_model_manager（刷新按钮统一接在文件末尾）。"""
        choices = choices_for_tasks(tasks)
        with gr.Group():
            gr.Markdown("#### 🧩 模型管理")
            model = gr.Dropdown(
                label=f"模型列表 (task={task_label})", choices=choices,
                value=(choices[0][1] if choices else None))
            with gr.Row(elem_classes="mm-btn-row"):
                load_btn = gr.Button("📥 加载模型", variant="primary",
                                     size="lg", min_width=100)
                refresh_btn = gr.Button("🔄 刷新列表", variant="primary",
                                        size="lg", min_width=100)
                dl_btn = gr.Button("⬇️ 下载模型", variant="primary",
                                   size="lg", min_width=100)
                dl_stat_btn = gr.Button("📊 下载进度", variant="primary",
                                        size="lg", min_width=100)
                unload_btn = gr.Button("🧹 释放显存", variant="secondary",
                                       size="lg", min_width=100)
            load_status = gr.Markdown("")
            dl_status = gr.Markdown("")
            timer = gr.Timer(3, active=False)
        return {"model": model, "load_btn": load_btn, "refresh_btn": refresh_btn,
                "dl_btn": dl_btn, "dl_stat_btn": dl_stat_btn, "unload_btn": unload_btn,
                "load_status": load_status, "dl_status": dl_status, "timer": timer}

    def _wire_model_manager(mm, tasks, hint):
        mm["load_btn"].click(_make_load_handler(tasks), mm["model"],
                             [mm["load_status"], status])
        mm["dl_btn"].click(download_start, [mm["model"], hf_token, proxy],
                           [mm["dl_status"], mm["timer"]])
        mm["timer"].tick(download_status_tick, mm["model"],
                         [mm["dl_status"], mm["timer"]])
        mm["dl_stat_btn"].click(download_status, mm["model"], mm["dl_status"])
        mm["unload_btn"].click(unload_model, None, [mm["load_status"], status])
        mm["model"].change(model_hint_for, mm["model"], hint)

    def _render_param_controls(model_comp, state_comp, skip=(), prefill_comp=None):
        """“高级参数”折叠区内容：按所选模型的 family 动态生成控件（gr.render），
        控件值写进共享 state（只有用户改过的项会随请求发送）。
        传入 prefill_comp（gr.State dict）时它也是渲染输入：程序化回填
        （如『🔍 分析源音频』）写 prefill 触发重渲染，把值显示在控件上。"""
        inputs = [model_comp] if prefill_comp is None else [model_comp, prefill_comp]

        @gr.render(inputs=inputs)
        def _render(model_id, prefill=None):
            specs = [p for p in params_for(model_id) if p.get("name") not in skip]
            if not specs:
                gr.Markdown("*该模型无可调高级参数。*")
                return
            prefill = prefill or {}
            for p in specs:
                if p["name"] in prefill:
                    p = {**p, "default": prefill[p["name"]]}
                comp = _make_param_component(p)
                comp.change(_adv_updater(p["name"]), [state_comp, comp], state_comp)

    # ---------------- TTS / 声音克隆 ----------------
    with gr.Tab("🗣️ TTS / 声音克隆"):
        with gr.Row(equal_height=False):
            # 左列：模型管理 + 参考音频（声音克隆）
            with gr.Column(scale=1):
                tts_mm = _model_manager_block("tts", TTS_TASKS)
                tts_model = tts_mm["model"]

                with gr.Group():
                    gr.Markdown("#### 🎧 参考音频（声音克隆）")
                    with gr.Row(elem_classes="voice-refresh-row"):
                        tts_builtin = gr.Dropdown(
                            label="内置参考音色",
                            choices=["(none)"] + builtin_voices(),
                            value="(none)", scale=8)
                        tts_voice_refresh = gr.Button(
                            "🔄", scale=1, min_width=48)
                    tts_upload = gr.Audio(
                        label="上传/录制参考音色（可选）", type="filepath",
                        elem_classes="audio-default")
                    tts_ref_text = gr.Textbox(
                        label="参考文本", lines=2,
                        placeholder="参考音频里说的原话（克隆时建议填写，越准越好）")

                # 切换模型后的提示，显示在参考音频整块下面
                tts_hint = gr.Markdown(model_hint_for(tts_model.value))

            # 右列：合成设置 + 合成内容 + 生成 + 输出
            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("#### ⚙️ 合成设置")
                    with gr.Row():
                        tts_seed = gr.Number(label="seed（-1=随机）", value=1234, precision=0)
                        tts_maxtok = gr.Number(label="max_tokens", value=1200, precision=0)
                    tts_adv_state = gr.State({})
                    with gr.Accordion("高级参数（只发送改动过的项）", open=False):
                        _render_param_controls(tts_model, tts_adv_state)

                    with gr.Accordion("其它参数（JSON，覆盖控件）", open=False):
                        tts_adv = gr.Textbox(
                            label="",
                            placeholder='{"num_inference_steps": 10, "voice_samples": "D:/a.wav,D:/b.wav"}',
                            lines=3)

                with gr.Group():
                    gr.Markdown("#### ✍️ 合成内容")
                    tts_text = gr.Textbox(
                        label="要合成的文字", lines=5,
                        value="Hello, this is audio dot cpp speaking from a web page.")
                    tts_lang = gr.Dropdown(
                        label="语言（留空=模型默认）", choices=LANGS,
                        value="chinese")

                tts_btn = gr.Button("🎵 生成语音", variant="primary", size="lg")
                with gr.Group():
                    gr.Markdown("#### 🔊 输出音频")
                    tts_out = gr.Audio(label="输出音频", type="filepath",
                                       elem_classes="audio-default")
                    tts_msg = gr.Markdown("")

        _wire_model_manager(tts_mm, TTS_TASKS, tts_hint)
        tts_model.change(lambda: {}, None, tts_adv_state)  # reset knobs on model switch
        tts_builtin.change(on_builtin_voice_change, tts_builtin,
                           [tts_upload, tts_ref_text])
        tts_voice_refresh.click(refresh_builtin_voices, tts_builtin,
                                [tts_builtin, tts_upload, tts_ref_text])
        # Clear the previous run's audio + status message the moment 生成 is
        # clicked, so a prior message doesn't linger next to the new run's
        # progress indicator (outputs otherwise update only when do_tts returns).
        tts_btn.click(lambda: (None, ""), None, [tts_out, tts_msg]).then(
            do_tts,
            [tts_model, tts_text, tts_lang, tts_upload, tts_builtin,
             tts_ref_text, tts_seed, tts_maxtok, tts_adv_state, tts_adv],
            [tts_out, tts_msg])

    # ---------------- ASR / 音频转写 ----------------
    with gr.Tab("📝 ASR / 音频转写"):
        with gr.Row(equal_height=False):
            # 左列：模型管理 + 音频输入
            with gr.Column(scale=1):
                asr_mm = _model_manager_block("asr", ASR_TASKS)
                asr_model = asr_mm["model"]

                with gr.Group():
                    gr.Markdown("#### 🎤 音频输入")
                    asr_audio = gr.Audio(label="上传/录制音频", type="filepath",
                                         elem_classes="audio-default")
                with gr.Accordion("🎛 转写选项（可选）", open=False):
                    asr_language = gr.Dropdown(
                        label="强制语种（留空=自动检测）",
                        choices=[("自动检测", "")] +
                                [(f"{zh} {en}", en) for zh, en in QWEN3_ASR_LANGUAGES],
                        value="")
                    asr_context = gr.Textbox(
                        label="上下文提示", lines=2,
                        placeholder="人名/术语等，帮助识别专有名词")
                    asr_dialogue = gr.Checkbox(
                        label="🗣 对话模式（≤120s，需 Sortformer）：输出带说话人和时间戳的对话稿")
                asr_hint = gr.Markdown(model_hint_for(asr_model.value))
                asr_btn = gr.Button("📝 开始转写", variant="primary", size="lg")

            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("#### 📄 识别结果")
                    asr_out = gr.Textbox(
                        label="转写文本", lines=10,
                        placeholder="转写结果将显示在这里……")
                    asr_msg = gr.Markdown("")

        _wire_model_manager(asr_mm, ASR_TASKS, asr_hint)
        asr_btn.click(lambda: ("", ""), None, [asr_out, asr_msg]).then(
            do_asr, [asr_model, asr_audio, asr_language, asr_context, asr_dialogue],
            [asr_out, asr_msg])

    # ---------------- 音乐 / 音效生成 ----------------
    with gr.Tab("🎵 音乐生成"):
        with gr.Row(equal_height=False):
            # 左列：模型管理 + 源音频（编辑类用法）
            with gr.Column(scale=1):
                gen_mm = _model_manager_block("gen", GEN_TASKS)
                gen_model = gen_mm["model"]

                with gr.Group():
                    gr.Markdown("#### 🎧 源音频（可选，仅编辑类用法）")
                    gen_audio = gr.Audio(
                        label="上传源音频（编辑类 route 用）",
                        type="filepath", elem_classes="audio-default")
                    gen_ana_btn = gr.Button(
                        "🔍 分析源音频（自动填入描述/歌词/BPM/调性）")
                    gen_ana_msg = gr.Markdown("")

                gen_hint = gr.Markdown(model_hint_for(gen_model.value))

            # 右列：生成设置 + 提示词/歌词 + 生成 + 输出
            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("#### ⚙️ 生成设置")
                    with gr.Row():
                        gen_duration = gr.Number(
                            label="时长(秒)，-1=自动", value=30, precision=1)
                        gen_seed = gr.Number(label="seed（-1=随机）", value=1234, precision=0)
                    gen_adv_state = gr.State({})
                    gen_prefill = gr.State({})   # 『🔍 分析源音频』回填 -> 触发控件重渲染
                    with gr.Accordion("高级参数（只发送改动过的项）", open=False):
                        _render_param_controls(gen_model, gen_adv_state,
                                               prefill_comp=gen_prefill)

                    with gr.Accordion("其它参数（JSON，覆盖控件）", open=False):
                        gen_adv = gr.Textbox(
                            label="",
                            placeholder='{"tags": "pop,bright,drums", "num_inference_steps": 8}',
                            lines=3)

                with gr.Group():
                    gr.Markdown("#### ✍️ 提示词与歌词")
                    gen_text = gr.Textbox(
                        label="提示词（英文效果最好）", lines=3,
                        value="uplifting pop with bright synths and driving drums")
                    gen_lyrics = gr.Textbox(
                        label="歌词（可选）", lines=4)

                gen_btn = gr.Button("🎵 生成音乐", variant="primary", size="lg")
                with gr.Group():
                    gr.Markdown("#### 🔊 输出音频")
                    gen_out = gr.Audio(label="输出音频", type="filepath",
                                       elem_classes="audio-default")
                    gen_msg = gr.Markdown("")

        _wire_model_manager(gen_mm, GEN_TASKS, gen_hint)
        gen_model.change(lambda: ({}, {}, ""), None,
                         [gen_adv_state, gen_prefill, gen_ana_msg])  # 切换模型清空旋钮+分析信息
        gen_ana_btn.click(lambda: "⏳ 分析中……（需先『📥 加载模型』）",
                          None, gen_ana_msg).then(
            do_music_analyze, [gen_model, gen_audio, gen_seed, gen_adv_state],
            [gen_adv_state, gen_prefill, gen_ana_msg])
        gen_btn.click(lambda: (None, ""), None, [gen_out, gen_msg]).then(
            do_music_gen,
            [gen_model, gen_text, gen_lyrics, gen_audio, gen_duration, gen_seed,
             gen_adv_state, gen_adv],
            [gen_out, gen_msg])

    # ---------------- 声音转换 (vc / svc / s2s) ----------------
    with gr.Tab("🎭 声音转换"):
        with gr.Row(equal_height=False):
            # 左列：模型管理 + 源音频 + 目标音色
            with gr.Column(scale=1):
                vc_mm = _model_manager_block("vc/svc/s2s", VC_TASKS)
                vc_model = vc_mm["model"]

                with gr.Group():
                    gr.Markdown("#### 🎙️ 源音频（要转换的内容）")
                    vc_source = gr.Audio(label="上传/录制源音频", type="filepath",
                                         elem_classes="audio-default")
                with gr.Group():
                    gr.Markdown("#### 🎧 目标音色（转换成谁的声音）")
                    with gr.Row(elem_classes="voice-refresh-row"):
                        vc_builtin = gr.Dropdown(
                            label="内置参考音色",
                            choices=["(none)"] + builtin_voices(),
                            value="(none)", scale=8)
                        vc_voice_refresh = gr.Button(
                            "🔄", scale=1, min_width=48)
                    vc_target = gr.Audio(label="上传/录制目标音色", type="filepath",
                                         elem_classes="audio-default")

                vc_hint = gr.Markdown(model_hint_for(vc_model.value))

            # 右列：转换设置 + 转换 + 输出
            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("#### ⚙️ 转换设置")
                    vc_seed = gr.Number(label="seed（-1=随机）", value=1234, precision=0)
                    vc_adv_state = gr.State({})
                    with gr.Accordion("高级参数（只发送改动过的项）", open=False):
                        _render_param_controls(vc_model, vc_adv_state)
                    with gr.Accordion("其它参数（JSON，覆盖控件）", open=False):
                        vc_adv = gr.Textbox(
                            label="",
                            placeholder='{"route": "style_converted_vc", "style_ref": "D:/style.wav", "target_text": "……"}',
                            lines=3)

                vc_btn = gr.Button("🎭 开始转换", variant="primary", size="lg")
                with gr.Group():
                    gr.Markdown("#### 🔊 输出音频")
                    vc_out = gr.Audio(label="输出音频", type="filepath",
                                      elem_classes="audio-default")
                    vc_msg = gr.Markdown("")

        _wire_model_manager(vc_mm, VC_TASKS, vc_hint)
        vc_model.change(lambda: {}, None, vc_adv_state)  # reset knobs on model switch
        vc_builtin.change(lambda n: on_builtin_voice_change(n)[0], vc_builtin, vc_target)
        vc_voice_refresh.click(lambda n: refresh_builtin_voices(n)[:2],
                               vc_builtin, [vc_builtin, vc_target])
        vc_btn.click(lambda: (None, ""), None, [vc_out, vc_msg]).then(
            do_vc,
            [vc_model, vc_source, vc_target, vc_builtin, vc_seed,
             vc_adv_state, vc_adv],
            [vc_out, vc_msg])

    # ---------------- 音源分离 (sep) ----------------
    with gr.Tab("🎚️ 音源分离"):
        with gr.Row(equal_height=False):
            # 左列：模型管理 + 输入音频
            with gr.Column(scale=1):
                sep_mm = _model_manager_block("sep", SEP_TASKS)
                sep_model = sep_mm["model"]

                with gr.Group():
                    gr.Markdown("#### 🎵 输入音频")
                    sep_audio = gr.Audio(label="上传要分离的歌曲/音频", type="filepath",
                                         elem_classes="audio-default")
                sep_hint = gr.Markdown(model_hint_for(sep_model.value))
                sep_btn = gr.Button("🎚️ 开始分离", variant="primary", size="lg")

            # 右列：分离结果（各分轨播放器 + 文件下载）
            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("#### 🔊 分离结果")
                    sep_stems = [gr.Audio(label=f"音轨 {i + 1}", type="filepath",
                                          visible=False, elem_classes="audio-default")
                                 for i in range(MAX_SEP_STEMS)]
                    sep_files = gr.File(label="全部音轨文件", file_count="multiple",
                                        interactive=False)
                    sep_msg = gr.Markdown("")

        _wire_model_manager(sep_mm, SEP_TASKS, sep_hint)
        sep_btn.click(
            lambda: (*(gr.update(value=None, visible=False)
                       for _ in range(MAX_SEP_STEMS)), None, ""),
            None, [*sep_stems, sep_files, sep_msg]).then(
            do_sep, [sep_model, sep_audio], [*sep_stems, sep_files, sep_msg])

    # ---------------- 音频分析 (vad / diar / align) ----------------
    with gr.Tab("🔎 音频分析"):
        with gr.Row(equal_height=False):
            # 左列：模型管理 + 音频输入（align 模型多出对齐文本/语言）
            with gr.Column(scale=1):
                ana_mm = _model_manager_block("vad/diar/align", ANALYZE_TASKS)
                ana_model = ana_mm["model"]

                with gr.Group():
                    gr.Markdown("#### 🎤 音频输入")
                    ana_audio = gr.Audio(label="上传/录制音频", type="filepath",
                                         elem_classes="audio-default")
                    gr.Markdown(
                        "*输入自动转 16 kHz 单声道。*",
                        elem_classes="hint-small")
                    _ana_is_align = bool(
                        ana_model.value and
                        (catalog_by_id(ana_model.value) or {}).get("task") == "align")
                    ana_text = gr.Textbox(
                        label="对齐文本（align 必填：音频原文）", lines=3,
                        visible=_ana_is_align)
                    ana_lang = gr.Textbox(
                        label="语言（可选，如 English / Chinese）",
                        visible=_ana_is_align)
                ana_hint = gr.Markdown(model_hint_for(ana_model.value))
                ana_btn = gr.Button("🔎 开始分析", variant="primary", size="lg")

            # 右列：分析结果
            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("#### 📄 分析结果")
                    ana_out = gr.Textbox(
                        label="结果（时间单位：秒）", lines=14,
                        placeholder="语音段 / 说话人 / 逐词时间戳将显示在这里……")
                    ana_json = gr.File(label="原始 JSON 结果", interactive=False)
                    ana_msg = gr.Markdown("")

        _wire_model_manager(ana_mm, ANALYZE_TASKS, ana_hint)
        ana_model.change(_align_fields_visibility, ana_model, [ana_text, ana_lang])
        ana_btn.click(lambda: ("", None, ""), None, [ana_out, ana_json, ana_msg]).then(
            do_analyze, [ana_model, ana_audio, ana_text, ana_lang],
            [ana_out, ana_json, ana_msg])

    # ---------------- 声音设计 (vdes) ----------------
    with gr.Tab("🎨 声音设计"):
        with gr.Row(equal_height=False):
            # 左列：模型管理 + 生成设置
            with gr.Column(scale=1):
                vdes_mm = _model_manager_block("vdes", VDES_TASKS)
                vdes_model = vdes_mm["model"]

                with gr.Group():
                    gr.Markdown("#### ⚙️ 生成设置")
                    with gr.Row():
                        vdes_seed = gr.Number(label="seed（-1=随机）", value=1234, precision=0)
                        vdes_maxtok = gr.Number(label="max_tokens", value=1200, precision=0)
                    vdes_adv_state = gr.State({})
                    with gr.Accordion("高级参数（只发送改动过的项）", open=False):
                        # instruct 有专用的『音色描述』输入框，这里不重复生成
                        _render_param_controls(vdes_model, vdes_adv_state, skip=("instruct",))
                    with gr.Accordion("其它参数（JSON，覆盖控件）", open=False):
                        vdes_adv = gr.Textbox(label="", placeholder='{"temperature": 0.9}',
                                              lines=3)
                vdes_hint = gr.Markdown(model_hint_for(vdes_model.value))

            # 右列：设计内容 + 生成 + 输出
            with gr.Column(scale=1):
                with gr.Group():
                    gr.Markdown("#### ✍️ 设计内容")
                    vdes_instruct = gr.Textbox(
                        label="音色描述（用文字描述想要的声音）", lines=3,
                        placeholder="例：低沉磁性的中年男声，语速偏慢，带播音腔")
                    vdes_text = gr.Textbox(
                        label="要合成的文字", lines=5,
                        value="你好，这是 audio.cpp 用文字描述设计出来的声音。")

                vdes_btn = gr.Button("🎨 生成语音", variant="primary", size="lg")
                with gr.Group():
                    gr.Markdown("#### 🔊 输出音频")
                    vdes_out = gr.Audio(label="输出音频", type="filepath",
                                        elem_classes="audio-default")
                    vdes_msg = gr.Markdown("")

        _wire_model_manager(vdes_mm, VDES_TASKS, vdes_hint)
        vdes_model.change(lambda: {}, None, vdes_adv_state)  # reset knobs on model switch
        vdes_btn.click(lambda: (None, ""), None, [vdes_out, vdes_msg]).then(
            do_vdes,
            [vdes_model, vdes_text, vdes_instruct, vdes_seed, vdes_maxtok,
             vdes_adv_state, vdes_adv],
            [vdes_out, vdes_msg])

    # 上传/录制后把文件换成短 ASCII 临时名，绕过 Gradio 在 Windows 上无法渲染
    # 含中文/超长文件名的声波图（见 _stage_upload）。只接输入类音频控件。
    for _in_audio in (tts_upload, asr_audio, gen_audio, vc_source, vc_target,
                      sep_audio, ana_audio):
        # upload: 长文件强制换成新的稳定临时文件，避免 X 清空后再次上传长音频时
        # 前端复用旧 preview 状态导致波形不渲染。
        _in_audio.upload(lambda p: _stage_upload(p, force_copy=True), _in_audio, _in_audio)
        # microphone: 录音完成不是 upload 事件，单独接 stop_recording。
        _in_audio.stop_recording(_stage_recording, _in_audio, _in_audio)
        # clear: 明确把后端值置空，避免长音频 preview fetch 被中断后控件状态残留。
        _in_audio.clear(lambda: None, None, _in_audio)

    # 顺序与 refresh()/TAB_SPECS 一致：各页下拉、状态行、各页提示。
    _refresh_outputs = [
        tts_model, asr_model, gen_model, vc_model, sep_model, ana_model, vdes_model,
        status,
        tts_hint, asr_hint, gen_hint, vc_hint, sep_hint, ana_hint, vdes_hint,
    ]
    for _mm in (tts_mm, asr_mm, gen_mm, vc_mm, sep_mm, ana_mm, vdes_mm):
        _mm["refresh_btn"].click(refresh, None, _refresh_outputs)
    # 顶部状态行在建界面时只求值一次，浏览器刷新会看到那份静态初值（即使模型
    # 已加载也显示“server 未运行”）。server 本身就是状态的唯一真相源
    # （/health + /v1/models），每次页面加载重新探测即可，无需额外状态文件。
    demo.load(server_status, None, status)
    # 音频播放器换源后把旧播放进度清回 0:00（见 _RESET_AUDIO_SEEK_JS 注释）。
    demo.load(None, None, None, js=_RESET_AUDIO_SEEK_JS)
    gr.HTML(
        '<div style="margin-top:18px;padding-top:12px;border-top:1px solid #1f2b3d;text-align:center">'
        '<span style="background:linear-gradient(96deg,#2ee6ff,#60a5fa 40%,#a78bfa 62%,#f472b6);'
        '-webkit-background-clip:text;background-clip:text;color:transparent;font-weight:800">云来AI工作室</span>'
        '<span style="color:#8296b0;font-size:.85em"> · 音频工作台 · 按需加载，同一时刻只驻留一个模型 · '
        '详细说明见 webui/README.md</span></div>')


if __name__ == "__main__":
    open_browser = os.environ.get("AUDIOCPP_NO_BROWSER") != "1"
    # --- 端口策略：视频工作台(MiniMax H3)钉死 7860，音频工作台默认 7861 ---
    # 可用 AUDIOCPP_PORT 覆盖；若被占用自动向后找空闲口，绝不与视频工作台抢 7860。
    def _find_free_port(preferred):
        for p in range(preferred, preferred + 40):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) if hasattr(socket, "SO_EXCLUSIVEADDRUSE") else None
                try:
                    probe.bind(("127.0.0.1", p))
                except OSError:
                    continue
            return p
        raise RuntimeError(f"no free port from {preferred}")
    try:
        _want = int(os.environ.get("AUDIOCPP_PORT", "17861"))   # 2026-09-27 TLS改造：外部https由Caddy接管(7861→17861)
    except ValueError:
        _want = 17861
    if _want == 7860:
        print("[port] 7860 已划归视频工作台(MiniMax H3)，音频自动改用 7861；如需指定请用 AUDIOCPP_PORT")
        _want = 7861
    _port = _find_free_port(_want)
    print(f"[port] 云来AI音频工作台 -> http://127.0.0.1:{_port}")
    # Gradio 6 moved css/theme from the Blocks constructor to launch().
    _fav = str((__import__("pathlib").Path(__file__).parent / "brand" / "favicon.ico"))
    demo.launch(server_name="127.0.0.1", server_port=_port, inbrowser=open_browser,  # 2026-09-27 三工作台互链 LAN 可达
                css=CUSTOM_CSS + _YUNLAI_BRAND_CSS,
                theme=_yl_theme, favicon_path=_fav,
                head=_YUNLAI_BRAND_HEAD, js=_YUNLAI_BRAND_JS)
