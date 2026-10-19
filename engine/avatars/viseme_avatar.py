# -*- coding: utf-8 -*-
###############################################################################
# Viseme 数字人 — 可控嘴型图层替换, 不走 MuseTalk 生成
# 链路: TTS整句音频 → pypinyin声韵 → viseme时间轴(句内精确缩放) → idle帧+嘴部sprite合成
# 原则: 只替换嘴部小矩形(9张全画布viseme图, 嘴矩形恒定), 眼/visor/身体=idle动画原帧
###############################################################################
import os
import re
import time
import queue
from queue import Queue
from threading import Thread, Event

import numpy as np
import cv2
from pypinyin import pinyin, Style

from avatars.base_avatar import BaseAvatar
from utils.logger import logger
from utils.image import read_imgs, mirror_index
from registry import register

SR = 16000
CHUNK = 320                    # 20ms
VIS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       '..', 'avatar', 'viseme')
VIS_DIR = os.path.normpath(VIS_DIR)
MOUTH_RECT = (330, 344, 384, 392)          # x0,y0,x1,y1 (全画布, 9图对齐; 凌风标定=默认)
# 1005 P0: per-avatar 嘴锚点(DST粘贴rect) —— 共用rect是按凌风标的, 凌云嘴实测偏移(-20,+28)
# → 口型被贴到鼻/上唇(线上bug, 每句话都错位)。SRC提取坐标不变, 只改DST; 未登记avatar像素级不变。
# 标定方法: HSV红粉检测口腔质心(跨10帧 σ0.0/1.7), rect=质心锚定54x48再并自身嘴bbox扩边。
MOUTH_DST = {
    'lingyun_cartoon': (308, 371, 368, 420),   # 质心(336.6,395.7); 60x49含下巴安全扩边
    # 1013: 凌雪 rect 与 MOUTH_RECT 完全同坐标(几何像素级不变), 登记目的=启用羽化3px+白壳保护,
    #       否则硬贴方块边(1005 凌云同源问题)。
    'lingxue_cartoon': (330, 344, 384, 392),
}
# 1013: 这些 avatar 的 idle 素材自带嘴部动作(生成残留)——静音期把嘴区锁成 REST,
# 否则"话音说完→切 idle 原帧"时嘴还在动(Owen 1013 报告)。未登记 avatar 行为像素级不变。
REST_LOCK_AVATARS = {'lingxue_cartoon'}
CHAR_DUR = 0.165               # 中文TTS近似等时(实测5.04s/29字≈0.174)
PAUSE_DUR = {'，': 0.18, '。': 0.32, '？': 0.26, '！': 0.26,
             '、': 0.12, '；': 0.18, ':': 0.12, '.': 0.2, '?': 0.26, '!': 0.26}
FADE_S = 0.04                  # viseme间交叉淡化(秒)

INIT_MAP = {'zh': 'CHSHJ', 'ch': 'CHSHJ', 'sh': 'CHSHJ', 'j': 'CHSHJ', 'q': 'CHSHJ',
            'x': 'E', 'b': 'MBP', 'p': 'MBP', 'm': 'MBP', 'f': 'FV', 'w': 'UW', 'r': 'O',
            'd': 'E', 't': 'E', 'n': 'E', 'l': 'L', 'g': 'AI', 'k': 'AI', 'h': 'AI',
            'z': 'E', 'c': 'E', 's': 'E', 'y': 'E'}
FIN_MAP = {'a': 'AI', 'ai': 'AI', 'ao': 'AI', 'ia': 'AI', 'ua': 'AI', 'uai': 'AI',
           'an': 'AI', 'ang': 'AI', 'iang': 'AI', 'uan': 'AI', 'uang': 'AI',
           'o': 'O', 'ong': 'O', 'uo': 'O', 'iong': 'O', 'ou': 'O',
           'e': 'E', 'ei': 'E', 'en': 'E', 'eng': 'E', 'ie': 'E', 'üe': 'E', 'er': 'E',
           'i': 'E', 'in': 'E', 'ing': 'E', 'iv': 'E', 'v': 'E', 'ia': 'AI', 'ian': 'AI',
           'iao': 'AI', 'iu': 'UW', 'ui': 'UW', 'un': 'UW', 'u': 'UW', 'ue': 'UW', 'ü': 'UW',
           've': 'UW', 'van': 'UW', 'vang': 'AI', 'ueng': 'O'}


