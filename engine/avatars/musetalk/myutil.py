import numpy as np
import cv2
import json
import os
import time

# 1004(嘴糊修复): MuseTalk 的 VAE 把脸部压到 32x32 latent(8倍下采样), 嘴部天然偏软;
# 紧框 coords 已把嘴占比从 10.7% 提到 14-16%, 再加贴回前 USM 锐化补回高频。
# 只对 face(unet 输出的嘴区) 锐化, mask 外是源图像素 → 眼睛/头盔锐利度不受影响。
# 配置 data/sharpen.json = {"amount": 0.9, "radius": 1.1} — amount=0 即关闭(原行为)。
# 热读 + 30s 缓存: 调参改 json 等 30s 生效, 不用重启引擎。
_SHARP_CFG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'data', 'sharpen.json'))
_SHARP_CACHE = [None, 0.0]


def _sharpen_cfg():
    now = time.time()
    if _SHARP_CACHE[1] and now - _SHARP_CACHE[1] < 30:
        return _SHARP_CACHE[0]
    cfg = (0.0, 1.1)
    try:
        d = json.load(open(_SHARP_CFG_PATH, encoding='utf-8'))
        cfg = (float(d.get('amount', 0.0)), float(d.get('radius', 1.1)))
    except Exception:
        pass
    _SHARP_CACHE[0] = cfg
    _SHARP_CACHE[1] = now
    return cfg


def _usm(img, amount, radius):
    """Unsharp mask: out = img*(1+amount) - blur*amount。纯cv2, 256x256约0.3ms"""
    blur = cv2.GaussianBlur(img, (0, 0), radius)
    return cv2.addWeighted(img, 1.0 + amount, blur, -amount, 0)


def get_image_blending(image,face,face_box,mask_array,crop_box):
    body = image
    x, y, x1, y1 = face_box
    x_s, y_s, x_e, y_e = crop_box
    amt, rad = _sharpen_cfg()
    if amt > 0:
        face = _usm(face, amt, rad)
    face_large = body[y_s:y_e, x_s:x_e].copy()
    face_large[y-y_s:y1-y_s, x-x_s:x1-x_s]=face

    mask_image = cv2.cvtColor(mask_array,cv2.COLOR_BGR2GRAY)
    mask_image = (mask_image/255).astype(np.float32)

    # mask_not = cv2.bitwise_not(mask_array)
    # prospect_tmp = cv2.bitwise_and(face_large, face_large, mask=mask_array)
    # background_img = body[y_s:y_e, x_s:x_e]
    # background_img = cv2.bitwise_and(background_img, background_img, mask=mask_not)
    # body[y_s:y_e, x_s:x_e] = prospect_tmp + background_img

    #print(mask_image.shape)
    #print(cv2.minMaxLoc(mask_image))

    body[y_s:y_e, x_s:x_e] = cv2.blendLinear(face_large,body[y_s:y_e, x_s:x_e],mask_image,1-mask_image)

    #body.paste(face_large, crop_box[:2], mask_image)
    return body
