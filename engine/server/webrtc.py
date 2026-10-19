###############################################################################
#  Copyright (C) 2024 LiveTalking@lipku https://github.com/lipku/LiveTalking
#  email: lipku@foxmail.com
# 
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#  
#       http://www.apache.org/licenses/LICENSE-2.0
# 
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
###############################################################################

import asyncio
import json
import logging
import threading
import time
from typing import Tuple, Dict, Optional, Set, Union
import queue
from av.frame import Frame
from av.packet import Packet
from av import AudioFrame
import fractions
import numpy as np

AUDIO_PTIME = 0.020  # 20ms audio packetization
VIDEO_CLOCK_RATE = 90000
VIDEO_PTIME = 0.040 #1 / 25  # 30fps
VIDEO_TIME_BASE = fractions.Fraction(1, VIDEO_CLOCK_RATE)
SAMPLE_RATE = 16000
AUDIO_TIME_BASE = fractions.Fraction(1, SAMPLE_RATE)

#from aiortc.contrib.media import MediaPlayer, MediaRelay
#from aiortc.rtcrtpsender import RTCRtpSender
from aiortc import (
    MediaStreamTrack,
)

logging.basicConfig()
logger = logging.getLogger(__name__)
from utils.logger import logger as mylogger


class PlayerStreamTrack(MediaStreamTrack):
    """
    A video track that returns an animated flag.
    """

    def __init__(self, player, kind):
        super().__init__()  # don't forget this!
        self.kind = kind
        self._player = player
        self._queue = queue.Queue(maxsize=100)
        self.timelist = [] #记录最近包的时间戳
        self.current_frame_count = 0
        if self.kind == 'video':
            self.framecount = 0
            self.lasttime = time.perf_counter()
            self.totaltime = 0
    
    _start: float
    _timestamp: int

    async def _pace(self, ptime: float):
        """1019fix 发送节拍锚定在"上一帧发送时刻", 恒定 ptime 间隔发送。

        旧实现: wait = self._start + current_frame_count*ptime - time.time()
        当通话空闲(TTS 未出声 / 队列空)时 recv() 会空转等待, current_frame_count 停住,
        而墙钟继续走 -> 恢复时 wait 变成很大的负数 -> 不 sleep -> 以最大速度狂吐积压帧,
        形成为期数百 ms 的突发(实测 16 帧/50ms)。远端 jitter buffer 按 PTS 消费不过来,
        只能丢帧 -> 客户端音频出现 0.6~1.5s 空洞。

        现场取证(1019 环回探针, 每轮 24s): 洞总时长 4.8s -> 11.1s -> 11.2s 逐轮恶化,
        packetsLost=0(包都到了, 只是时间错乱), jitter audio=1301/video=5786。
        这正是"通话过了几轮就开始卡顿"。

        改为 _last_send 锚定后: 发送间隔恒为 ptime; 渲染慢于 ptime 时自然降到渲染速度
        (recv 里队列空转等待); 空闲恢复时只立即补发一帧再回到节拍, 不再突发。
        PTS 仍按原逻辑单调递增, 客户端播放语义不变。
        """
        last = getattr(self, "_last_send", None)
        if last is not None:
            gap = time.time() - last
            if gap < ptime:
                await asyncio.sleep(ptime - gap)
        self._last_send = time.time()

    async def next_timestamp(self) -> Tuple[int, fractions.Fraction]:
        if self.readyState != "live":
            raise Exception

        if self.kind == 'video':
            if hasattr(self, "_timestamp"):
                self._timestamp += int(VIDEO_PTIME * VIDEO_CLOCK_RATE)
                self.current_frame_count += 1
                await self._pace(VIDEO_PTIME)   # 1019fix: 恒定节拍(见 _pace 注释)
                # if len(self.timelist)>=100:
                #     self.timelist.pop(0)
                # self.timelist.append(time.time())
            else:
                self._start = time.time()
                self._timestamp = 0
                self.timelist.append(self._start)
                mylogger.info('video start:%f',self._start)
            return self._timestamp, VIDEO_TIME_BASE
        else: #audio
            if hasattr(self, "_timestamp"):
                self._timestamp += int(AUDIO_PTIME * SAMPLE_RATE)
                self.current_frame_count += 1
                await self._pace(AUDIO_PTIME)   # 1019fix: 恒定节拍(见 _pace 注释)
                # if len(self.timelist)>=200:
                #     self.timelist.pop(0)
                #     self.timelist.pop(0)
                # self.timelist.append(time.time())
            else:
                self._start = time.time()
                self._timestamp = 0
                self.timelist.append(self._start)
                mylogger.info('audio start:%f',self._start)
            return self._timestamp, AUDIO_TIME_BASE

    async def recv(self) -> Union[Frame, Packet]:
        # frame = self.frames[self.counter % 30]            
        self._player._start(self)
        # if self.kind == 'video':
        #     frame = await self._queue.get()
        # else: #audio
        #     if hasattr(self, "_timestamp"):
        #         wait = self._start + self._timestamp / SAMPLE_RATE + AUDIO_PTIME - time.time()
        #         if wait>0:
        #             await asyncio.sleep(wait)
        #         if self._queue.qsize()<1:
        #             #frame = AudioFrame(format='s16', layout='mono', samples=320)
        #             audio = np.zeros((1, 320), dtype=np.int16)
        #             frame = AudioFrame.from_ndarray(audio, layout='mono', format='s16')
        #             frame.sample_rate=16000
        #         else:
        #             frame = await self._queue.get()
        #     else:
        while True:
            try:
                frame, eventpoint = self._queue.get_nowait()
                break
            except queue.Empty:
                await asyncio.sleep(0.005)

        pts, time_base = await self.next_timestamp()
        frame.pts = pts
        frame.time_base = time_base
        if eventpoint and self._player is not None:
            self._player.notify(eventpoint)
        if frame is None:
            self.stop()
            raise Exception
        if self.kind == 'video':
            self.totaltime += (time.perf_counter() - self.lasttime)
            self.framecount += 1
            self.lasttime = time.perf_counter()
            if self.framecount==100:
                mylogger.info(f"------actual avg final fps:{self.framecount/self.totaltime:.4f}")
                self.framecount = 0
                self.totaltime=0
        return frame
    
    def stop(self):
        super().stop()
        # Drain & delete remaining frames
        while not self._queue.empty():
            item = self._queue.get_nowait()
            del item
        if self._player is not None:
            self._player._stop(self)
            self._player = None

