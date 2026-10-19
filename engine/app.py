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

# server.py
from flask import Flask, render_template,send_from_directory,request, jsonify
#from flask_sockets import Sockets
import base64
import json
#import gevent
#from gevent import pywsgi
#from geventwebsocket.handler import WebSocketHandler
import re
import os
import numpy as np
from threading import Thread,Event
#import multiprocessing
import torch.multiprocessing as mp

from aiohttp import web
import aiohttp
import aiohttp_cors
from aiortc import RTCPeerConnection, RTCSessionDescription,RTCIceServer,RTCConfiguration
from aiortc.rtcrtpsender import RTCRtpSender
from server.webrtc import HumanPlayer
from avatars.base_avatar import BaseAvatar
from llm import llm_response, note_spoken
import registry
from server.routes import setup_routes
from server.rtc_manager import RTCManager
from server.session_manager import session_manager

import argparse
import random
import shutil
import asyncio
import torch
from io import BytesIO
from typing import Dict
from utils.logger import logger
from utils.image import read_imgs
import copy
import gc
from dotenv import load_dotenv


app = Flask(__name__)
#sockets = Sockets(app)
opt = None
model = None
global_avatars = {} # avatar_id: payload
        

#####webrtc###############################
# rtc_manager replaces the old pcs set and duplicate offer handlers.
rtc_manager = None

def randN(N)->int:
    '''生成长度为 N的随机数 '''
    min = pow(10, N - 1)
    max = pow(10, N)
    return random.randint(min, max - 1)

def build_avatar_session(sessionid:str, params:dict)->BaseAvatar:
    opt_this = copy.deepcopy(opt)
    opt_this.sessionid = sessionid

    avatar_id = params.get('avatar',opt.avatar_id) 
    opt_this.avatar_id = avatar_id
    ref_audio = params.get('refaudio','') #音色
    ref_text = params.get('reftext','')
    if (avatar_id and avatar_id != opt.avatar_id):
        # Avoid reloading if already cached globally
        if avatar_id not in global_avatars:
            global_avatars[avatar_id] = load_avatar(avatar_id)
        avatar_this = global_avatars[avatar_id]
    else:
        # Default avatar loaded at startup
        avatar_this = global_avatars.get(opt.avatar_id)
    if ref_audio: #请求参数配置了参考音频
        opt_this.REF_FILE = ref_audio
        opt_this.REF_TEXT = ref_text
    custom_config=params.get('custom_config','') #动作编排配置
    # 1003修复(病根): data/custom_config.json 的 idle 闭嘴视频(data/customvideo/image)是默认老师Brenda专用素材。
    # 之前无条件套给所有形象 → 静音态永远推Brenda帧, 卡通只在说话瞬间闪现("无法切换"假象)。
    # 1004升级(B路线真透明): 每形象可有自己的 idle 序列 data/custom_config_<avatar_id>.json
    # (凌风=渲染的绿幕呼吸眨眼idle, 与说话帧同画布同key色 → 静音/说话切换无缝);
    # 无专属文件时: 默认形象仍用 custom_config.json, 卡通形象回退素材包原生帧。
    if not custom_config:
        _d = os.path.dirname(os.path.abspath(__file__))
        _cc = os.path.join(_d, 'data', f'custom_config_{avatar_id}.json')
        if not os.path.exists(_cc) and avatar_id == opt.avatar_id:
            _cc = os.path.join(_d, 'data', 'custom_config.json')
        if os.path.exists(_cc):
            custom_config = open(_cc, encoding='utf-8').read().strip()
    if custom_config:
        _opts = json.loads(custom_config)
        _root = os.path.dirname(os.path.abspath(__file__))
        for item in _opts:  # 相对路径兜底：cwd 不对时按脚本目录解析
            ip = item.get('imgpath', '')
            if ip and not os.path.isabs(ip) and not os.path.exists(ip):
                item['imgpath'] = os.path.join(_root, ip)
        opt_this.customopt = _opts

    avatar_session = registry.create("avatar", opt.model, opt=opt_this, model=model, avatar=avatar_this)
    return avatar_session

async def offer(request):
    return await rtc_manager.handle_offer(request)

async def whep(request):
    return await rtc_manager.handle_whep(request)

async def on_shutdown(app):
    await rtc_manager.shutdown()

async def download_record(request):
    sessionid = request.match_info.get('sessionid')
    if not sessionid:
        return web.Response(status=400, text="sessionid is required")
    
    record_file = os.path.join('data', 'record', f"{sessionid}.mp4")
    
    if os.path.exists(record_file):
        return web.FileResponse(record_file)
    else:
        return web.Response(status=404, text="Record not found")


