# -*- coding: utf-8 -*-
"""多老师 TTS 服务 v2 (CosyVoice2-0.5B, 声纹注册表 + voice 路由 + 403 无兜底)
1015 方案A(见 多老师音色架构方案_1015.md):
- 协议与 v1 完全兼容: POST /v1/audio/speech {model,input,voice,...} -> 24k/mono/PCM16 WAV
- 启动时读 engine/data/voice_registry.json, 逐师 add_zero_shot_spk (每师~2s, 一次性)
- voice 命中注册表 -> 用该师 spk+prompt+speed 合成; 未命中 -> 403 (结构性防串音, 绝不兜底)
- 兼容旧调用: LX_SINGLE=coach1013 环境变量时只注册凌雪(等价 v1, 用于回滚)
用法: LX_PORT=18092 python cosyvoice_lx_server_v2.py   (测试口先验, 绿了才换 18091)
"""
import os, sys, io, json, time, threading, traceback
# 1016双worker: 支持 --port N 参数(看门狗直接传端口, 免env管道)
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


VP = r"D:\AI-ModeLs\Voice-Pro"
sys.path.insert(0, VP)
sys.path.insert(0, os.path.join(VP, "third_party", "Matcha-TTS"))
sys.path.insert(0, os.path.join(VP, "cosyvoice"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODEL_DIR = os.path.join(VP, "model", "cosyvoice", "CosyVoice2-0.5B")
PORT = _ARGV_PORT if _ARGV_PORT else int(os.environ.get("LX_PORT", "18091"))
SINGLE = os.environ.get("LX_SINGLE", "")   # e.g. "lingxue" -> 只注册这一位(v1等价回滚位)

import numpy as np
import soundfile as sf
import voice_registry as VR

_t0 = time.time()
from cosyvoice.cli.cosyvoice import CosyVoice2
CW = CosyVoice2(MODEL_DIR, load_jit=False, load_trt=False, fp16=True)
SR = CW.sample_rate

REGISTRY = VR.load()
if SINGLE:
    REGISTRY = {SINGLE: REGISTRY[SINGLE]}

SPKS = {}   # voice_key -> {spk_id, ref_wav, prompt_text, speed, f0}
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
STAT = {"n": 0, "err": 0, "gen_s": 0.0, "audio_s": 0.0}


def synth(voice_key, text, speed=None):
    ent = SPKS[voice_key]
    spk, ref, prompt = ent["spk_id"], ent["ref_wav"], ent["prompt_text"]
    speed = speed or ent["speed"] or 1.0
    chunks = []
    with LOCK:
        # 1013cv-fix2 沿用: 极短句/跨语种先 cross_lingual, 失败回 zero_shot(同 spk 同源, 不换人).
        gens = [("cross", lambda: CW.inference_cross_lingual(
                    text, ref, zero_shot_spk_id=spk, stream=False, speed=speed)),
                ("zero", lambda: CW.inference_zero_shot(
                    text, prompt, ref, zero_shot_spk_id=spk, stream=False, speed=speed))]
        last = None
        for name, fn in gens:
            try:
                for out in fn():
                    chunks.append(out["tts_speech"].numpy().squeeze())
                if chunks:
                    last = name
                    break
            except Exception as e:
                _log("[lx-tts] %s fail %r" % (name, e))
                chunks = []
        if last:
            _log("[lx-tts] voice=%s mode=%s" % (voice_key, last))
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
            return self._json(200, {"ok": True, "engine": "cosyvoice2-0.5b-v2", "sr": SR,
                                    "voices": sorted(SPKS.keys()), "stat": STAT,
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
        # 1015 红线: voice 必须命中注册表, 未注册一律 403 —— 绝不兜底借嗓.
        voice = str(body.get("voice") or "").strip().lower()
        if voice not in SPKS:
            _log("[lx-tts] 403 unknown voice=%r registered=%s" % (voice, sorted(SPKS.keys())))
            return self._json(403, {"error": "unknown voice %r (no fallback by design)" % voice})
        speed = None
        try:
            if isinstance(body.get("options"), dict) and body["options"].get("speed"):
                speed = float(body["options"]["speed"])
        except Exception:
            speed = None
        t = time.time()
        try:
            y = synth(voice, text, speed)
        except Exception as e:
            STAT["err"] += 1
            _log("[lx-tts] ERR %r\n%s" % (e, traceback.format_exc()[-400:]))
            return self._json(500, {"error": str(e)[:200]})
        g = time.time() - t
        STAT["n"] += 1; STAT["gen_s"] += g; STAT["audio_s"] += len(y) / SR
        data = wav_bytes(y)
        _log("[lx-tts] voice=%s gen=%.2fs audio=%.2fs rtf=%.2f bytes=%d nchar=%d" % (
            voice, g, len(y) / SR, g / max(len(y) / SR, 1e-6), len(data), len(text)))
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    _log("[lx-tts] serving on http://127.0.0.1:%d" % PORT)
    srv.serve_forever()