def player_worker_thread(
    quit_event,
    container
):
    container.render(quit_event)

class HumanPlayer:

    def __init__(
        self, avatar_session, format=None, options=None, timeout=None, loop=False, decode=True
    ):
        self.__thread: Optional[threading.Thread] = None
        self.__thread_quit: Optional[threading.Event] = None

        # examine streams
        self.__started: Set[PlayerStreamTrack] = set()
        self.__audio: Optional[PlayerStreamTrack] = None
        self.__video: Optional[PlayerStreamTrack] = None

        self.__audio = PlayerStreamTrack(self, kind="audio")
        self.__video = PlayerStreamTrack(self, kind="video")

        self.__container = avatar_session
        if hasattr(self.__container, 'output'):
            self.__container.output._player = self

    def push_video(self, frame, keep=8):  # 1019: 容纳批次突发(8帧)避免25%丢帧
        """1013q 视频积压丢旧帧(治"话音落了嘴还在动>3s"):
        旧码无条件入队, 队列 maxsize=100 ≈ 4s。发送侧(25fps 节奏 + VP8 编码)慢于渲染侧时
        积压越攒越多 → 客户端看到的画面落后音频数秒 → 说话结束音频早停了, 队列里的"说话帧"
        还要放几秒才播完(嘴一直动), 之后才进 idle。只保留最新 keep 帧(≈160ms 抖动垫),
        其余丢弃: 画面永远最新, 音画漂移不累积。"""
        from av import VideoFrame
        new_frame = VideoFrame.from_ndarray(frame, format="bgr24")
        q = self.__video._queue
        dropped = 0
        try:
            while q.qsize() > keep:
                q.get_nowait()
                dropped += 1
        except queue.Empty:
            pass
        try:
            q.put((new_frame, None))
        except queue.Full:
            pass
        # 秒级遥测: 丢帧数/积压(证明是否真存在积压, 修前 vs 修后)
        self._vpush = getattr(self, '_vpush', 0) + 1
        self._vdrop = getattr(self, '_vdrop', 0) + dropped
        now = time.time()
        if now - getattr(self, '_vlog_t', 0.0) >= 10.0:
            self._vlog_t = now
            try:
                mylogger.info('video queue: push=%d dropped=%d vb=%d ab=%d',
                              self._vpush, self._vdrop, self.__video._queue.qsize(),
                              self.__audio._queue.qsize())
            except Exception:
                pass

    def push_audio(self, frame, eventpoint=None):
        from av import AudioFrame
        new_frame = AudioFrame(format='s16', layout='mono', samples=frame.shape[0])
        new_frame.planes[0].update(frame.tobytes())
        new_frame.sample_rate = 16000
        self.__audio._queue.put((new_frame, eventpoint))

    def get_buffer_size(self) -> int:
        return self.__video._queue.qsize()

    def get_audio_buffer_size(self) -> int:
        """1013q 遥测: 音频队列积压 (与视频队列同结构, 谁积压一目了然)"""
        return self.__audio._queue.qsize()

    def notify(self,eventpoint):
        if self.__container is not None:
            self.__container.notify(eventpoint)

    @property
    def audio(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains audio.
        """
        return self.__audio

    @property
    def video(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains video.
        """
        return self.__video

    def _start(self, track: PlayerStreamTrack) -> None:
        self.__started.add(track)
        if self.__thread is None:
            self.__log_debug("Starting worker thread")
            self.__thread_quit = threading.Event()
            self.__thread = threading.Thread(
                name="media-player",
                target=player_worker_thread,
                args=(
                    self.__thread_quit,
                    self.__container
                ),
            )
            self.__thread.start()

    def _stop(self, track: PlayerStreamTrack) -> None:
        self.__started.discard(track)

        if not self.__started and self.__thread is not None:
            self.__log_debug("Stopping worker thread")
            self.__thread_quit.set()
            self.__thread.join()
            self.__thread = None

        if not self.__started and self.__container is not None:
            #self.__container.close()
            self.__container = None

    def __log_debug(self, msg: str, *args) -> None:
        mylogger.debug(f"HumanPlayer {msg}", *args)