# ---------- 1004: 英文音素路径(CMUdict → ARPAbet → viseme) ----------
# 根因: 原实现只认CJK, 英语学习老师的回复大量英文 → plan空 → 嘴完全不动
ENGINE_DATA = os.path.normpath(os.path.join(VIS_DIR, '..', '..', 'engine', 'data'))
ARP_MAP = {'AA': 'AI', 'AE': 'AI', 'AH': 'AI', 'AO': 'O', 'AW': 'UW', 'AY': 'AI',
           'EH': 'E', 'ER': 'E', 'EY': 'E', 'IH': 'E', 'IY': 'E', 'OW': 'O', 'OY': 'O',
           'UH': 'UW', 'UW': 'UW',
           'B': 'MBP', 'P': 'MBP', 'M': 'MBP', 'F': 'FV', 'V': 'FV',
           'CH': 'CHSHJ', 'JH': 'CHSHJ', 'SH': 'CHSHJ', 'TH': 'E', 'DH': 'E',
           'HH': 'AI', 'K': 'AI', 'G': 'AI', 'T': 'E', 'D': 'E', 'N': 'E',
           'L': 'L', 'R': 'O', 'S': 'E', 'Z': 'E', 'W': 'UW', 'Y': 'E',
           'NG': 'E'}
_ARP_VOWEL = set('AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW'.split())
_CONS_DUR, _VOW_DUR, _WORD_GAP = 0.055, 0.15, 0.04
_cmu = None


def _cmu_dict():
    global _cmu
    if _cmu is None:
        _cmu = {}
        try:
            for line in open(os.path.join(ENGINE_DATA, 'cmudict.txt'),
                             encoding='utf-8', errors='ignore'):
                if not line or line[0] == ';':
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                w = parts[0].split('(')[0].lower()
                if w not in _cmu:
                    _cmu[w] = parts[1:]
        except Exception as e:
            logger.warning(f'cmudict load fail: {e}')
    return _cmu


def _word_phonemes(word):
    """英文词 → ARPAbet列表(去重音)。词典没有则按字母规则兜底。"""
    ph = _cmu_dict().get(word)
    if ph:
        return [p.rstrip('0123456789') for p in ph]
    out = []
    for ch in word:
        if ch in 'aeiouy':
            out.append({'a': 'AA', 'e': 'EH', 'i': 'IH', 'o': 'AO',
                        'u': 'UH', 'y': 'IH'}[ch])
        else:
            out.append({'b': 'B', 'p': 'P', 'm': 'M', 'f': 'F', 'v': 'V', 'w': 'W',
                        'r': 'R', 'l': 'L', 'h': 'HH', 'g': 'G', 'k': 'K',
                        'c': 'S', 'j': 'JH', 'q': 'K', 'x': 'K', 'n': 'N',
                        's': 'S', 'z': 'Z', 't': 'T', 'd': 'D', 'y': 'Y'}.get(ch, 'T'))
    return out


def _english_dur_visemes(word):
    """词 → [(viseme, 秒)]; 辅音连缀合并(只取首个), 避免碎跳"""
    ph = _word_phonemes(word.lower())
    seq = []
    prev_v = False
    for p in ph:
        isv = p in _ARP_VOWEL
        v = ARP_MAP.get(p)
        if v is None:
            continue
        if not isv and prev_v is False and seq:
            continue                    # 连缀辅音并入前一段
        seq.append((v, _VOW_DUR if isv else _CONS_DUR))
        prev_v = isv
    return seq


