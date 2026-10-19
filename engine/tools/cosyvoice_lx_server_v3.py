# -*- coding: utf-8 -*-
"""多老师 TTS 服务 v3 (CosyVoice2-0.5B) —— 基于 v2, 加两处硬优化+确定性
1017 (见 首音吞吐_1017计划.md, 全部结论来自本机实测):
 P1 AR 快路: ①Qwen2LM 权重真 fp16(原为 fp32 权重 + 每 token autocast 现转 fp16)
             ②跳过 151936 词表 lm_head 白算(logits_to_keep=1, 隐藏态语义不变)
             实测 AR 34.6 -> 44.3 tok/s, 单发 RTF ≈0.85 -> 0.72 (idle)
             回滚: 环境变量 LX_AR_FAST=0 (或直接改回 v2 文件)
 P3 采样确定性: 支持 body.seed / body.options.seed -> torch/random/np 三路播种
             实测同音色同句无 seed 时 F0 会在 145~170Hz 漂移(音质不连贯根因), 
             有 seed 后同请求可复现 -> 引擎侧"锁句种子"才能真正生效
协议与 v2 完全一致(POST /v1/audio/speech -> 24k/mono/PCM16 WAV; GET /health);
v3 只增: 响应头 X-LX-Seed, /health 里 ar_fast/seed 计数。未注册 voice 仍 403 无兜底。
"""
import os, sys, io, json, time, threading, traceback
_ARGV_PORT = None
if "--port" in sys.argv:
    _i = sys.argv.index("--port")
    if _i + 1 < len(sys.argv):
        _ARGV_PORT = int(sys.argv[_i + 1])
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def _resolve_vp():
    """1019portable: Voice-Pro 根目录 —— LX_VP 环境变量 > 包内 runtime/voice-pro > 开发机 D: 兜底。"""
    e = os.environ.get("LX_VP")
    if e and os.path.isdir(e):
        return e
    try:
        here = os.path.dirname(os.path.abspath(__file__))      # engine/tools
        pkg = os.path.dirname(os.path.dirname(here))            # 包根
        cand = os.path.join(pkg, "runtime", "voice-pro")
        if os.path.isdir(cand):
            return cand
    except Exception:
        pass
    return r"D:\AI-ModeLs\Voice-Pro"


