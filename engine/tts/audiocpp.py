import os
import time
import numpy as np
import resampy
import soundfile as sf
import requests
from io import BytesIO
import json
import threading

from utils.logger import logger
from .base_tts import BaseTTS, State
from registry import register


# 1016双worker: 多端点轮询(读 data/tts_endpoints.json 热更新; 失败自动摘除60s)
_TTS_EP = {"t": 0.0, "urls": [], "i": 0, "dead": {}}
_TTS_EP_LOCK = threading.Lock()


def _tts_pick(default):
    """返回本轮使用的 TTS 端点(轮询); 配置缺失时回退默认单口。"""
    try:
        p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'tts_endpoints.json')
        m = os.path.getmtime(p)
        if m != _TTS_EP["t"]:
            with open(p, encoding='utf-8') as f:
                urls = json.load(f).get('servers') or []
            _TTS_EP["urls"] = [str(u).rstrip('/') for u in urls if str(u).strip()]
            _TTS_EP["t"] = m
            logger.info('[tts-ep] endpoints -> %s', ','.join(_TTS_EP["urls"]) or '(default)')
    except Exception:
        pass
    now = time.time()
    urls = [u for u in _TTS_EP["urls"] if now > _TTS_EP["dead"].get(u, 0)]
    if not urls:
        return (default or 'http://127.0.0.1:18091').rstrip('/')
    with _TTS_EP_LOCK:
        _TTS_EP["i"] = (_TTS_EP["i"] + 1) % len(urls)
        pick = urls[_TTS_EP["i"]]
    return pick


def _tts_dead(url, secs=60):
    _TTS_EP["dead"][url] = time.time() + secs
    logger.warning('[tts-ep] %s 暂摘 %ss', url, secs)


def _tts_alt(primary):
    """返回一个不同于 primary 的可用端点(不推进轮询计数), 用于失败备用。"""
    now = time.time()
    for u in _TTS_EP["urls"]:
        if u != primary and now > _TTS_EP["dead"].get(u, 0):
            return u
    return None


# 1016并行预取: 下一句在第二个 worker 上提前合成, 与当前句校验/投递并行 —— 治"说两句就卡"
from concurrent.futures import Future
_PF = {}
_PF_LOCK = threading.Lock()


def _synth_key(voice, text):
    return (str(voice or '').lower(), str(text))


def _synth_run(opt, text, voice, vp, model, seed):
    """线程内完成一次完整合成(含长句分段), 返回 wav bytes。端点按轮询自选+失败备用。"""
    _temp = float(vp.get('temperature') if vp.get('temperature') is not None else 0.1)

    def _post(_txt):
        body = {'model': model, 'input': _txt, 'voice': voice, 'response_format': 'wav',
                'options': {'temperature': _temp}}
        if seed is not None:
            body['seed'] = int(seed)
            body['options']['seed'] = int(seed)
        _u1 = _tts_pick(opt.TTS_SERVER)
        _u2 = _tts_alt(_u1)
        _urls = [_u1] + ([_u2] if _u2 else [])
        _last = None
        for _u in _urls:
            try:
                r = requests.post(_u + '/v1/audio/speech', json=body, timeout=90)
                r.raise_for_status()
                if len(r.content) <= 800:
                    raise RuntimeError('tiny audio %d bytes' % len(r.content))
                return r.content
            except requests.exceptions.RequestException as _e:
                _last = _e
                _tts_dead(_u)
                logger.warning('[tts-ep] %s 请求失败(%s), 尝试备用', _u, type(_e).__name__)
        raise _last

    from .voice_anchor import synth_multi as _synth_multi
    return _synth_multi(_post, text)


def _synth_async(opt, text, voice, vp, model, seed):
    f = Future()

    def _work():
        try:
            f.set_result(_synth_run(opt, text, voice, vp, model, seed))
        except Exception as e:
            f.set_exception(e)

    threading.Thread(target=_work, daemon=True, name='tts-synth').start()
    return f


