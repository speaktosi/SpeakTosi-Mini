# -*- coding: utf-8 -*-
"""多老师声纹注册表 (1015 方案A, 见 多老师音色架构方案_1015.md)
唯一真相源: 每师一行 {ref_wav, prompt_text, lang, f0, speed}.
- cosyvoice_lx_server v2 启动时逐师 add_zero_shot_spk -> voice 路由, 未注册一律 403(绝不兜底).
- backend/引擎 pitch-lock 读本表取该师 f0 锚(不再锁死 262.3).
红线: 任何老师不得借用别人的嗓子 —— 查不到就 403, 没有 default.
"""
import os, json

REGISTRY_PATH = r"C:\Speaktosi\engine\data\voice_registry.json"


def load(path=REGISTRY_PATH):
    reg = json.loads(open(path, encoding="utf-8").read())
    # 1020mini: ref_wav 可能是打包机绝对路径(换机后不存在) -> 回落到本包 ref 音目录
    _vb = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path)))),
                       "audiocpp", "webui", "voice")
    for k, v in reg.items():
        _rw = v.get("ref_wav")
        if _rw and not os.path.exists(_rw):
            _alt = os.path.join(_vb, os.path.basename(str(_rw).replace("\\", "/")))
            if os.path.exists(_alt):
                v["ref_wav"] = _alt
        if not (v.get("ref_wav") and v.get("prompt_text") and os.path.exists(v["ref_wav"])):
            raise ValueError("registry entry %r incomplete/missing ref: %s" % (k, v.get("ref_wav")))
    return reg


def lookup(voice, path=REGISTRY_PATH):
    """大小写不敏感查表; 未注册返回 None(调用方必须 403, 禁止兜底)."""
    reg = load(path)
    return reg.get(str(voice or "").strip().lower())
