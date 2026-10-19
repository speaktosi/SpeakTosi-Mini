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
#
#  MuseTalk 数字人 — 迁移自 musereal.py + museasr.py
#

import math
import gc
import torch
import numpy as np

import subprocess
import os
import time
import torch.nn.functional as F
import cv2
import glob
import pickle

import queue
from queue import Queue
from threading import Thread, Event
import torch.multiprocessing as mp

from avatars.musetalk.utils.utils import get_file_type,get_video_fps,datagen
from avatars.musetalk.myutil import get_image_blending
from avatars.musetalk.utils.utils import load_all_model
from avatars.musetalk.whisper.audio2feature import Audio2Feature

from avatars.audio_features.whisper import WhisperASR
import asyncio
from av import AudioFrame, VideoFrame
from avatars.base_avatar import BaseAvatar

from tqdm import tqdm
from utils.logger import logger
from utils.image import read_imgs, mirror_index
from utils.device import initialize_device
from registry import register

device = initialize_device()
logger.info('Using {} for inference.'.format(device))

def load_model():
    # 1018 显存优化: use_float16=True -> fp16 转换在 CPU 上完成, 3.4G fp32 权重不再进显存。
    # 数值结果与原路径完全一致(权重都是 fp16)。详见 models/unet.py 注释。
    vae, unet, pe = load_all_model(use_float16=True)
    #device = torch.device("cuda" if torch.cuda.is_available() else ("mps" if (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()) else "cpu"))
    timesteps = torch.tensor([0], device=device)
    pe = pe.half().to(device)
    vae.vae = vae.vae.half().to(device)          # 幂等(已 half)
    unet.model = unet.model.half().to(device)    # 幂等(已 half)
    # Initialize audio processor and Whisper model
    audio_processor = Audio2Feature(model_path="./models/whisper")
    # 1018: 回收加载期碎片, 并打印真实占用供验收(与 nvidia-smi 对照)
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        logger.info('[1018] musetalk 模型加载完成: allocated=%.0fM reserved=%.0fM (总显存=%.0fM)'
                    % (torch.cuda.memory_allocated() / 2**20,
                       torch.cuda.memory_reserved() / 2**20,
                       torch.cuda.get_device_properties(0).total_memory / 2**20))
    return vae, unet, pe, timesteps, audio_processor