def _wsola(x, speed):
    """1002 WSOLA 时域伸缩(纯numpy): 保基频=保 SpeakTosi Demo Voice 音色。
    speed<1 变慢(时长×1/speed), speed>1 变快。与 backend _stretch_wav 同款算法。"""
    N, H = 1024, 512
    if len(x) < 2 * N:
        return x
    win = np.sqrt(0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N) / N))
    out = np.zeros(int(len(x) / speed) + N, np.float64)
    ipos, opos, search = 0.0, 0, H // 2
    while ipos < len(x) - N:
        i = int(round(ipos))
        if opos > 0:
            ref = out[max(0, opos - N):opos]
            lo = max(0, i - search); hi = min(len(x) - N, i + search, len(x) - len(ref))
            if hi >= lo:
                best, bi = -1e18, i
                for off in range(lo, hi + 1, 32):
                    d = float(np.dot(ref, x[off:off + len(ref)]))
                    if d > best:
                        best, bi = d, off
                # 粗搜后细搜 ±32
                lo2, hi2 = max(lo, bi - 32), min(hi, bi + 32)
                for off in range(lo2, hi2 + 1, 4):
                    d = float(np.dot(ref, x[off:off + len(ref)]))
                    if d > best:
                        best, bi = d, off
                i = bi
        seg = x[i:i + N]
        if len(seg) < N:
            break
        out[opos:opos + N] += seg * win
        opos += H
        ipos += H * speed           # 1002fix: 步进固定累加(与backend同款; 不可rebase到搜索i, 否则循环失控越界)
    out = out[:int(round(len(x) / speed))]
    peak = np.abs(x).max() / (np.abs(out).max() + 1e-9)   # 幅度对齐原信号
    return out * min(peak, 6.0)


