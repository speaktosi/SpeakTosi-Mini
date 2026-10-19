###############################################################################
#  WebRTC 连接管理 + RTC 音频/视频接收
###############################################################################

import json
import asyncio
import random
import copy
from typing import Dict, Optional
import queue

from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceServer, RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender

from utils.logger import logger

# 1004(画面糊的真凶): aiortc 的 H264 默认 DEFAULT_BITRATE=1Mbps / MAX=3Mbps / profile=Baseline。
# 704x896@25fps 跑 1Mbps 严重欠码率 → 高频细节被编码器丢掉:
#   实测离线把嘴锐化到"源图101%", 过一遍WebRTC采集端只剩52%。
# 本链路是 loopback(引擎↔Electron同机), 带宽无限, 没有任何理由省码率。
# target_bitrate setter 里有 max(MIN, min(x, MAX)) 钳制 → 抬高 MIN_BITRATE 即可无视 REMB 压制。
# (不改 site-packages 源文件: 库升级不丢, 引擎重启即生效, 回滚=删本段)
from aiortc.codecs import h264 as _h264_mod
_H264_MIN = 12_000_000      # 12 Mbps 下限
_h264_mod.DEFAULT_BITRATE = _H264_MIN
_h264_mod.MIN_BITRATE = _H264_MIN
_h264_mod.MAX_BITRATE = 24_000_000
logger.info(f"[1004] aiortc H264 码率下限提至 {_H264_MIN//1_000_000}Mbps (原1Mbps)")


# def _rand_session_id(n: int = 6) -> int:
#     """生成 N 位随机 session ID"""
#     return random.randint(10 ** (n - 1), 10 ** n - 1)


from server.session_manager import session_manager
from server.session_manager import MaxSessionError

class RTCManager:
    """
    WebRTC 连接管理器。
    
    管理 PeerConnection 生命周期、音视频轨道收发、DataChannel。
    """

    def __init__(self, opt):
        """
        Args:
            opt: 全局配置
        """
        self.opt = opt
        self.pcs: set = set()

    async def _create_pc_and_answer(self, avatar_session, sessionid, offer):
        """创建 PeerConnection、添加轨道、SDP 交换，返回已完成 answer 的 pc"""
        ice_server = RTCIceServer(urls=self.opt.stun)
        pc = RTCPeerConnection(
            configuration=RTCConfiguration(iceServers=[ice_server])
        )
        self.pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info("Connection state is %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                await pc.close()
                self.pcs.discard(pc)
                session_manager.remove_session(sessionid)
            elif pc.connectionState == "disconnected":
                # 1006: 僵尸会话兜底回收 — 客户端异常断开(DTLS close丢失/直接杀进程)时,
                # 状态停在disconnected永不回收 → 占满max_session新会话全被拒(1006 Brenda连接失败二因)。
                # 给15s自愈窗口(网络抖动可回connected), 未恢复即回收。
                async def _reap_later():
                    await asyncio.sleep(15)
                    if pc.connectionState in ("disconnected", "failed", "closed"):
                        logger.warning(f"reap zombie session {sessionid} (state={pc.connectionState})")
                        try:
                            await pc.close()
                        except Exception:
                            pass
                        self.pcs.discard(pc)
                        session_manager.remove_session(sessionid)
                asyncio.ensure_future(_reap_later())

        # 添加发送轨道
        from server.webrtc import HumanPlayer
        player = HumanPlayer(avatar_session)
        pc.addTrack(player.audio)
        pc.addTrack(player.video)

        # 设置编解码器偏好
        capabilities = RTCRtpSender.getCapabilities("video")
        preferences = list(filter(lambda x: x.name == "H264", capabilities.codecs))
        preferences += list(filter(lambda x: x.name == "VP8", capabilities.codecs))
        preferences += list(filter(lambda x: x.name == "rtx", capabilities.codecs))
        transceiver = pc.getTransceivers()[1]
        transceiver.setCodecPreferences(preferences)

        await pc.setRemoteDescription(offer)
        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        return pc

    async def handle_offer(self, request):
        """处理 WebRTC offer 信令"""
        params = await request.json()
        offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

        try:
            sessionid = await session_manager.create_session(params)
        except MaxSessionError as e:
            logger.warning("Rejecting offer: %s", e)
            return web.Response(
                content_type="application/json",
                text=json.dumps({"code": -1, "msg": str(e)}),
            )
        logger.info('offer sessionid=%s', sessionid)

        pc = await self._create_pc_and_answer(
            session_manager.get_session(sessionid), sessionid, offer
        )

        return web.Response(
            content_type="application/json",
            text=json.dumps({
                "sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type,
                "sessionid": sessionid,
            }),
        )

    async def handle_whep(self, request):
        """
        处理 WHEP 信令（WebRTC HTTP Egress Protocol）

        - 请求 body 为裸 SDP offer（Content-Type: application/sdp）
        - 扩展参数通过 query string 传入（avatar, tts, tts_server 等）
        - 返回 SDP answer（Content-Type: application/sdp）
        - sessionid 通过 X-Session-ID 响应头返回
        """
        params = dict(request.query)
        # 客户端可通过 query param 自定义 sessionid，不传则自动生成
        client_sid = params.pop("sessionid", None)

        offer_sdp = await request.text()
        offer = RTCSessionDescription(sdp=offer_sdp, type="offer")

        try:
            sessionid = await session_manager.create_session(params, sessionid=client_sid)
        except MaxSessionError as e:
            logger.warning("Rejecting whep: %s", e)
            return web.Response(
                status=503,
                content_type="text/plain",
                text=str(e),
            )
        logger.info("whep sessionid=%s", sessionid)

        pc = await self._create_pc_and_answer(
            session_manager.get_session(sessionid), sessionid, offer
        )

        return web.Response(
            status=201,
            content_type="application/sdp",
            text=pc.localDescription.sdp,
            headers={"X-Session-ID": sessionid},
        )

    async def handle_rtcpush(self, push_url, sessionid: str):
        """RTCPush 模式：主动推流"""
        import aiohttp
        await session_manager.create_session({}, sessionid)
        avatar_session = session_manager.get_session(sessionid)

        pc = RTCPeerConnection()
        self.pcs.add(pc)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info("Connection state is %s", pc.connectionState)
            if pc.connectionState == "failed":
                await pc.close()
                self.pcs.discard(pc)

        from server.webrtc import HumanPlayer
        player = HumanPlayer(avatar_session)
        pc.addTrack(player.audio)
        pc.addTrack(player.video)

        await pc.setLocalDescription(await pc.createOffer())

        async with aiohttp.ClientSession() as session:
            async with session.post(push_url, data=pc.localDescription.sdp) as response:
                answer_sdp = await response.text()

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer_sdp, type='answer')
        )

    async def shutdown(self):
        """关闭所有 PeerConnection"""
        coros = [pc.close() for pc in self.pcs]
        await asyncio.gather(*coros)
        self.pcs.clear()
