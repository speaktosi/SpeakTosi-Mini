# -*- coding: utf-8 -*-
"""凌雪声音纯净规则 R3b —— 声道包络锚定 (把每句的"声道尺寸"对齐 Coach)

问题(1013 实证): qwen3-tts 0.6B 克隆**每次请求都重新随机抽一次说话人**(seed 无法复现:
同文本同 seed 连合 3 次 md5 全不同; 8 句连发 F0 231.9~320.0Hz = 5.57 半音; 单次 20.5s 长句
12s 后掉到 200~222Hz)。pitch-lock 只把基频拉回 262.3Hz, 声道(formant 包络)仍是抽到的那个人
→ Owen 耳朵里还是"串音/换人"。

本模块不赌模型采样, 改为**事后把每句的声学指纹对齐 Coach**:
  ① 静态频带增益(EQ): 量出该句的 24 频带电平, 与 Coach 参考比, 用线性相位 FIR 只改"亮度/厚度"
     (声道包络) —— 每句之间、每句与 Coach 之间的声道尺寸因此一致 = 同一个人。
  ② 之后把 RMS 还原成原值 → **响度不变**; 不动时长/节奏/停顿(纯静态滤波, 零时移)。
  ③ 任何异常一律 fail-open: 返回原音频, 绝不阻断出声。

纯 numpy + soundfile(两个 venv 都有), 无引擎依赖; 8010 引擎与 8765 backend 共用同一份。
"""
import io
import os
import wave

import numpy as np
try:                      # 两个 venv 都装了 soundfile; 万一缺 → 下文 fail-open 放行原音
    import soundfile as sf
except Exception:         # pragma: no cover
    sf = None

REF_DEFAULT = r"C:\Speaktosi\audiocpp\webui\voice\lingxue_ref.wav"
CACHE_DIR = r"C:\Speaktosi\engine\data"
N_BANDS = 24
F_LO, F_HI = 80.0, 11000.0
N_FFT = 1024
HOP = 256
N_TAPS = 512
MAX_DB = 8.0          # 单频带最大修正量(超过就可能是抽到极端嗓子, 硬掰会有金属音 → 夹住)
VOICED_RMS = 0.01
LAST_INFO = {}        # 最近一次锚定的 info(给 backend 的日志用; 免改函数签名)

# ===== 1015多师: 锚定目标 = 该老师自己的 ref(注册表真源), 不再全员对齐凌雪 =====
# 旧行为(1013单师): REF_DEFAULT=lingxue 压平所有人的声道包络 → 多师上线后 arthur/marcus/default
# 被掰成凌雪味(26条NN 23 MISS 实锤)。voice 不在注册表/文件缺失 → 回退 REF_DEFAULT(旧行为)。
_REGISTRY_PATH = r"C:\Speaktosi\engine\data\voice_registry.json"
_REG_CACHE = {"ts": 0.0, "map": {}}

def voice_ref(voice):
    """voice -> 该师 ref_wav 路径(注册表, 30s热载); 无 -> REF_DEFAULT。"""
    import time, json
    if time.time() - _REG_CACHE["ts"] > 30 or not _REG_CACHE["map"]:
        try:
            _REG_CACHE["map"] = json.loads(open(_REGISTRY_PATH, encoding="utf-8").read())
        except Exception:
            _REG_CACHE["map"] = {}
        _REG_CACHE["ts"] = time.time()
    e = _REG_CACHE["map"].get(str(voice or "").strip().lower()) or {}
    p = e.get("ref_wav")
    if p and os.path.exists(p):
        return p
    return REF_DEFAULT


def _band_idx(sr, n_fft=N_FFT):
    edges = np.geomspace(F_LO, min(F_HI, sr / 2 * 0.95), N_BANDS + 1)
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    return freqs, [np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0] for i in range(N_BANDS)]


def band_db(y, sr, n_fft=N_FFT, hop=HOP):
    """每帧算 24 频带 dB, 帧间取中位 → 谐波梳被不同 pitch 的帧抹平, 留下声道包络。"""
    freqs, idx = _band_idx(sr, n_fft)
    win = np.hanning(n_fft)
    rows = []
    for s in range(0, max(1, len(y) - n_fft), hop):
        f = y[s:s + n_fft]
        if len(f) < n_fft:
            break
        if np.sqrt(np.mean(f ** 2)) < VOICED_RMS:
            continue
        M = np.abs(np.fft.rfft(f * win)) / (n_fft / 2)
        rows.append([20 * np.log10(max(1e-9, float(M[i].mean()))) if len(i) else np.nan
                     for i in idx])
    if not rows:
        return None
    with np.errstate(invalid="ignore"):
        return np.nanmedian(np.array(rows), axis=0)