@register("tts", "audiocpp")
class AudioCppTTS(BaseTTS):
    """本地 audiocpp server 的 OpenAI 式 /v1/audio/speech。
    voice_ref 已在 server config 注入(SpeakTosi Demo Voice 25s 克隆)，这里只需发文本。
    opt.TTS_SERVER = http://127.0.0.1:18080 ; opt.REF_FILE = voice preset name (default)
    """

    def _prefetch_next(self, model):
        """1016并行预取: 在后台线程用另一个 TTS worker 提前合成队列里的下一条文本。"""
        q = getattr(self, 'msgqueue', None)
        # 1016: 预取深度2 —— 让两个 worker 持续都忙, 播放队列不断粮(治句中3~4s空洞)
        items = list(getattr(q, 'queue', []) or [])[:2]
        if not items:
            return
        for m in items:
            self._prefetch_one(m, model)

    def _prefetch_one(self, m, model):
        """预取单条消息(内部用)。"""
        nt, nev = (m if isinstance(m, (tuple, list)) and len(m) == 2 else (m, {}))
        nt = str(nt or '')
        if not nt:
            return
        nv = (nev or {}).get('tts', {}).get('ref_file', self.opt.REF_FILE) or 'default'
        nk = _synth_key(nv, nt)
        with _PF_LOCK:
            if nk in _PF:
                return
            if len(_PF) > 6:
                _PF.pop(next(iter(_PF)), None)
            holder = Future()
            _PF[nk] = holder
        try:
            nvp = dict(self._voice_params(nv) or {})
            nseed = AudioCppTTS._seed_cache.get((str(nv or '').lower(), nt[:160]), nvp.get('seed'))
        except Exception:
            nvp, nseed = {}, None

        def _work():
            try:
                holder.set_result(_synth_run(self.opt, nt, nv, nvp, model, nseed))
            except Exception as e:
                holder.set_exception(e)

        threading.Thread(target=_work, daemon=True, name='tts-prefetch').start()
        logger.info('[pf] 预取下一句 voice=%s text=%r', nv, nt[:28])

    def txt_to_audio(self, msg: tuple[str, dict]):
        text, textevent = msg
        # 1016并行预取(前置): 立刻让第二个 worker 开始合成"下一句", 与本句合成并行 —— 双倍吞吐
        try:
            _model0 = getattr(self.opt, 'audiocpp_model', 'qwen3-tts')
            self._prefetch_next(_model0)
        except Exception as _pe0:
            logger.warning('[pf] prefetch(pre) skip: %s', _pe0)
        # 1016双worker: 轮询选取端点(18091/18092 并行合成, 提高流水线余量)
        server = _tts_pick(self.opt.TTS_SERVER)
        model = getattr(self.opt, 'audiocpp_model', 'qwen3-tts')
        voice = textevent.get('tts', {}).get('ref_file', self.opt.REF_FILE) or 'default'
        t = time.time()
        # 1006 音色自检重试: 0.6B克隆服务无seed时轮间随机漂移(凌云~40%漂到凌风音色, Owen 1006报告"串音")。
        # 逐位参数(seed/temperature)写在 tts_calibration.json voice_params; 合成后实测F0中位数,
        # 偏离该音色ref基频超tol(对数比)→换seed重试, 最多3次。根治"声音不正常/串音"。
        vp = self._voice_params(voice)
        if str(voice).strip().lower() in ('', 'default', 'none'):
            logger.error(f'audiocpp R1 voice-fallback risk: voice={voice!r} — 服务器会走 '
                         f'voice_presets.default(=demo_voice_ref.wav, 另一个人) = 串音! '
                         f'检查 --REF_FILE / textevent.tts.ref_file 传参')
        # 1013r2 严选 best-of-N: 治 Owen"声音还是串"。
        # 实证(1013实测): 18080/0.6B 生成对 (文本,seed,temperature) 完全确定——
        #   同句同seed连抽5次F0一模一样(208.7/210.5/210.5/210.5/212.4), 换seed则整条轨迹换人
        #   (263/238/282/266/207Hz)。F0由文本决定: "You can do it." seed7=355Hz(童声),
        #   "That is a board." seed7=207Hz(成人低嗓)。
        # 1013pure 规则R2(从头定, 见 凌雪声音纯净规则.md): 生产链路**不抽签**。
        #   旧的 best-of-N + "F0不合格换seed重试" 每换一次 seed 就是换一次音色归属/声道尺寸
        #   → 它本身就是"串音"制造机(同一句话被抽成 250/237/286Hz 三种人)。
        #   新规则: seed 只来自 ①人工锁定表 _seed_cache ②标定 voice_params.seed, **一次请求定音**;
        #   残余偏差交给 R3 pitch-lock(rubberband formant=preserved) 锁回, 失败即静音(R4)。
        ref_f0 = vp.get('f0')
        strict = float(vp.get('strict_tol', 0.04))
        base = vp.get('seed')
        key = (str(voice or '').lower(), str(text)[:160])
        _cache = AudioCppTTS._seed_cache
        seed = _cache.get(key, base)
        data = None
        try:
            # 1016并行预取: 命中后台预取结果(秒回), 否则现场起线程合成
            with _PF_LOCK:
                _fut = _PF.pop(_synth_key(voice, text), None)
            if _fut is None:
                _fut = _synth_async(self.opt, text, voice, vp, model, seed)
            data = _fut.result()
            if data:
                try:
                    ok, f0 = self._voice_ok(data, vp)
                    import math as _m
                    dev = abs(_m.log(f0 / float(ref_f0))) if (f0 and ref_f0) else 0.0
                    logger.info(f'audiocpp voice-check(R2单粒): voice={voice} seed={seed} '
                                f'f0={f0:.1f} dev={dev:.3f} strict={strict} ok={ok} '
                                f'text={str(text)[:28]!r} secs={len(data) / 48000.0:.2f}')
                except Exception:
                    pass
            # 1016并行预取: 趁本句投递/播放时, 另一个 worker 先把下一句合成掉
            try:
                self._prefetch_next(model)
            except Exception as _pe:
                logger.warning('[pf] prefetch skip: %s', _pe)
        except Exception as e:
            logger.error(f'audiocpp tts err(R2 不重抽): {type(e).__name__} {str(e)[:120]}')
        if data is None:
            logger.error('audiocpp tts failed -> 静音(R4: 不兜底, 不换seed)')
            return
        logger.info(f'-------audiocpp tts time:{time.time() - t:.4f}s bytes={len(data)} voice={voice}')
        self.input_stream.seek(0)
        self.input_stream.truncate()
        self.input_stream.write(data)
        self.input_stream.seek(0)
        stream = self.__create_bytes_stream(self.input_stream, voice, text)
        streamlen = stream.shape[0]
        idx = 0
        _wait = 0.0
        while streamlen >= self.chunk:
            if self.state != State.RUNNING:
                # 1013fix 掉词/掉句: 状态瞬变(PAUSE/重连/打断)不再静默丢弃整句
                # 现场铁证: 11:39:04 deliver sent=0/72960 state=PAUSE = 整句4.56s凭空消失(Owen"掉词")
                if _wait <= 0.0:
                    logger.warning(f'audiocpp deliver WAIT state={self.state} voice={voice} '
                                   f'text={str(text)[:40]!r}')
                if _wait > 6.0:
                    logger.error(f'audiocpp deliver ABORT state={self.state} after {_wait:.1f}s '
                                 f'sent={idx}/{stream.shape[0]} voice={voice}')
                    break
                time.sleep(0.05)
                _wait += 0.05
                continue
            eventpoint = {}
            streamlen -= self.chunk
            if idx == 0:
                eventpoint = {'status': 'start', 'text': text}
            elif streamlen < self.chunk:
                eventpoint = {'status': 'end', 'text': text}
            eventpoint.update(**textevent)
            self.parent.put_audio_frame(stream[idx:idx + self.chunk], eventpoint)
            idx += self.chunk
        # 1013diag: 掉词/掉句取证 —— 实际投递样本 vs 生成样本 + 收尾状态
        _sr = max(self.sample_rate, 1)
        logger.info(f'audiocpp deliver voice={voice} sent={idx}/{stream.shape[0]} samples '
                    f'({idx / _sr:.2f}s/{stream.shape[0] / _sr:.2f}s) state={self.state} chunk={self.chunk}')
        self.input_stream.seek(0)
        self.input_stream.truncate()

    def __create_bytes_stream(self, byte_stream, voice=None, text=None):
        stream, sample_rate = sf.read(byte_stream)
        logger.info(f'[INFO]audiocpp audio stream {sample_rate}: {stream.shape}')
        stream = stream.astype(np.float32)
        if stream.ndim > 1:
            stream = stream[:, 0]
        if sample_rate != self.sample_rate and stream.shape[0] > 0:
            stream = resampy.resample(x=stream, sr_orig=sample_rate, sr_new=self.sample_rate)
        # 1002 语速档位(-25%/0/+25%): 学生可在设置页调; 读 backend config/app.json, 30s缓存
        # 变速用相位声码器 time_stretch —— 只改时长不改音高, 保持 SpeakTosi Demo Voice 音色
        # 1005v3: 传原文+本轮裸时长 → 动态语速归一化(服务裸读轮间±30%方差, 静态因子修不了)
        raw_secs = (stream.shape[0] / self.sample_rate) if stream.shape[0] > 0 else None
        rate = self._speech_rate(voice, text, raw_secs)
        if rate and abs(rate - 1.0) > 1e-3 and stream.shape[0] > 0:
            try:
                # 1002fix: 弃 librosa 相位声码器(+25%实测基频漂移+8.7%, 听感变尖),
                # 改 WSOLA(实域重叠相加+波形对齐): 真实语音实测漂移 ≤2.5%, 保 SpeakTosi Demo Voice 音色
                stream = _wsola(stream, rate).astype(np.float32)
                logger.info(f'audiocpp speech_rate={rate:.2f} wsola {stream.shape[0]} samples')
            except Exception as e:
                logger.error(f'audiocpp wsola fail: {type(e).__name__} {str(e)[:80]}')
        # ===== 1013lock: 逐句音高锁回参考基频(治"声音仍然在变") =====
        # 1015终局(v35+v37): 裸链39/39 + 旁路链26/26 campplus全对; 锁/锚反而把
        # mia 0.83→0.39、eleanor 0.63→0.40 拉爆 = 串音真凶。默认旁路;
        # 需要回滚时设 SPEAKTOSI_PITCH_LOCK=1 / SPEAKTOSI_ANCHOR=1 即可恢复旧行为。
        if os.environ.get('SPEAKTOSI_PITCH_LOCK', '0') != '0':
            stream = self._pitch_lock(stream, voice)
        # ===== 1013pure R3b: 声道包络锚定(把每句的"声道尺寸"对齐 Coach) =====
        # pitch-lock 只锁 F0; 但 0.6B 克隆每句随机抽人(8句连发 F0 231.9~320.0Hz = 5.57半音),
        # 声道包络跟着变 = Owen 耳朵里的"串音/换人"。这里把每句 24 频带电平对齐 Coach 参考
        # (静态线性相位 EQ, 只改亮度/厚度, 零时移, RMS 还原) → 实测每句与 Coach 包络距离 4.0dB→0.7dB。
        try:
            if os.environ.get('SPEAKTOSI_ANCHOR', '0') == '0':
                logger.info('audiocpp anchor(R3b): BYPASS(默认旁路, SPEAKTOSI_ANCHOR=1恢复) voice=%s' % voice)
            else:
                from .voice_anchor import anchor_array as _anchor_array, LAST_INFO as _ainfo
                stream = _anchor_array(stream, self.sample_rate, voice=voice).astype(np.float32)
                logger.info(f"audiocpp anchor(R3b): voice={voice} n={stream.shape[0]} "
                            f"dist={_ainfo.get('before', -1):.2f}->{_ainfo.get('after', -1):.2f}dB "
                            f"cent={_ainfo.get('centroid_before', -1):.0f}->{_ainfo.get('centroid_after', -1):.0f}Hz")
        except Exception as e:
            logger.error(f'audiocpp anchor fail(放行原音): {type(e).__name__} {str(e)[:80]}')
        return stream

    def _pitch_lock(self, stream, voice=None):
        """1013lock: 逐句音高锁回该音色参考基频(治0.6B克隆逐句漂移 = "第二句突然换了人")。
        实测(1013 locktest 18样本×3温度): 逐句F0 208~333Hz 跨度6~8半音; 温度0.3->0.1 仍6.2半音,
        温度0被服务端拒(500)/换seed/加长参考(28s)全部无效 → 模型层无旋钮, 只能对生成结果锁定。
        只做整句常量等比变调(rubberband pitchq=3 + formant=preserved 真保共振峰;
        1013串音: 原命令漏写 formant= → 走默认 shifted(共振峰随音高同倍搬移),
        每句变调比不同(±2.5半音)则每句"声道尺寸"都变 = 逐句换人("串音"))。
        不动节奏/停顿/时长/响度(严格守"按Coach刻制")。迭代≤2次自查收敛, 失败一律 fail-open。"""
        vp = self._voice_params(voice) or {}
        if not vp.get('pitch_lock'):
            return stream
        try:
            f0ref = float(vp.get('f0') or 0)
            if not f0ref or stream is None or stream.shape[0] <= 0:
                return stream
            tol = float(vp.get('lock_tol', 0.03))
            import math
            cur, n = stream, 0
            med0 = self._f0_med(stream, self.sample_rate, ref=f0ref)
            logger.info(f'audiocpp lock-probe voice={voice} f0={med0 and round(med0, 1)} '
                        f'ref={f0ref:.1f} tol={tol}')
            med = med0
            # 1015bisect: 固定比例步在 tol 附近会过冲振荡(mia实证 +0.6->-0.7->+0.4 来回弹,
            # 每次 rubberband 重采样还添噪)。改带记忆二分: 记住上一次变调步的符号,
            # 同侧=步长不够→加倍; 越过零=过头→步长减半回退。必收敛, 上限4次。
            last = 0.0
            while med and n < 4:
                lr = math.log(f0ref / float(med))   # >0=偏低需升, <0=偏高需降
                if abs(lr) <= tol:
                    break
                if last and (lr > 0) != (last > 0):
                    step = abs(last) / 2.0          # 过头: 减半回退
                else:
                    step = min(abs(lr), abs(last) * 2 if last else abs(lr))
                last = step if lr > 0 else -step
                ratio = max(0.75, min(1.33, math.exp(last)))
                out = self._pitch_shift(cur, ratio)
                if out is None or out.shape[0] <= 0:
                    break
                cur, n = out, n + 1
                med = self._f0_med(cur, self.sample_rate, ref=f0ref)
            if n:
                logger.info(f'audiocpp pitch-lock voice={voice} f0={med0:.1f}->{med or 0:.1f} '
                            f'ref={f0ref:.1f} iters={n}')
            return cur
        except Exception as e:
            logger.error(f'audiocpp pitch-lock fail: {type(e).__name__} {str(e)[:100]}')
            return stream

    def _pitch_shift(self, stream, ratio):
        """整句等比变调: ffmpeg rubberband pitchq=3 + formant=preserved(真保共振峰,
        只移F0不动声道尺寸) 纯pipe无临时文件。失败返回None(fail-open)。"""
        import os, shutil, subprocess
        ff = os.environ.get('SPEAKTOSI_FFMPEG') or r'C:\Speaktosi\audiocpp\webui\ffmpeg.exe'
        if not os.path.exists(ff):
            ff = shutil.which('ffmpeg') or ''
        if not ff:
            return None
        sr = int(self.sample_rate)
        raw = (np.clip(stream, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        p = subprocess.run([ff, '-hide_banner', '-loglevel', 'error',
                            '-f', 's16le', '-ar', str(sr), '-ac', '1', '-i', 'pipe:0',
                            '-af', f'rubberband=pitch={ratio:.6f}:pitchq=3:formant=preserved',
                            '-f', 's16le', '-ac', '1', '-ar', str(sr), 'pipe:1'],
                           input=raw, capture_output=True)
        if p.returncode != 0 or not p.stdout:
            logger.error(f'audiocpp pitch-ffmpeg rc={p.returncode} {str(p.stderr)[:120]}')
            return None
        return np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32) / 32768.0

    _seed_cache = {}            # 1013r2 {voice,text前160} -> 命中严选线的seed; 重复句1粒即中

    _rate_cache = {}            # {voice_key: [value, ts]} 类级, 30s 热加载(按voice分桶)

    def _speech_rate(self, voice=None, text=None, raw_secs=None):
        import json, os, time
        now = time.time()
        ck = str(voice or '').lower()
        cache = AudioCppTTS._rate_cache
        tier = None
        if ck in cache and cache[ck][1] and now - cache[ck][1] < 30:
            tier = cache[ck][0]
        else:
            tier = 1.0
            _AG = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           '..', '..', 'app', 'backend', 'config', 'app.json'))
            for p in (_AG, r'D:\Software\ai-girlfriend\backend\config\app.json'):
                try:
                    c = json.loads(open(p, encoding='utf-8').read())
                    m = str(c.get('speech_rate', '+0%')).replace('%', '').replace('+', '') or '0'
                    tier = 1.0 + float(m) / 100.0
                    break
                except Exception:
                    continue
            cache[ck] = [tier, now]
        # ===== 1005v3 动态语速归一化(治本) =====
        # 实测: 18080 同voice同句连读6遍, 裸时长极差30~35%(lingyun 3.52~4.96s)。
        # 静态因子(1004)只修均值修不了方差 → 成品忽快忽慢("很多文字太快/有时正常")。
        # 治法: 合成后实测本轮裸时长 raw_secs, 按目标wpm反推理想时长 desired,
        #   WSOLA rate = raw_secs/desired —— 成品时长钉死≈ words/target_wpm。
        #   用户档位tier乘在目标wpm之上(+25%=读快25%), clamp[0.55,1.30]防大变形。
        #   退化路径(无text/短词/JSON无target_wpm字段)→ 退回1004静态因子, 绝不吃哑弹。
        if text is not None and raw_secs:
            try:
                words = self._count_words(text)
                target = self._target_wpm(voice)
                if target and words >= 2:
                    desired = words / (target * tier) * 60.0
                    rate = max(0.75, min(1.25, raw_secs / desired))   # 1006: clamp收窄(0.55/1.30极端拉伸毁音色=Owen听到的"串音"体感之一)
                    return rate
            except Exception:
                pass
        return tier * self._calib_factor(voice)

    @staticmethod
    def _count_words(text):
        import re
        cjk = len(re.findall(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text))
        latin = len(re.findall(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?", text))
        return cjk + latin

    def _target_wpm(self, voice=None):
        """1005v3 逐位成品目标语速(标尺=凌风1004验收164wpm)。JSON缺字段→None=走静态因子。"""
        name = str(voice or getattr(self.opt, 'REF_FILE', '') or '').strip().lower()
        cal = self._cal_map()
        tw = cal.get('target_wpm')
        if not isinstance(tw, dict):
            return None
        try:
            return float(tw.get(name, tw.get('_default') or 0)) or None
        except Exception:
            return None

    @staticmethod
    def _cal_map():
        """校准JSON统一加载器(30s热载)。兼容新旧格式: 旧=逐位factor, 新=target_wpm+fallback。"""
        import json, os, time
        cal = getattr(AudioCppTTS, "_cal_mapobj", None)
        if cal is None or (time.time() - getattr(AudioCppTTS, "_cal_ts", 0) > 30):
            try:
                p = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 '..', 'data', 'tts_calibration.json'))
                cal = json.loads(open(p, encoding='utf-8').read())
            except Exception:
                cal = {}
            AudioCppTTS._cal_mapobj = cal
            AudioCppTTS._cal_ts = time.time()
        return cal

    def _voice_params(self, voice=None):
        """1006 逐位合成参数(seed/temperature/f0自检线), 真源=tts_calibration.json voice_params(30s热载).
        1015方案A: f0 锚以 voice_registry.json 逐师实测值为准(注册表=唯一真相源, 覆盖标定表旧值;
        旧 262.3 是凌雪专属, 锁到别人身上会压平其特色 —— per-teacher F0 锚根治)。pitch_lock 对
        注册表内所有老师默认开启(逐句锁回各自基频)。注册表不可用时静默退回旧行为(fail-open)。"""
        name = str(voice or getattr(self.opt, 'REF_FILE', '') or '').strip().lower()
        cal = self._cal_map()
        vp = cal.get('voice_params')
        if not isinstance(vp, dict):
            return {}
        d = vp.get(name) or vp.get('_default')
        d = dict(d) if isinstance(d, dict) else {}
        reg = AudioCppTTS._reg_map().get(name)
        if reg and reg.get('f0'):
            d['f0'] = float(reg['f0'])
            d.setdefault('pitch_lock', True)
        return d

    @staticmethod
    def _reg_map():
        """1015 声纹注册表(30s热载, 与_cal_map同构): voice -> {f0, speed...}。"""
        import json, os, time
        if getattr(AudioCppTTS, "_reg_obj", None) is None or (
                time.time() - getattr(AudioCppTTS, "_reg_ts", 0) > 30):
            try:
                p = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  '..', 'data', 'voice_registry.json'))
                obj = json.loads(open(p, encoding='utf-8').read())
            except Exception:
                obj = {}
            AudioCppTTS._reg_obj = obj
            AudioCppTTS._reg_ts = time.time()
        return AudioCppTTS._reg_obj

    @staticmethod
    def _f0_med(pcm, sr, ref=None):
        """纯numpy自相关基频中位数(有声帧, 强度>0.5, 八度纠偏)。估不出返回None。
        1007r4: 新增可选 ref 锚点(该音色标定基频)。自相关法在低频男声上会系统性
        锁到2倍频(旧 v>320 阈值漏纠, 如 Marcus 真值108 测出216), 导致音色自检误判
        drift → 每句白合成3次。传 ref 后逐帧折到同八度再取中位数; ref=None 行为不变。"""
        if sr != 16000 and len(pcm) > 0:   # 简单线性重采样到16k(只为估F0)
            idx = np.arange(0, len(pcm), sr / 16000.0)
            pcm = np.interp(idx, np.arange(len(pcm)), pcm).astype(np.float64)
            sr = 16000
        win, hop = int(0.08 * sr), int(0.04 * sr)   # 1015f0v2: 40ms窗在<125Hz有假低频峰(ref_f0 v1曾把kai锚成119/kate锚成190, 差2~3半音); 80ms可到60Hz仍有2周期
        f0s = []
        for s in range(0, len(pcm) - win, hop):
            f = pcm[s:s + win].astype(np.float64)
            if np.sqrt(np.mean(f ** 2)) < 0.01:
                continue
            f = f - f.mean()
            ac = np.correlate(f, f, 'full')[len(f) - 1:]
            if ac[0] <= 0:
                continue
            lo, hi = int(sr / 400), int(sr / 60)
            bv = None; bs = -1.0
            for i in range(lo, min(hi, len(ac) - 1)):
                if ac[i] / ac[0] <= 0.25:
                    continue
                if ac[i] < ac[max(lo, i - 2):min(hi, i + 3)].max() * 0.98:
                    continue        # 局部峰(容忍平台)
                sc = ac[i] * (i / lo) ** 0.5      # 长周期优先: 压低频假第一峰, 抬真基频谐波
                if sc > bs:
                    bs, bv = sc, sr / float(i)
            if bv is not None:
                v = bv
                if ref:
                    # 1007r4 八度折叠: 把 v 折到与 ref 同八度(阈值 sqrt2)
                    for _ in range(3):
                        if v > ref * 1.41:
                            v /= 2.0
                        elif v < ref * 0.71:
                            v *= 2.0
                        else:
                            break
                f0s.append(v)
        return float(np.median(f0s)) if f0s else None

    def _voice_ok(self, wav_bytes, vp):
        """1006 音色自检: 合成wav的F0中位数须落在 voice_params.f0 的 tol(对数比) 内。
        无f0配置/估计失败→放行(绝不吃哑弹)。返回 (ok, f0)。"""
        f0ref = vp.get('f0')
        if not f0ref:
            return True, 0.0
        try:
            import io as _io
            y, sr = sf.read(_io.BytesIO(wav_bytes))
            y = np.asarray(y, dtype=np.float64)
            if y.ndim > 1:
                y = y[:, 0]
            med = self._f0_med(y, sr, ref=float(f0ref))   # 1007r4: 传ref做八度折叠, 消除误判drift
            if not med:
                return True, 0.0
            tol = float(vp.get('tol', 0.15))
            import math
            return abs(math.log(med / float(f0ref))) <= tol, med
        except Exception:
            return True, 0.0

    def _calib_factor(self, voice=None):
        """1004静态因子(现作fallback: 新JSON的_fallback_factor子表, 兼容旧JSON顶层键)。"""
        name = str(voice or getattr(self.opt, 'REF_FILE', '') or '').strip().lower()
        cal = self._cal_map()
        fb = cal.get('_fallback_factor')
        if not isinstance(fb, dict):
            fb = cal
        try:
            return float(fb.get(name, fb.get('_default', 1.0)))
        except Exception:
            return 1.0