def load_avatar(avatar_id):
    avatar_path = f"./data/avatars/{avatar_id}"
    full_imgs_path = f"{avatar_path}/full_imgs" 
    coords_path = f"{avatar_path}/coords.pkl"
    latents_out_path= f"{avatar_path}/latents.pt"
    video_out_path = f"{avatar_path}/vid_output/"
    mask_out_path =f"{avatar_path}/mask"
    mask_coords_path =f"{avatar_path}/mask_coords.pkl"
    avatar_info_path = f"{avatar_path}/avator_info.json"

    input_latent_list_cycle = torch.load(latents_out_path)
    with open(coords_path, 'rb') as f:
        coord_list_cycle = pickle.load(f)
    frame_list_cycle = None
    input_img_list = glob.glob(os.path.join(full_imgs_path, '*.[jpJP][pnPN]*[gG]'))
    input_img_list = sorted(input_img_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    frame_list_cycle = read_imgs(input_img_list)
    with open(mask_coords_path, 'rb') as f:
        mask_coords_list_cycle = pickle.load(f)
    input_mask_list = glob.glob(os.path.join(mask_out_path, '*.[jpJP][pnPN]*[gG]'))
    input_mask_list = sorted(input_mask_list, key=lambda x: int(os.path.splitext(os.path.basename(x))[0]))
    mask_list_cycle = read_imgs(input_mask_list)

    # 1008fix(铁律安全网): mask_coords 必须与 mask PNG 尺寸逐帧一致, 否则
    # get_image_blending 里 body.crop(crop_box) 与 mask_array 错位 -> 嘴糊/丢帧。
    # 生成端已在 blending.py 修复(返回 clamp 后 crop_box), 这里是加载期保险丝:
    # r3 故障时失配是静默的(只在一次性脚本里查过), 引擎自身毫无预警。
    _bad = []
    for _i, _m in enumerate(mask_list_cycle):
        if _i >= len(mask_coords_list_cycle):
            break
        _c = mask_coords_list_cycle[_i]
        _h, _w = _m.shape[:2]
        if (_c[2] - _c[0]) != _w or (_c[3] - _c[1]) != _h:
            _bad.append((_i, (_w, _h), (_c[2] - _c[0], _c[3] - _c[1])))
    if _bad:
        logger.error(
            f"🔴 [{avatar_id}] mask_coords 与 mask 尺寸失配 {len(_bad)}/{len(mask_list_cycle)} 帧"
            f" (首例 帧{_bad[0][0]}: mask={_bad[0][1]} coords={_bad[0][2]}) -> 该素材嘴部会糊/丢帧, "
            f"请用修复后的 blending.py 重新 genavatar"
        )
    else:
        logger.info(f"✅ [{avatar_id}] mask_coords 校验通过 ({len(mask_list_cycle)} 帧, 失配=0)")

    return frame_list_cycle,mask_list_cycle,coord_list_cycle,mask_coords_list_cycle,input_latent_list_cycle

@torch.no_grad()
def warm_up(batch_size,model):
    # 预热函数
    print('warmup model...')
    vae, unet, pe, timesteps, audio_processor = model
    whisper_batch = np.ones((batch_size, 50, 384), dtype=np.uint8)
    latent_batch = torch.ones(batch_size, 8, 32, 32).to(unet.device)

    audio_feature_batch = torch.from_numpy(whisper_batch)
    audio_feature_batch = audio_feature_batch.to(device=unet.device, dtype=unet.model.dtype)
    audio_feature_batch = pe(audio_feature_batch)
    latent_batch = latent_batch.to(dtype=unet.model.dtype)
    pred_latents = unet.model(latent_batch,
                              timesteps,
                              encoder_hidden_states=audio_feature_batch).sample
    vae.decode_latents(pred_latents)    

@register("avatar", "musetalk")
class MuseReal(BaseAvatar):
    @torch.no_grad()
    def __init__(self, opt, model, avatar):
        super().__init__(opt)

        #self.fps = opt.fps # 20 ms per frame

        # self.batch_size = opt.batch_size
        # self.idx = 0
        # self.res_frame_queue = mp.Queue(self.batch_size*2)

        self.vae, self.unet, self.pe, self.timesteps, self.audio_processor = model

        self.frame_list_cycle,self.mask_list_cycle,self.coord_list_cycle,self.mask_coords_list_cycle, self.input_latent_list_cycle = avatar

        self.asr = WhisperASR(opt,self,self.audio_processor)
        self.asr.warm_up()
    

    @torch.no_grad()
    def inference_batch(self, index, audiofeat_batch):
        # 这里的 index 是针对当前 avatar 的索引
        # 返回一个 batch 的推理结果，batch 大小由 self.batch_size 决定
        length = len(self.input_latent_list_cycle)
        whisper_batch = np.stack(audiofeat_batch)
        latent_batch = []
        for i in range(self.batch_size):
            idx = mirror_index(length, index + i)
            latent = self.input_latent_list_cycle[idx]
            latent_batch.append(latent)
        latent_batch = torch.cat(latent_batch, dim=0)
        
        audio_feature_batch = torch.from_numpy(whisper_batch)
        audio_feature_batch = audio_feature_batch.to(device=self.unet.device,
                                                        dtype=self.unet.model.dtype)
        audio_feature_batch = self.pe(audio_feature_batch)
        # 1003修复: 原 .to(dtype=...) 不带device — Brenda素材latents恰好保存在cuda被掩盖;
        # 卡通包latents在cpu → 开口瞬间conv_in崩(Input type HalfTensor/cpu vs weight cuda) → inference线程死 → 会话假死。
        latent_batch = latent_batch.to(device=self.unet.device, dtype=self.unet.model.dtype)

        pred_latents = self.unet.model(latent_batch, 
                                    self.timesteps, 
                                    encoder_hidden_states=audio_feature_batch).sample
        pred = self.vae.decode_latents(pred_latents)
        return pred

    def paste_back_frame(self,pred_frame,idx:int):
        bbox = self.coord_list_cycle[idx]
        ori_frame = self.frame_list_cycle[idx].copy()
        x1, y1, x2, y2 = bbox

        # 1007(嘴糊修复): 默认INTER_LINEAR在256→215x354非整数倍放大时抹高频(USM无法回补被抹掉的细节)。
        # 改INTER_LANCZOS4保留高频，贴回后由myutil的USM再增强。
        res_frame = cv2.resize(pred_frame,(x2-x1,y2-y1),
                               interpolation=cv2.INTER_LANCZOS4)
        mask = self.mask_list_cycle[idx]
        mask_crop_box = self.mask_coords_list_cycle[idx]

        combine_frame = get_image_blending(ori_frame,res_frame,bbox,mask,mask_crop_box)
        return combine_frame
            