def main():
    global rtc_manager, opt, model,load_avatar
    # 解析命令行参数
    from config import parse_args
    opt = parse_args()

    # ─── 加载 avatar 插件（触发 @register 注册）──────────────────────
    _avatar_modules = {
        'musetalk':   'avatars.musetalk_avatar',
        'wav2lip':    'avatars.wav2lip_avatar',
        'ultralight': 'avatars.ultralight_avatar',
        'viseme':     'avatars.viseme_avatar',   # 1004 可控嘴型图层(不走生成)
    }
    import importlib
    avatar_mod = importlib.import_module(_avatar_modules[opt.model])
    load_model = avatar_mod.load_model
    load_avatar = avatar_mod.load_avatar
    warm_up = avatar_mod.warm_up
    logger.info(opt)

    if opt.model == 'musetalk':
        model = load_model()
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id) 
        warm_up(opt.batch_size,model)      
    elif opt.model == 'viseme':
        # 1004: viseme只需idle PNG序列(无 latents/coords/mask), 全分辨率直读
        import glob as _glob
        _cv = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data',
                           f'customvideo', f'{opt.avatar_id}_idle')
        _files = sorted(_glob.glob(os.path.join(_cv, '*.png')),
                        key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
        if not _files:
            raise RuntimeError(f'viseme idle 序列缺失: {_cv}')
        global_avatars[opt.avatar_id] = read_imgs(_files)
        logger.info(f'viseme idle loaded {len(_files)} frames from {_cv}')
    elif opt.model == 'wav2lip':
        model = load_model("./models/wav2lip.pth")
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,model,256)
    elif opt.model == 'ultralight':
        model = load_model(opt)
        global_avatars[opt.avatar_id] = load_avatar(opt.avatar_id)
        warm_up(opt.batch_size,global_avatars[opt.avatar_id],160)

    # init rtc manager
    session_manager.set_max_session(opt.max_session)
    session_manager.init_builder(build_avatar_session)
    rtc_manager = RTCManager(opt)
    # share avatar_sessions (RTCManager handles it but routes.py expects it)

    # 虚拟摄像头或 RTMP 模式：启动后台渲染线程
    if opt.transport == 'virtualcam' or opt.transport == 'rtmp':
        thread_quit = Event()
        params = {}
        # session 0 for virtualcam
        session_manager.add_session('0', build_avatar_session('0', params))
        rendthrd = Thread(target=session_manager.get_session('0').render, args=(thread_quit,))
        rendthrd.start()
        if opt.transport == 'virtualcam':
            logger.info("[VirtualCam] Virtual camera output enabled - digital human will be rendered to virtual camera")

    #############################################################################
    appasync = web.Application(client_max_size=1024**2*100)
    appasync["llm_response"] = llm_response
    appasync["note_spoken"] = note_spoken   # 1005: echo开口句记入对话史(防重复自我介绍)
    appasync["opt"] = opt
    appasync["rtc_manager"] = rtc_manager

    appasync.on_shutdown.append(on_shutdown)
    appasync.router.add_post("/offer", offer)
    appasync.router.add_get("/record/{sessionid}", download_record)

    # 注册 server/routes.py 中的通用 API 路由
    setup_routes(appasync)

    # Configure default CORS settings.
    cors = aiohttp_cors.setup(appasync, defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_credentials=True,
                expose_headers="*",
                allow_headers="*",
            )
        })
    # Configure CORS on all routes.
    for route in list(appasync.router.routes()):
        cors.add(route)

    # /whep 注册在 CORS 之后：自行管理 OPTIONS，避免与 aiohttp_cors 冲突
    whep_resource = appasync.router.add_resource('/whep')
    whep_resource.add_route('POST', whep)
    whep_resource.add_route('OPTIONS', lambda _: web.Response(status=200))

    logger.info('start http server; http://<serverip>:'+str(opt.listenport))
    # logger.info('如果使用webrtc，推荐访问webrtc集成前端: http://<serverip>:'+str(opt.listenport)+'/dashboard.html')
    # 1019fix: 默认绑 127.0.0.1 —— 第三方软件(实测 Feishu.exe)会抢 0.0.0.0:8011 通配端口,
    # 致本机引擎 bind 失败(winerror 10048), 且释放后端口被抢走再也拿不回。
    # 全链路(app/backend/server.py 的 engine 路由 + watchdog health)本就都走 127.0.0.1,
    # 引擎绑通配反而把别家进程引到本机端口上。需局域网直连时: set LT_LISTEN_HOST=0.0.0.0 回退。
    listen_host = os.environ.get('LT_LISTEN_HOST', '127.0.0.1')
    def run_server(runner):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, listen_host, opt.listenport)
        loop.run_until_complete(site.start())
        if opt.transport=='rtcpush':
            for k in range(opt.max_session):
                push_url = opt.push_url
                if k!=0:
                    push_url = opt.push_url+str(k)
                loop.run_until_complete(rtc_manager.handle_rtcpush(push_url, str(k)))
        loop.run_forever()    
    #Thread(target=run_server, args=(web.AppRunner(appasync),)).start()
    run_server(web.AppRunner(appasync))

    #app.on_shutdown.append(on_shutdown)
    #app.router.add_post("/offer", offer)

    # print('start websocket server')
    # server = pywsgi.WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)
    # server.serve_forever()


# os.environ['MKL_SERVICE_FORCE_INTEL'] = '1'
# os.environ['MULTIPROCESSING_METHOD'] = 'forkserver'                                                    
if __name__ == '__main__':
    mp.set_start_method('spawn')
    load_dotenv()  # Load environment variables from .env file, if it exists
    main()
    
    
    