VP = _resolve_vp()
sys.path.insert(0, VP)
sys.path.insert(0, os.path.join(VP, "third_party", "Matcha-TTS"))
sys.path.insert(0, os.path.join(VP, "cosyvoice"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODEL_DIR = os.path.join(VP, "model", "cosyvoice", "CosyVoice2-0.5B")
PORT = _ARGV_PORT if _ARGV_PORT else int(os.environ.get("LX_PORT", "18091"))
SINGLE = os.environ.get("LX_SINGLE", "")
AR_FAST = os.environ.get("LX_AR_FAST", "1") != "0"

import random
import numpy as np
import soundfile as sf
import voice_registry as VR

_t0 = time.time()
from cosyvoice.cli.cosyvoice import CosyVoice2
CW = CosyVoice2(MODEL_DIR, load_jit=False, load_trt=False, fp16=True)
SR = CW.sample_rate

# ---------- P1 AR 快路 ----------
AR_STATE = {"fp16": False, "no_lmhead": False}


def _fast_lm(cw):
    if not AR_FAST:
        _log("[lx-tts] P1 AR fastpath OFF (LX_AR_FAST=0)")
        return
    import types
    import torch
    try:
        lm = cw.model.llm          # Qwen2LM (llm/Qwen2Encoder 三件套)
        inner = lm.llm.model       # transformers Qwen2ForCausalLM
        lm.half()                  # ① 权重真 fp16
        AR_STATE["fp16"] = True
        orig = inner.forward
        def fast_forward(self, *a, **kw):
            kw["logits_to_keep"] = 1      # ② 只有末位隐藏态进 lm_head
            return orig(*a, **kw)
        inner.forward = types.MethodType(fast_forward, inner)
        AR_STATE["no_lmhead"] = True
        _log("[lx-tts] P1 AR fastpath ON: lm fp16 + logits_to_keep=1")
    except Exception as e:
        _log("[lx-tts] P1 AR fastpath FAILED -> baseline: %r" % (e,))
        AR_STATE["fp16"] = AR_STATE["no_lmhead"] = False


_fast_lm(CW)


# ---------- P3 采样确定性 ----------
def _seed_all(seed):
    """三路播种: torch(CUDA) / random / numpy。必须在 LOCK 内、每次生成前调用。"""
    import torch
    seed = int(seed)
    random.seed(seed)
    np.random.seed(seed % (2 ** 31 - 1))
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


REGISTRY = VR.load()
if SINGLE:
    REGISTRY = {SINGLE: REGISTRY[SINGLE]}

SPKS = {}
for key, ent in REGISTRY.items():
    spk_id = ent.get("spk_id") or key
    t = time.time()
    CW.add_zero_shot_spk(ent["prompt_text"], ent["ref_wav"], spk_id)
    SPKS[key] = {"spk_id": spk_id, "ref_wav": ent["ref_wav"],
                 "prompt_text": ent["prompt_text"],
                 "speed": float(ent.get("speed", 1.0) or 1.0),
                 "f0": ent.get("f0")}
    _log("[lx-tts] registered %-9s spk=%-10s f0=%s (%.1fs)" % (key, spk_id, ent.get("f0"), time.time() - t))
_log("[lx-tts] loaded CosyVoice2 + %d voices in %.1fs sr=%d" % (len(SPKS), time.time() - _t0, SR))

LOCK = threading.Lock()
STAT = {"n": 0, "err": 0, "gen_s": 0.0, "audio_s": 0.0, "seeded": 0, "unseeded": 0}


def synth(voice_key, text, speed=None, seed=None):
    ent = SPKS[voice_key]
    spk, ref, prompt = ent["spk_id"], ent["ref_wav"], ent["prompt_text"]
    speed = speed or ent["speed"] or 1.0
    chunks = []
    with LOCK:
        gens = [("cross", lambda: CW.inference_cross_lingual(
                    text, ref, zero_shot_spk_id=spk, stream=False, speed=speed)),
                ("zero", lambda: CW.inference_zero_shot(
                    text, prompt, ref, zero_shot_spk_id=spk, stream=False, speed=speed))]
        last = None
        for name, fn in gens:
            try:
                chunks = []
                if seed is not None:
                    _seed_all(seed)     # 每次尝试前重播, 保证跨尝试同源
                for out in fn():
                    chunks.append(out["tts_speech"].numpy().squeeze())
                if chunks:
                    last = name
                    break
            except Exception as e:
                _log("[lx-tts] %s fail %r" % (name, e))
                chunks = []
        if last:
            _log("[lx-tts] voice=%s mode=%s seed=%s" % (voice_key, last, seed))
    if not chunks:
        raise RuntimeError("empty output")
    y = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def wav_bytes(y):
    buf = io.BytesIO()
    sf.write(buf, y, SR, subtype="PCM_16", format="WAV")
    return buf.getvalue()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith(("/health", "/")):
            return self._json(200, {"ok": True, "engine": "cosyvoice2-0.5b-v3", "sr": SR,
                                    "voices": sorted(SPKS.keys()), "stat": STAT,
                                    "ar_fast": AR_STATE,
                                    "uptime_s": round(time.time() - _t0, 1)})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith(("/v1/audio/speech", "/tts")):
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._json(400, {"error": "bad json %r" % (e,)})
        text = (body.get("input") or body.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "empty input"})
        voice = str(body.get("voice") or "").strip().lower()
        if voice not in SPKS:
            _log("[lx-tts] 403 unknown voice=%r registered=%s" % (voice, sorted(SPKS.keys())))
            return self._json(403, {"error": "unknown voice %r (no fallback by design)" % voice})
        opt = body.get("options") if isinstance(body.get("options"), dict) else {}
        speed = None
        try:
            if opt.get("speed"):
                speed = float(opt["speed"])
        except Exception:
            speed = None
        seed = None
        try:
            raw_seed = body.get("seed", opt.get("seed"))
            if raw_seed is not None and str(raw_seed).strip() != "":
                seed = int(raw_seed)
        except Exception:
            seed = None
        t = time.time()
        try:
            y = synth(voice, text, speed, seed)
        except Exception as e:
            STAT["err"] += 1
            _log("[lx-tts] ERR %r\n%s" % (e, traceback.format_exc()[-400:]))
            return self._json(500, {"error": str(e)[:200]})
        g = time.time() - t
        STAT["n"] += 1; STAT["gen_s"] += g; STAT["audio_s"] += len(y) / SR
        STAT["seeded" if seed is not None else "unseeded"] += 1
        data = wav_bytes(y)
        _log("[lx-tts] voice=%s gen=%.2fs audio=%.2fs rtf=%.2f bytes=%d nchar=%d seed=%s" % (
            voice, g, len(y) / SR, g / max(len(y) / SR, 1e-6), len(data), len(text), seed))
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-LX-Seed", "" if seed is None else str(seed))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    _log("[lx-tts] v3 serving on http://127.0.0.1:%d (ar_fast=%s)" % (PORT, AR_STATE))
    srv.serve_forever()