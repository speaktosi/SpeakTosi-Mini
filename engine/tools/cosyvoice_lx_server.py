# -*- coding: utf-8 -*-
"""凌雪专用 TTS 服务 (CosyVoice2-0.5B zero-shot, 音色一次注册=恒定)
- 说引擎现有的 OpenAI 式协议: POST /v1/audio/speech  {model,input,voice,response_format,options,seed}
  -> 返回 24k/mono/PCM16 WAV 字节。引擎 audiocpp.py 客户端不用改一行, 只改 --TTS_SERVER。
- 音色: 启动时把 Coach-Lingxue.wav 注册成 spk='coach1013' 一次, 此后每句都从这一个
  说话人条件生成 => 机制上不可能"每句换人"(实测句间距离 0.09 vs 生产 0.58)。
用法: python cosyvoice_lx_server.py     (端口 LX_PORT=18091)
"""
import os, sys, io, json, time, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 1013cv-fix1 致命BUG: 看门狗启动时 stdout=cp1252, print 中文日志触发 UnicodeEncodeError
# -> 合成已成功但响应发不出去 -> 客户端 RemoteDisconnected("串音修好了却听不到声")。
# 强制 UTF-8 + 所有日志走 _log() 兜底(errors=replace), 任何情况下不因日志崩掉请求。
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

MODEL_DIR = os.path.join(VP, "model", "cosyvoice", "CosyVoice2-0.5B")
REF_WAV   = os.environ.get("LX_REF", r"C:\Speaktosi\audiocpp\webui\voice\lingxue_ref.wav")
PROMPT_TEXT = ("That's a board. That's how you surf. Oh, this is how she trained. "
               "She was great at it. And she had a whole room filled with trophies.")
SPK_ID = "coach1013"
PORT = int(os.environ.get("LX_PORT", "18091"))

import numpy as np
import soundfile as sf

_t0 = time.time()
from cosyvoice.cli.cosyvoice import CosyVoice2
CW = CosyVoice2(MODEL_DIR, load_jit=False, load_trt=False, fp16=True)
CW.add_zero_shot_spk(PROMPT_TEXT, REF_WAV, SPK_ID)
SR = CW.sample_rate
_log("[lx-tts] loaded CosyVoice2 in %.1fs sr=%d spk=%s ref=%s" % (time.time() - _t0, SR, SPK_ID, REF_WAV))

LOCK = threading.Lock()
STAT = {"n": 0, "err": 0, "gen_s": 0.0, "audio_s": 0.0}


def synth(text, speed=1.0):
    chunks = []
    with LOCK:
        # 1013cv-fix2: 参考音是英文(Coach), 生产文本多为中文 => 跨语种。
        # 极短句用 zero_shot 实测会 "empty output"(prompt 比正文长太多), 先 cross_lingual,
        # 失败再回 zero_shot。两者音色同源(同一个注册 spk), 不影响"恒定单人声"。
        gens = []
        try:
            gens.append(("cross", lambda: CW.inference_cross_lingual(
                text, REF_WAV, zero_shot_spk_id=SPK_ID, stream=False, speed=speed)))
        except Exception:
            pass
        gens.append(("zero", lambda: CW.inference_zero_shot(
            text, PROMPT_TEXT, REF_WAV, zero_shot_spk_id=SPK_ID, stream=False, speed=speed)))
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
            _log("[lx-tts] mode=%s" % last)
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
            return self._json(200, {"ok": True, "engine": "cosyvoice2-0.5b", "sr": SR,
                                    "spk": SPK_ID, "ref": REF_WAV, "stat": STAT,
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
        # voice 参数只为兼容引擎payload; 本服务=凌雪专用, 一律 coach1013 (不兜底别人)
        voice = str(body.get("voice") or SPK_ID)
        speed = 1.0
        try:
            if isinstance(body.get("options"), dict):
                speed = float(body["options"].get("speed", 1.0) or 1.0)
        except Exception:
            speed = 1.0
        t = time.time()
        try:
            y = synth(text, speed)
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