def build_sentence_plan(text):
    """整句(中/英/混杂) → [(t0,t1,viseme)] 相对秒(未缩放, 渲染端整体缩放到真实音频时长)"""
    text = text or ''
    segs = []
    t = 0.0
    for tk in re.findall(r'[\u4e00-\u9fff]|[A-Za-z]+|[^\sA-Za-z\u4e00-\u9fff]|\s', text):
        c = tk[0]
        if '\u4e00' <= c <= '\u9fff':
            ini = pinyin(tk, style=Style.INITIALS, strict=False)[0][0]
            fin = pinyin(tk, style=Style.FINALS_TONE3, strict=False)[0][0]
            seq = []
            if ini in INIT_MAP:
                seq.append(INIT_MAP[ini])
            seq.append(FIN_MAP.get(fin.rstrip('0123456789'), 'AI'))
            split = 0.4 if len(seq) == 2 else 1.0
            for k, v in enumerate(seq):
                d = CHAR_DUR * (split if k == 0 else (1 - split))
                segs.append((t, t + d, v))
                t += d
        elif tk.isalpha():
            for v, d in _english_dur_visemes(tk):
                segs.append((t, t + d, v))
                t += d
            t += _WORD_GAP
        elif c in PAUSE_DUR:
            t += PAUSE_DUR[c]
        # 空格等不占时长
    return segs, t