def ref_db(ref_path=REF_DEFAULT, cache_dir=CACHE_DIR):
    """Coach 参考的频带电平(带缓存, 按 ref 路径+mtime 失效)。量不出→None。"""
    try:
        import soundfile as sf
        st = os.stat(ref_path)
        key = f"{os.path.basename(ref_path)}_{int(st.st_mtime)}_{N_BANDS}_{N_FFT}"
        cp = os.path.join(cache_dir, "voice_anchor_ref_" + key + ".npz")
        if os.path.exists(cp):
            return np.load(cp)["db"]
        y, sr = sf.read(ref_path, dtype="float64", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        db = band_db(y, sr)
        if db is None:
            return None
        os.makedirs(cache_dir, exist_ok=True)
        np.savez(cp, db=db)
        return db
    except Exception:
        return None


def _fir_from_gain_db(gain_db, sr, freqs, n_taps=N_TAPS):
    """把每频带增益曲线做成线性相位 FIR(零时移, 保节奏)。"""
    # 频带中心 → 逐 bin 增益(先用 band 代表值, 再对数频率插值抹台阶)
    centers = []
    for g in range(N_BANDS):
        lo = F_LO * (min(F_HI, sr / 2 * 0.95) / F_LO) ** (g / N_BANDS)
        hi = F_LO * (min(F_HI, sr / 2 * 0.95) / F_LO) ** ((g + 1) / N_BANDS)
        centers.append((lo, hi))
    mids = np.array([np.sqrt(a * b) for a, b in centers])
    # 端点外推, 保证 0..Nyquist 全覆盖
    xs = np.concatenate([[1.0], mids, [sr / 2]])
    ys = np.concatenate([[gain_db[0]], gain_db, [gain_db[-1]]])
    # 1013fix: 直接截 irfft 前 n_taps 会丢掉循环回卷的一半能量(实测把距离从 5.6dB 推到 20dB)
    # → 改成: 在长 FFT(16k)上做零相位响应, fftshift 后取中心 n_taps 加 Blackman 窗 = 真正的线性相位 FIR
    n_long = 1 << 14
    f_long = np.fft.rfftfreq(n_long, 1.0 / sr)
    g_bin = np.interp(f_long, xs, ys)              # 逐 bin(dB)
    lin = 10 ** (g_bin / 20.0)
    hf = np.fft.irfft(lin, n=n_long)
    hc = np.fft.fftshift(hf)                       # 零相位 IR 中心对齐
    c = n_long // 2
    h = hc[c - n_taps // 2: c + n_taps // 2].copy()
    h *= np.blackman(len(h))
    return h


_REF_CENT = {}


def _shelf(y, sr, g_db, f0=1500.0):
    """极简亮度旋钮: 零相位高频搁架(低频不动, 高频 ±g_db), RMS 还原。"""
    n = len(y)
    Y = np.fft.rfft(y)
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    w = 1.0 / (1.0 + (fr / f0) ** 2)
    o = np.fft.irfft(Y * (10 ** ((g_db * (1.0 - w)) / 20.0)), n)
    return o * (np.sqrt(np.mean(y ** 2)) / (np.sqrt(np.mean(o ** 2)) + 1e-12))


def _centroid_hz(y, sr, n=1024):
    rows = []
    for s in range(0, max(0, len(y) - n), 512):
        f = y[s:s + n]
        if np.sqrt(np.mean(f ** 2)) < VOICED_RMS:
            continue
        M = np.abs(np.fft.rfft(f * np.hanning(n)))
        fr = np.fft.rfftfreq(n, 1.0 / sr)
        rows.append(float((M * fr).sum() / (M.sum() + 1e-9)))
    return float(np.median(rows)) if rows else None


def _bright_match(y, sr, target, lo=-8.0, hi=8.0, iters=8):
    """把整句的亮度(谱心)收到 target(=Coach), 只有一个旋钮; 求不出→原样。"""
    if not target:
        return y, 0.0
    g = 0.0
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        c = _centroid_hz(_shelf(y, sr, mid), sr)
        if c is None:
            return y, 0.0
        g = mid
        if c < target:
            lo = mid
        else:
            hi = mid
    return _shelf(y, sr, g), g


def _ref_centroid(sr=None, ref_path=None):
    """参考样音的谱心中位数(按目标采样率算, 带缓存; fail-open 返 None)。
    1015多师: ref_path=该师自己的 ref(注册表), None=旧行为 Coach(凌雪)。"""
    try:
        rp = ref_path or REF_DEFAULT
        if sf is None or not os.path.exists(rp):
            return None
        key = (os.path.basename(rp), int(sr) if sr else 0)
        if key in _REF_CENT:
            return _REF_CENT[key]
        y, sr0 = sf.read(rp, dtype="float64", always_2d=False)
        if getattr(y, "ndim", 1) > 1:
            y = y.mean(axis=1)
        if sr and int(sr) != int(sr0):
            n = int(round(len(y) * float(sr) / float(sr0)))
            y = np.interp(np.linspace(0.0, 1.0, n, endpoint=False),
                          np.linspace(0.0, 1.0, len(y), endpoint=False), y)
            sr0 = sr
        v = _centroid_hz(y, sr0)
        _REF_CENT[key] = v
        return v
    except Exception:
        return None


def anchor(y, sr, target_db=None, max_db=MAX_DB, voice=None):
    """把 y 的声道包络对齐 target。返回 (y2, info)。异常 -> 原样。
    1015多师: voice=该老师 -> 对齐其自己的 ref(注册表真源); voice=None -> 旧行为对齐 Coach。"""
    info = {"applied": False}
    try:
        y = np.asarray(y, dtype=np.float64)
        rp = voice_ref(voice) if voice else REF_DEFAULT
        if target_db is None:
            target_db = ref_db(rp)
        if target_db is None or len(y) < sr // 4:
            return y, info
        # 1013fix: 只对齐"频谱倾斜"(各自减掉自己的语音段均值) —— 绝对响度交给下面的 RMS 还原,
        # 否则整体音量差会同时加到 24 个频带上, 造出一个假的巨大修正量。
        freqs, _ = _band_idx(sr)          # 1013fix: _band_idx 返回 (freqs, idx) 两个值

        def _rel(v):
            v = np.asarray(v, dtype=np.float64)
            return v - float(np.nanmean(v[6:18]))

        def _dist(v):
            return float(np.nanmean(np.abs(_rel(v) - _rel(target_db))))

        cur = band_db(y, sr)
        if cur is None:
            return y, info
        before = _dist(cur)
        y2 = y
        gain_max = 0.0
        # 1013fix2: 一轮滤波只实现约 40% 目标量(FIR 截断 + 频带均值随修正漂移) →
        # 闭环两轮: 量 → 修 → 再量 → 再修。实测 4.64dB → 1.77dB(两轮)。
        for _it in range(2):
            c_i = band_db(y2, sr)
            if c_i is None:
                break
            g_i = np.nan_to_num(np.clip(_rel(target_db) - _rel(c_i), -max_db, max_db),
                                nan=0.0, posinf=max_db, neginf=-max_db)
            gain_max = max(gain_max, float(np.max(np.abs(g_i))))
            if float(np.max(np.abs(g_i))) < 0.5:
                break
            h = _fir_from_gain_db(g_i, sr, freqs)
            pad = len(h)
            yp = np.concatenate([np.full(pad, y2[0]), y2, np.full(pad, y2[-1])])
            y2 = np.convolve(yp, h, "same")[pad:-pad]
        m = np.abs(y) > VOICED_RMS
        if m.any() and np.abs(y2[m]).mean() > 1e-9:
            y2 *= float(np.sqrt(np.mean(y[m] ** 2)) / np.sqrt(np.mean(y2[m] ** 2)))
        after = _dist(band_db(y2, sr))
        # 1013pure R3c(简化): 包络对齐后, 再用**一个旋钮**把整句亮度(谱心)收到 Coach ——
        # 24频带EQ会把谱心推走(实测句间极差 557Hz = 耳朵里的"换人"); 收口后 557→9Hz。
        c_before = _centroid_hz(y, sr)
        try:
            y2, g_bright = _bright_match(y2, sr, _ref_centroid(sr, rp))
        except Exception:
            g_bright = 0.0
        c_after = _centroid_hz(y2, sr)
        info.update(applied=(after != before), gain_max=gain_max, before=before, after=after,
                    centroid_before=c_before, centroid_after=c_after, bright_db=g_bright)
        return y2, info
    except Exception as e:
        info["error"] = f"{type(e).__name__} {e}"
        return y, info


def _read_wav_bytes(data):
    with wave.open(io.BytesIO(data), "rb") as w:
        sr, nch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        raise ValueError(f"only 16-bit pcm supported, got {sw*8}bit")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if nch > 1:
        x = x.reshape(-1, nch)
    return x, sr, nch


def _write_wav_bytes(x, sr, nch):
    if x.ndim > 1:
        x = x.reshape(-1)
    pcm = np.clip(x, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(nch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return b.getvalue()


def anchor_wav_bytes(data, max_db=MAX_DB, target_db=None, voice=None):
    """就地锚定一段 wav bytes(引擎/后端共用)。失败 → 原样返回。"""
    try:
        x, sr, nch = _read_wav_bytes(data)
        if nch > 1:
            mono = x.mean(axis=1)
            y2, info = anchor(mono, sr, target_db, max_db, voice=voice)
            # 多声道: 用同一增益曲线作用于每一路(保持立体声/相位)
            if info.get("applied") and not info.get("noop"):
                # 简化: 只锚定单声道内容(本链路参考为 24k mono)
                x = np.repeat(y2[:, None], nch, axis=1)
            else:
                return data
        else:
            y2, info = anchor(x, sr, target_db, max_db, voice=voice)
            LAST_INFO.clear()
            LAST_INFO.update(info)
            if not info.get("applied"):
                return data
            x = y2
        return _write_wav_bytes(x, sr, nch)
    except Exception:
        return data


def anchor_file(path, max_db=MAX_DB, target_db=None, voice=None):
    """就地把文件里的声道包络锚定到该师参考(backend 用)。返回 info。"""
    info = {"applied": False}
    try:
        data = open(path, "rb").read()
        out = anchor_wav_bytes(data, max_db, target_db, voice=voice)
        info.update({k: v for k, v in LAST_INFO.items() if k != "applied"})
        if out is not data:
            open(path, "wb").write(out)
            info["applied"] = True
            info["bytes"] = len(out)
        return info
    except Exception as e:
        info["error"] = f"{type(e).__name__} {e}"
        return info


# ===== 规则 R3b 数组级入口(8010 引擎用: __create_bytes_stream 里拿到的是 float32 数组) =====
def anchor_array(y, sr, max_db=MAX_DB, target_db=None, voice=None):
    """只改声道包络(静态频带增益) + 把 RMS 还原 → 响度/时长/节奏不变。fail-open。
    1015多师: voice=该老师 → 锚其自己的 ref; None=旧行为锚 Coach(凌雪)。"""
    try:
        y2, info = anchor(np.asarray(y, dtype=np.float64).ravel(), sr,
                          target_db=target_db, max_db=max_db, voice=voice)
        LAST_INFO.clear()
        LAST_INFO.update(info)
        return y2
    except Exception:
        return np.asarray(y, dtype=np.float32).ravel()


# ===== 规则 R2b: 长句切段(单次合成超过参考窗就抽人; 分段合成后再靠 R3/R3b 对齐) =====
import re as _re

def est_secs(text):
    """粗估朗读秒数(CJK ≈0.22s/字, 英文 ≈0.43s/词; 与 18080 实测 154wpm 同量级)"""
    t = text or ""
    cjk = len(_re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", t))
    lat = len(_re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?", t)) if cjk == 0 else 0
    return cjk * 0.22 + lat * 0.43


def _tokens(t):
    """无损切词: CJK 单字 / 拉丁词 / 空白 / 其他符号 —— 拼接回去必须等于原文"""
    return _re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]|[A-Za-z0-9]+(?:'[A-Za-z]+)?|\s+|[^\s]", t)


def _hard_cap(seg, max_secs):
    """单段仍超长 → 按词/字无损硬切(不切词、不删字)"""
    out, cur = [], ""
    for tk in _tokens(seg):
        if cur and est_secs(cur + tk) > max_secs:
            out.append(cur)
            cur = tk if tk.strip() else ""
        else:
            cur += tk
    if cur.strip():
        out.append(cur)
    return out


def split_text(text, max_secs=10.0):
    """按标点贪心打包成 ≤max_secs 的段(保留全部标点/字符, 只分段)。"""
    t = (text or "").strip()
    if not t:
        return []
    if est_secs(t) <= max_secs:
        return [t]
    # 按标点切(标点留在前段), 保留空白
    raw = _re.findall(r"[^。！？!?.;；，,、:：]*[。！？!?.;；，,、:：]?\s*", t) or [t]
    segs, cur = [], ""
    for p in raw:
        if not p:
            continue
        if cur and est_secs(cur + p) > max_secs:
            segs.append(cur)
            cur = p
        else:
            cur += p
        while est_secs(cur) > max_secs:      # 逗号太少的超长句 → 硬切
            parts = _hard_cap(cur, max_secs)
            if len(parts) <= 1:
                break
            segs.append(parts[0])
            cur = "".join(parts[1:])
    if cur:
        segs.append(cur)
    return [s for s in segs if s]


def join_wavs(wavs, sil_max=0.08, fade_ms=20.0):
    """多段 wav bytes → 一段: 只掐掉各段首尾引擎补的静音(≤sil_max), 20ms 等功率交叉淡入对接。
    不重排、不压缩语音; 拼接后总时长 ≈ 各段语音 + 少量静音(节奏不变)。"""
    if not wavs:
        return None
    if len(wavs) == 1:
        return wavs[0]
    ys, srs = [], []
    for b in wavs:
        y, sr = sf.read(io.BytesIO(b), dtype="float64", always_2d=False)
        if getattr(y, "ndim", 1) > 1:
            y = y.mean(axis=1)
        ys.append(y)
        srs.append(sr)
    sr = srs[0]
    cut = int(sil_max * sr)
    fade = max(16, int(fade_ms / 1000.0 * sr))
    # ① 首尾静音只留 cut 个采样(引擎补的静音, 不是语音)
    trimmed = []
    for y in ys:
        thr = max(1e-4, 0.02 * float(np.max(np.abs(y)) or 0.0))
        nz = np.where(np.abs(y) > thr)[0]
        if len(nz) == 0:
            trimmed.append(y)
            continue
        a = max(0, int(nz[0]) - cut)
        b = min(len(y), int(nz[-1]) + 1 + cut)
        trimmed.append(y[a:b])
    # ② 20ms 等功率交叉淡入
    out = trimmed[0].astype(np.float64)
    for y in trimmed[1:]:
        y = y.astype(np.float64)
        n = min(fade, len(out), len(y))
        if n <= 1:
            out = np.concatenate([out, y])
            continue
        w = np.linspace(0.0, 1.0, n)
        fin, fout = np.sin(w * np.pi / 2), np.cos(w * np.pi / 2)
        out = np.concatenate([out[:len(out) - n],
                              out[len(out) - n:] * fout + y[:n] * fin,
                              y[n:]])
    return _write_wav_bytes(out, sr, 1)


def synth_multi(call_fn, text, max_secs=10.0):
    """按 R2b 分段合成再对接。call_fn(piece_text) -> wav bytes|None。
    短句(=绝大多数对话)只有一段 → 行为与旧代码完全一致(不加延迟)。"""
    pieces = split_text(text, max_secs=max_secs)
    if not pieces:
        return None
    if len(pieces) == 1:
        return call_fn(pieces[0])
    outs = []
    for p in pieces:
        try:
            w = call_fn(p)
        except Exception:
            w = None
        if w:
            outs.append(w)
    if not outs:
        return None
    return join_wavs(outs)


if __name__ == "__main__":
    import sys
    import soundfile as sf
    t = ref_db()
    print("Coach 参考 24 频带 dB:", None if t is None else np.round(t, 1).tolist())
    print("自比距离:", float(np.nanmean(np.abs(t - t))) if t is not None else None)
    if len(sys.argv) > 1:
        y, sr = sf.read(sys.argv[1], dtype="float64", always_2d=False)
        if y.ndim > 1:
            y = y.mean(axis=1)
        b = band_db(y, sr)
        print("输入距离:", round(float(np.nanmean(np.abs(b - t))), 2), "dB")
        y2, info = anchor(y, sr)
        print("锚定后:", round(info.get("after", -1), 2), "dB  (max修正 %.2f dB)" % info.get("gain_max", 0))
        sf.write(sys.argv[1].replace(".wav", "_anchored.wav"), y2.astype(np.float32), sr)