@register("avatar", "viseme")
class VisemeReal(BaseAvatar):
    """renderer=viseme 会话: 完全绕开 MuseTalk 推理与 whisper 特征管线"""

    def __init__(self, opt, model, avatar):
        super().__init__(opt)
        self.mouth_imgs = {}
        for name in ['REST', 'MBP', 'AI', 'E', 'O', 'UW', 'FV', 'L', 'CHSHJ']:
            p = os.path.join(VIS_DIR, name + '.png')
            img = cv2.imread(p, cv2.IMREAD_UNCHANGED)
            if img is None:
                raise RuntimeError(f'viseme素材缺失: {p}')
            self.mouth_imgs[name] = img
        x0, y0, x1, y1 = MOUTH_RECT
        self.mr = (x0, y0, x1, y1)
        # 嘴区预合成底: viseme图自带visor黑底, alpha在嘴区为255 → 直接取RGB
        self.mouth_rgb = {k: v[y0:y1, x0:x1, :3].copy() for k, v in self.mouth_imgs.items()}
        # 1005 P0: per-avatar 嘴锚点 — DST偏移+缩放+羽化alpha(3px坡, 防平涂黑vs渐变黑硬边)
        # 只有登记在 MOUTH_DST 的 avatar 走此路径; 其余三位(含凌风)行为像素级不变(回归保证)。
        av_id = str(getattr(opt, 'avatar_id', '') or '')
        dst = MOUTH_DST.get(av_id)
        self.dst_alpha = None
        if dst:
            dx0, dy0, dx1, dy1 = dst
            dw, dh = dx1 - dx0, dy1 - dy0
            sw, sh = x1 - x0, y1 - y0
            if (dw, dh) != (sw, sh):
                self.mouth_rgb = {k: cv2.resize(v, (dw, dh), interpolation=cv2.INTER_LANCZOS4)
                                  for k, v in self.mouth_rgb.items()}
            self.mr = (dx0, dy0, dx1, dy1)
            a = np.ones((dh, dw), np.float32)
            fe = 3
            ramp = np.linspace(0, 1, fe + 2, dtype=np.float32)[1:-1]
            a[:fe, :] *= ramp[:, None]; a[-fe:, :] *= ramp[::-1, None]
            a[:, :fe] *= ramp[None, :]; a[:, -fe:] *= ramp[::-1][None, :]
            self.dst_alpha = a[:, :, None]
            logger.info(f'viseme mouth anchor override: {av_id} dst={dst} size={dw}x{dh} feather=3px')
        self.mouth_rgb_f = {k: a.astype(np.float32) for k, a in self.mouth_rgb.items()}
        self.est_dur = None      # 当前句估算时长(秒)
        self.sentence_q = Queue(8)
        self._cur_segs, self._cur_t0 = None, 0.0
        self.idle_key = next(iter(self.custom_img_cycle), None) if self.custom_img_cycle else None
        if self.idle_key is None:
            raise RuntimeError('viseme renderer 需要 idle 序列(customopt)')
        self.idle_len = len(self.custom_img_cycle[self.idle_key])
        self.idle_idx = 0
        # 1019 说话期"身体抖动"修复: idle 序列整幅有 ±2px 上下呼吸位移, 说话时序列被
        # 非等间隔推进(渲染耗时/音频补块), 呼吸变成肉眼可见的抖动。说话期改用固定
        # 参考帧作底(第0帧, 与 dst_prot 标定帧一致) → 身体完全静止, 只合成嘴部;
        # idle 分支仍照旧播放整段序列, 待机呼吸感不变。
        self.idle_ref = self.custom_img_cycle[self.idle_key][0]
        # 1005 P0: 白壳保护 — DST rect 下缘可能覆盖凌云自己的下巴白壳(rect内白壳7.1%),
        # 粘贴时这些像素强制保留 idle 原值(白壳=静态头壳, idle循环各帧一致, 取首帧标定即可)。
        self.dst_prot = None
        if dst:
            f0 = self.custom_img_cycle[self.idle_key][0]
            reg = f0[dy0:dy1, dx0:dx1]
            hsv = cv2.cvtColor(reg, cv2.COLOR_BGR2HSV)
            prot = cv2.inRange(hsv, (0, 0, 180), (180, 40, 255)) > 0
            if prot.any():
                self.dst_prot = prot
                logger.info(f'viseme white-shell protect: {int(prot.sum())} px in dst rect')
        self._buf = []           # 当前句音频 chunk 累积
        self._buf_text = ''
        self._pending = None     # (stream, pos, text, real)
        self._rest_lock = str(getattr(opt, 'avatar_id', '') or '') in REST_LOCK_AVATARS
        if self._rest_lock:
            logger.info('idle mouth REST-lock ON (1013 mouth fix): %s' % av_id)
        logger.info(f'viseme avatar ready: {len(self.mouth_rgb)} sprites, idle_len={self.idle_len}')

    def _bufq(self, kind='v'):
        """1015fix: 1013q 遥测调的 self._bufq 从未定义 → idle 日志行 AttributeError 炸死
        media-player 渲染线程(切老师后不打招呼/画面卡死的根因)。安全读 WebRTC 队列积压。"""
        try:
            out = getattr(self, 'output', None)
            if out is None:
                return -1
            fn = (out.get_audio_buffer_size if kind == 'a' else out.get_buffer_size)
            return fn()
        except Exception:
            return -1

    # ---------- 接管音频入口: 不再进 asr, 攒整句 ----------
    def put_audio_frame(self, audio_chunk, datainfo=None):
        datainfo = datainfo or {}
        self._buf.append(np.asarray(audio_chunk, dtype=np.float32))
        st = datainfo.get('status')
        if st == 'start':
            self._buf_text = datainfo.get('text', '')
        elif st == 'end':
            stream = np.concatenate(self._buf) if self._buf else None
            self._buf = []
            if stream is not None and stream.size:
                try:
                    self.sentence_q.put((stream, self._buf_text), timeout=2)
                except Exception:
                    logger.warning('viseme sentence queue full')
        elif not self._buf and self.custom_audiotype:  # 静音期兜底
            pass

    def put_audio_frames_bulk(self, stream, text=''):
        """离线直灌(自测用)"""
        self.sentence_q.put((np.asarray(stream, dtype=np.float32), text))

    def flush_talk(self):
        super().flush_talk()
        self._buf = []
        while not self.sentence_q.empty():
            try:
                self.sentence_q.get_nowait()
            except queue.Empty:
                break

    # ---------- 时间轴 ----------
    def _pick_viseme(self, t):
        """t=句内相对秒 → (prev_v, cur_v, w): 渲染 = prev*(1-w)+cur*w
        相邻viseme之间交叉淡化(嘴连贯), 只在句首从REST淡入、句尾淡出REST"""
        segs = self._cur_segs
        if not segs:
            return 'REST', 'REST', 1.0
        for i, (t0, t1, v) in enumerate(segs):
            if t0 <= t < t1:
                f = min(FADE_S, (t1 - t0) * 0.6)
                if t - t0 < f and f > 1e-6:
                    pv = segs[i - 1][2] if i > 0 else 'REST'
                    return pv, v, (t - t0) / f
                return v, v, 1.0
        # 段间隙(标点静音/尾部): 淡出到REST
        last = segs[-1]
        if last[2] != 'REST' and t > last[1]:
            w = min(1.0, (t - last[1]) / FADE_S)
            return last[2], 'REST', w
        return 'REST', 'REST', 1.0

    def _draw_mouth(self, base, rgb):
        """把嘴部 rgb 合成到 base(原地)。说话=viseme sprite, 静音/说话结束=REST"""
        x0, y0, x1, y1 = self.mr
        if self.dst_alpha is not None:
            roi = base[y0:y1, x0:x1].astype(np.float32)
            base[y0:y1, x0:x1] = (rgb * self.dst_alpha + roi * (1 - self.dst_alpha)).astype(np.uint8)
            if self.dst_prot is not None:
                base[y0:y1, x0:x1][self.dst_prot] = roi.astype(np.uint8)[self.dst_prot]
        else:
            base[y0:y1, x0:x1] = rgb.astype(np.uint8)

    # ---------- 主循环(自管: 音频攒句 + 视频25fps节拍) ----------
    def render(self, quit_event):
        self.quit_event = quit_event
        self.init_customindex()
        self.tts.render(quit_event)
        self.output.start()

        fps = self.opt.fps
        period = 1.0 / fps
        next_tick = time.perf_counter()
        self.speaking = False
        while not quit_event.is_set():
            if self._pending is None and not self.sentence_q.empty():
                stream, text = self.sentence_q.get_nowait()
                segs, est = build_sentence_plan(text or '')
                real = len(stream) / SR
                scale = real / max(est, 1e-6) if est > 0 else 1.0
                self._cur_segs = [(t0 * scale, t1 * scale, v) for (t0, t1, v) in segs]
                self._pending = [stream, 0, text, real]
                self.speaking = True
                self.notify({'status': 'start', 'text': text})

            if self._pending is not None:
                stream, pos, text, real = self._pending
                # 本帧消费 40ms 音频(两chunk), 尾不足补零 → 定长推送
                # 1013掉词fix(墙钟喂音频): 旧码死推 640样本(40ms)/帧 —— 每帧要干嘴区羽化+合成(本身就吃
                # 30-40ms), 渲染一超 40ms 音频摄入就<实时 → 客户端 NetEq 缓冲饿死 → 补洞/吞字(Owen"掉词").
                # 现按"应喂-已喂"算 20ms 块数; 每块独立推一帧(PTS步进320=AUDIO_PTIME 不变 ✓ 音画同步保持)。
                now = time.perf_counter()
                if getattr(self, '_pt_t0', None) is None:
                    self._pt_t0, self._pt_fed_ms, self._pt_catch = now - period, 0.0, 0
                due_ms = (now - self._pt_t0) * 1000.0 - self._pt_fed_ms
                k = int(due_ms // 20.0)
                if k < 0:
                    k = 0
                elif k > 8:
                    if due_ms > 600.0:
                        self._pt_catch += 1
                    self._pt_fed_ms += (k - 8) * 20.0      # 超160ms不追, 丢弃积压
                    k = 8
                elif k > 2:
                    self._pt_catch += 1
                n = CHUNK * k
                pos0 = pos
                if n:
                    piece = stream[pos:pos + n]
                    pos += len(piece)
                    self._pending[1] = pos      # ★写回游标(1004修复: 局部pos不写回→永远重放首帧)
                    if len(piece) < n:          # 尾不足补零 → 定长
                        piece = np.concatenate([piece, np.zeros(n - len(piece), np.float32)])
                    # 1005音画同步fix(口型滞后+2x快+声糊三合一真凶): 必须按 320采样/帧 推——
                    # webrtc.py AUDIO_PTIME=0.020 每帧PTS只前进320; 一帧推640 → PTS时间轴=内容一半
                    # → 浏览器NetEq判定积压 → 自动2x加速(WSOLA压缩=快+糊), 音频先于口型结束。
                    pcm = (np.clip(piece, -1.0, 1.0) * 32767).astype(np.int16)
                    for _o in range(0, n, CHUNK):
                        self.output.push_audio_frame(pcm[_o:_o + CHUNK], {})
                    self._pt_fed_ms += k * 20.0
                frame_idx = pos0 / SR + 0.02   # 帧起点+半帧 → 口型对准本帧音频中点
                # 视频帧: 说话期固定在参考帧(身体零位移), 嘴部照常合成; idle_idx 仍推进,
                # 说话结束后待机呼吸从当前相位继续, 不会有跳变。
                self.idle_idx += 1
                base = self.idle_ref.copy()
                if frame_idx < real and self._cur_segs:
                    pv, cv, w = self._pick_viseme(frame_idx)
                    if pv == cv:
                        rgb = self.mouth_rgb_f[cv]
                    else:
                        rgb = self.mouth_rgb_f[pv] * (1 - w) + self.mouth_rgb_f[cv] * w
                elif self._rest_lock:
                    rgb = self.mouth_rgb_f['REST']   # 1013: 音频已推完→嘴立刻回 REST 并保持不动
                else:
                    rgb = None
                if rgb is not None:
                    self._draw_mouth(base, rgb)
                self.output.push_video_frame(base)
                if pos >= len(stream):
                    self.notify({'status': 'end', 'text': text})
                    try:
                        logger.info(f'viseme feed dur={real:.2f}s fed={self._pt_fed_ms / 1000.0:.2f}s '
                                    f'catch={self._pt_catch} vb={self._bufq("v")} ab={self._bufq("a")} '
                                    f'text={str(text)[:26]!r}')
                    except Exception:
                        pass
                    self._pt_t0 = None
                    self._pending = None
                    self._cur_segs = None
                    self.speaking = False
            else:
                # 静音: 推 idle 帧 + 静音音频(按fps节拍, 防洪泛缓冲)
                idle = self.custom_img_cycle[self.idle_key][mirror_index(self.idle_len, self.idle_idx)]
                self.idle_idx += 1
                base = idle.copy()
                if self._rest_lock:      # 1013: 静音期嘴区锁 REST(治"说完话嘴还在动")
                    self._draw_mouth(base, self.mouth_rgb_f['REST'])
                self.output.push_video_frame(base)
                self._idl_n = getattr(self, '_idl_n', 0) + 1
                if self._idl_n % 120 == 0:      # 1013q 遥测: 空闲期队列是否还在攒(约5s一条)
                    logger.info('viseme idle buf: vb=%d ab=%d', self._bufq('v'), self._bufq('a'))
                # 1005: idle静音同样按320/帧×2推(与说话帧一致, 防PTS错位在静音期重新累积)
                _z = np.zeros(CHUNK, np.int16)
                self.output.push_audio_frame(_z, {})
                self.output.push_audio_frame(_z, {})
                next_tick = time.perf_counter() + period
                time.sleep(period)
                continue

            # 节拍
            delay = next_tick - time.perf_counter()
            next_tick += period
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.perf_counter()
        self.output.stop()
        logger.info('viseme render thread stop')


def load_model():
    return None


def load_avatar(avatar_id):
    # viseme 分支在 app.py 直接加载 idle 序列; 此处兜底(勿删: app.py import 时取属性)
    import glob as _glob
    cv_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'data', 'customvideo', f'{avatar_id}_idle')
    files = sorted(_glob.glob(os.path.join(cv_dir, '*.png')),
                   key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    if not files:
        raise RuntimeError(f'viseme idle 缺失: {cv_dir}')
    from utils.image import read_imgs as _ri
    return _ri(files)


def warm_up(batch_size, model):
    pass
