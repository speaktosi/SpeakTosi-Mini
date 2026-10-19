#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SpeakTosi 1020【mini 简化版】打包规则 = 只留 Coach Arthur + Coach Mia 两位老师.

为什么单独一个模块: package_share.py 是"通用打包器", 这里是"档位规则"。
mini 档的每一步裁剪都有取证依据(1020 实测/源码追踪), 不是猜的:

  1. 老师私有资产(逐目录取证)
     engine/data/avatars/<id>           79~115M   musetalk 形象包(latents/coords/mask/full_imgs)
     engine/data/customvideo/<id>_idle  ~75M      待机图序列(引擎启动即读)
     engine/data/custom_config_<id>.json          该老师的渲染源(customvideo 挂载)
     audiocpp/webui/voice/<id>_ref.wav            克隆参考音
     engine/data/voice_anchor_ref_<id>_ref.wav_*.npz  音色锚点(句首到位)
     avatar/persona/<Name>.md                     人设(soul 源)
     avatar/<Name>.png/.wav                       立绘 + 见面语参考音
     tools/scripts/tts_demo_voice.json 的 <id> preset + voice_registry.json / tts_calibration.json 的 <id> 键

  2. 引擎路由(1006 双引擎, 源码实证)
     卡通系(ling*)→8010 viseme 图层渲染; 真人系(Brenda / coach_*)→8011 musetalk。
     mini 只有两位真人系老师 → 8010 服务整块移除, 由 8011 兼做
     engine/web/desktop.html(托盘"老师出现在桌面"那个窗口)的宿主。
     依据: app/avatars/viseme_avatar.py 的嘴型是共享卡通图层(avatar/viseme/*.png)
     贴到 <avatar>_idle 底图上 —— 真人底图贴卡通嘴 = 五官错位, 且 1017 实录
     musetalk 素材喂 viseme 引擎会卡死。所以不做"用 8010 跑真人"的取巧。

  3. 深探坑(不改必死循环): watchdog.deep_tts_ok() 用 voice='lingxue' 真合成一句判存活,
     音色被裁掉却仍按 lingxue 探 → 18091 永远"卡死"→ 反复被杀。已改 arthur。

  4. 运行档位: ultra 的定义就是"只跑卡通引擎", mini 没有卡通引擎 → 请求 ultra 时钳到 lite。

  5. 体育精神: 开发机源码零改动 —— 所有裁剪在打包期内存里做; 要回 13 师/卡通系,
     用 --lowmem 重新打包即可(见 tools/package_share.py --help)。
"""
from __future__ import annotations

import ast
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MINI_TEACHERS = ("coach_arthur", "coach_mia")
MINI_IDLE_KEEP = tuple(t + "_idle" for t in MINI_TEACHERS)
MINI_MEDIA_KEEP = ("Coach-Arthur.", "Coach-Mia.")          # avatar/ 下的立绘与参考音
MINI_VOICES = ("arthur", "mia")                            # 保留的音色 preset
MINI_PERSONA_KEEP = ("CoachArthur.md", "CoachMia.md", "Shared-core.md", "Shared-core-adult.md")
MINI_BOOT_AVATAR = "coach_arthur"      # 8011 主位 = 桌面窗口(desktop.html)渲染的那位
MINI_BOOT_REF = "arthur"
MINI_DEFAULT_AVATAR = "coach_arthur"   # 出厂默认老师(与 8011 主位一致, 免"窗里人不是选中的人")
MINI_WAKE_WORDS = ["SpeakTosi", "hello", "Arthur", "Mia"]

# 只有仓库档(GitHub)剔的超大二进制: GitHub 单文件 100MB 硬上限(ffmpeg.exe 227M 会被拒收);
# 自包含 zip 档照旧保留 —— 它们是运行时真需要的(ffmpeg 切音频/导出, s3fd 是 wav2lip 的人脸检测权重)。
MINICODE_DROP_FILES = {
    "audiocpp/webui/ffmpeg.exe":
        "227MB 单文件超 GitHub 100MB 上限(仓库档不带; 见 docs/ASSETS.md 第 4 节)",
    "engine/avatars/wav2lip/face_detection/detection/sfd/s3fd.pth":
        "90MB 人脸检测权重(wav2lip 路径用, musetalk 不需要; 见 docs/ASSETS.md)",
}

PERSONA_MD_DIR = os.path.join(ROOT, "avatar", "persona")
PERSONA_JSON = os.path.join(ROOT, "app", "backend", "config", "persona.json")
SERVER_PY = os.path.join(ROOT, "app", "backend", "server.py")


# --------------------------------------------------------------------------
# 1) 逐文件保留/剔除判定
# --------------------------------------------------------------------------
def build_info_text(mode: str, picked, skipped, raw_bytes: int) -> str:
    """产物内的 BUILD-INFO.txt: 真实文件数/体积/规则指纹/裁剪统计 —— 免得文档里的数字过期。
    只给自包含档(--mini)写; 仓库档的"构建记录"就是 git 历史。"""
    import datetime
    import hashlib
    try:
        with open(os.path.join(ROOT, "tools", "mini_edition.py"), "rb") as f:
            fp = hashlib.sha256(f.read()).hexdigest()[:12]
    except OSError:
        fp = "?"
    reasons = {}
    for s in skipped:
        if "(mini: " in s:
            r = s.split("(mini: ", 1)[1].rstrip(")").strip()
            reasons[r] = reasons.get(r, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: -kv[1])[:8]
    return "\n".join([
        "SpeakTosi Mini · 打包信息(BUILD-INFO)",
        "=========================================",
        "档位模式    : %s (只装两位老师: Coach Arthur / Coach Mia)" % mode,
        "生成时间    : %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "文件数      : %d" % len(picked),
        "原始体积    : %.2f GB" % (raw_bytes / 1024.0 / 1024.0 / 1024.0),
        "",
        "保留阵容    : coach_arthur / coach_mia (只这两位)",
        "保留音色    : arthur / mia (+ default 内部别名, 指向 Arthur, 不进界面)",
        "引擎拓扑    : 单一 MuseTalk 引擎(8011); 卡通系 viseme 引擎(8010)已裁",
        "引擎主位    : coach_arthur(桌面窗口渲染的那位, 与出厂默认老师一致)",
        "",
        "裁剪规则指纹: mini_edition.py sha256[:12] = %s" % fp,
        "被裁条目    : %d 个(按理由, 前 %d 类)" % (len(skipped), len(top)),
    ] + ["              - %-34s %d 个" % (k, v) for k, v in top] + [
        "",
        "复现同一份:  python tools/package_share.py --mini",
        "回到 13 师 :  python tools/package_share.py --lowmem",
        "版本不变量:  python tools/check_mini_invariants.py  (CI 也跑它)",
    ]) + "\n"


def drop_reason(rel: str, mode: str = "mini"):
    """mini 档: 返回剔除理由(保留则 None)。rel = 包内相对路径, 正斜杠。
    mode="mini-code"(GitHub 仓库档)额外剔几个"超大二进制": 仓库有 100MB/文件 硬上限,
    ffmpeg.exe(227M) 会被 GitHub 直接拒收 —— 这些在自包含档里照旧保留。"""
    b = os.path.basename(rel)
    p = rel.split("/")
    if mode == "mini-code" and rel in MINICODE_DROP_FILES:
        return MINICODE_DROP_FILES[rel]
    if rel.startswith("engine/data/avatars/") and len(p) > 3:
        return None if p[3] in MINI_TEACHERS else "非 Arthur/Mia 形象包"
    if rel.startswith("engine/data/customvideo/") and len(p) > 3:
        return None if p[3] in MINI_IDLE_KEEP else "非 Arthur/Mia 待机序列"
    if rel.startswith("engine/data/custom_config_") and rel.endswith(".json"):
        return None if b == "custom_config_%s.json" % MINI_BOOT_AVATAR else "非 Arthur/Mia 引擎配置"
    if rel.startswith("engine/data/voice_anchor_"):
        return None if any(k in b for k in ("_arthur_ref", "_mia_ref")) else "非 Arthur/Mia 音色锚点"
    if rel.startswith("audiocpp/webui/voice/"):
        return None if (b.startswith(("arthur_ref", "mia_ref")) or b == "prompt_text") else "非 Arthur/Mia 参考音频"
    if rel.startswith("docs/"):
        if b.startswith("SpeakTosi_1019") or b == "AI_COACHES_PROFILES.md":
            return "非本档文档(13 师名册/开发期交接报告)"
    if rel.startswith("avatar/persona/"):
        if len(p) != 3:
            return "人设目录下的历史备份子目录"
        return None if b in MINI_PERSONA_KEEP else "非 Arthur/Mia 人设"
    if rel.startswith("app/backend/config/personas/"):
        return "默认老师(Brenda)灵魂备份, mini 不需要"
    if rel.startswith("avatar/"):
        if b.startswith(MINI_MEDIA_KEEP):
            return None
        if len(p) > 2 and p[1] == "viseme":
            return None if (len(p) == 3 and b.endswith(".png")) else "非通用 viseme 图层"
        return "非 Arthur/Mia 形象素材"
    return None


# --------------------------------------------------------------------------
# 2) 代码补丁(打包期内存里改文本, 每条都 assert 锚点命中)
# --------------------------------------------------------------------------
def _reps(txt: str, reps, rel: str, hard: bool = True) -> str:
    """reps = [(old, new, expect_count|None), ...]; 命中数不符 → 报错(hard)或告警"""
    for old, new, exp in reps:
        n = txt.count(old)
        if n == 0 or (exp is not None and n != exp):
            msg = "[mini] %s 锚点异常: %r 命中 %d 次(期望 %s)" % (rel, old[:60], n, exp)
            if hard:
                raise SystemExit(msg + " —— 源码已变, 请核对 mini_edition.py")
            print("[!] " + msg + " —— 已跳过")
            continue
        txt = txt.replace(old, new)
    return txt


def patch_watchdog(txt: str) -> str:
    txt = _reps(txt, [
        # 8011 主位与音色 → Arthur(与 AVATARS 两位一致)
        ("'--avatar_id', 'aiteacher_demo'", "'--avatar_id', 'coach_arthur'", 1),
        ("'--REF_FILE', 'default'", "'--REF_FILE', 'arthur'", 1),
        # 深探句: 音色裁掉仍按 lingxue 探活 → 18091 会被判"卡死"反复杀(死循环)
        ("'voice': 'lingxue'", "'voice': 'arthur'", 1),
        # 过期注释(1006 结论)修正 —— 注意: 源码是 CRLF + 8 空格缩进(锚点必须照抄)
        ("        # 1006: musetalk 真人系引擎(Brenda/Coach系)独立实例。viseme引擎只认 *_cartoon_idle 素材,\n"
         "        # aiteacher_demo/Coach 是 musetalk 素材(latents.pt等) → 切过去必卡死(1006 Owen Brenda连接失败根因)。",
         "        # 1020mini: 主位=Coach Arthur(与本档两位老师一致); 1017 实录: musetalk 素材喂 viseme 引擎会卡死,\n"
         "        # 故本档只留这一个真人引擎 —— 原 8010 卡通引擎块已整块移除(见文件顶部 1020mini 说明)。", 1),
        # ultra(只跑卡通引擎)在 mini 无意义 → 钳到 lite
        ("# ultra: 不起 musetalk(9G 显存大户), 只留卡通系 viseme 引擎\nif MODEL_MODE == 'ultra':\n"
         "    SERVICES = [s for s in SERVICES if s['name'] != ENGINE_MUSETALK]",
         "# 1020mini: 本档没有卡通引擎 —— ultra(只跑卡通)无意义, 请求 ultra 时按 lite 跑(最低需 >=12G 显存)\n"
         "if MODEL_MODE == 'ultra':\n    MODEL_MODE = 'lite'", 1),
    ], "engine/aiteacher_watchdog.py")
    # 8010 卡通引擎服务块整块移除(它依赖 ling*_cartoon 素材, 本档不带)
    start = txt.find("    {\n        'name': 'livetalking', 'port': 8010,")
    end = txt.find("\n    },\n", start) if start > 0 else -1
    if start < 0 or end < start:
        raise SystemExit("[mini] 8010 服务块锚点未命中 —— 源码已变, 请核对 mini_edition.py")
    block = txt[start:end + len("\n    },\n")]
    txt = txt.replace(block,
                      "    # 1020mini: 原 8010 卡通系 viseme 引擎块已整块移除 —— 本档只有 Coach Arthur / Coach Mia\n"
                      "    # 两位 musetalk 老师, 由 8011 兼做 engine/web/desktop.html(桌面窗口)的宿主。\n"
                      "    # 回滚: 用 tools/package_share.py --lowmem 重新打包(13 师 + 卡通系)。\n")
    txt = _reps(txt, [
        ("LiveTalking口型引擎(8010/8011)", "LiveTalking口型引擎(8011)", 1),
    ], "engine/aiteacher_watchdog.py", hard=False)
    return txt


def patch_main_js(txt: str) -> str:
    return _reps(txt, [
        ("tcpUp(8010)", "tcpUp(8011)", 1),
        ("'http://127.0.0.1:8010/desktop.html'", "'http://127.0.0.1:8011/desktop.html'", 2),
        ("（8010 未就绪？看门狗会自动拉起）", "（8011 未就绪？看门狗会自动拉起）", 1),
        ("数字人引擎 (8010)", "数字人引擎 (8011)", 1),
    ], "app/main.js")


def patch_overlay_js(txt: str) -> str:
    return _reps(txt, [
        ("const LT_DEFAULT = 'http://127.0.0.1:8010';", "const LT_DEFAULT = 'http://127.0.0.1:8011';", 1),
        ("viseme卡通系→8010, musetalk真人系(Brenda/Coach)→8011",
         "1020mini: 只剩 musetalk 真人系→8011(卡通引擎已裁)", 1),
    ], "app/renderer/overlay.js")


def patch_desktop_html(txt: str) -> str:
    return _reps(txt, [
        ("（确认看门狗已拉起 8010）", "（确认看门狗已拉起 8011）", 1),
    ], "engine/web/desktop.html")


def patch_devtool(txt: str, rel: str) -> str:
    """维护脚本里的 8010/viseme/lingxue 引用收窄到 mini 实际拓扑(静默、确定性替换)。"""
    txt = txt.replace("8010: 'viseme', ", "")       # PORTS 字典里的 8010 一行去掉(8011 本就在)
    txt = txt.replace("'lingxue'", "'arthur'")      # 探活/合成样例音色 → 保留的音色
    txt = txt.replace("凌雪老师", "Arthur 老师")
    txt = txt.replace("18080", "18091")             # 1015 已退役的旧 TTS 口
    txt = txt.replace("8010", "8011")               # 收尾: 剩余 8010 一律归到唯一引擎
    return txt


def patch_server_py(txt: str) -> str:
    """app/backend/server.py:
    (1) AVATARS 只留 Arthur/Mia —— 用 ast 摘原表里的那两条(保原生字段/格式), 不是手抄;
    (2) _TEACHER_MD 收窄;
    (3) DEFAULT_CFG 的出厂老师/音色从 aiteacher_demo+default 改成 coach_arthur+arthur。
    功能上不补也行(/avatars 只列素材存在的形象), 但"简化版只装两位老师"必须是注册表事实。"""
    tree = ast.parse(txt)
    node = None
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "AVATARS" for t in n.targets):
            node = n.value
            break
    if not isinstance(node, ast.List):
        raise SystemExit("[mini] server.py 里 AVATARS 不是列表字面量(规则已变)")
    segs = {}
    n_el = 0
    for el in node.elts:
        if not isinstance(el, ast.Dict):
            continue
        n_el += 1
        aid = None
        for k, v in zip(el.keys, el.values):
            if isinstance(k, ast.Constant) and k.value == "avatar_id" and isinstance(v, ast.Constant):
                aid = v.value
        if aid in MINI_TEACHERS:
            seg = ast.get_source_segment(txt, el)
            segs[aid] = "    " + seg.split("\n")[0] + (
                "\n" + "\n".join(seg.split("\n")[1:]) if "\n" in seg else "")
    missing = [t for t in MINI_TEACHERS if t not in segs]
    if missing:
        raise SystemExit("[mini] AVATARS 里找不到这两位老师: %s" % missing)
    lines = txt.splitlines(keepends=True)
    new = ("AVATARS = [\n"
           "    # 1020mini: 本档只保留 Arthur / Mia 两位老师 —— 其余 %d 条的注册项已裁掉,\n"
           "    # 对应的形象包/待机序列/人设/音色同样不在包里(见 tools/mini_edition.py)。\n"
           % max(0, n_el - 2))
    for t in MINI_TEACHERS:
        new += segs[t] + ",\n"
    new += "]"
    lines[node.lineno - 1:node.end_lineno] = [new]
    txt = "".join(lines)
    # _TEACHER_MD 收窄(首个 '}' 即字典尾 —— 值都是字符串, 没有嵌套花括号)
    new_md = ('_TEACHER_MD = {"coach_arthur": "CoachArthur.md", "coach_mia": "CoachMia.md"}')
    txt, n = re.subn(r"^_TEACHER_MD = \{.*?\}", new_md.replace("\\", "\\\\"), txt,
                     count=1, flags=re.M | re.S)
    if n != 1:
        print("[!] [mini] _TEACHER_MD 锚点未命中(已跳过)")
    # 出厂默认老师/音色
    for a, b in (('"avatar_id": "aiteacher_demo"', '"avatar_id": "coach_arthur"'),
                 ('"teacher_id": "aiteacher_demo"', '"teacher_id": "coach_arthur"'),
                 ('"avatar_voice": "default"', '"avatar_voice": "arthur"'),
                 ('"audiocpp_tts_voice": "default"', '"audiocpp_tts_voice": "arthur"')):
        c = txt.count(a)
        txt = txt.replace(a, b)
        print("[mini] server.py 默认值替换 %s x%d" % (a, c))
    # /voices 里"default 演示音色"兜底项: 本档音色只有 Arthur/Mia → 去掉兜底, 免得再冒第 3 个"默认音色"
    fb = ('    if not presets or "default" in presets:\n'
          '        voices.append({"id": "default", "name": "SpeakTosi Demo Voice · 默认",\n'
          '                       "emoji": "🌙", "avatar_id": "coach_arthur", "teacher": False})\n')
    if fb in txt:
        txt = txt.replace(fb,
                          '    # 1020mini: 原"default 演示音色"兜底项已移除 —— 本档音色只有 Arthur / Mia 两套克隆音。\n')
        print("[mini] server.py /voices 兜底演示音色项已移除")
    else:
        print("[!] [mini] server.py /voices 兜底项锚点未命中(已跳过)")
    return txt


def patch_voice_registry(txt: str) -> str:
    """1020mini: voice_registry.load() 换机自愈。

    坑(1020 实测): 注册表里的 ref_wav 烙着打包机绝对路径, 且是 JSON 转义形态
    (C:\\\\Speaktosi\\\\...), portable_paths 的 OLD_ROOTS 只认单反斜杠 -> 永不命中 ->
    换机后 os.path.exists 为假 -> load() 抛 ValueError -> CosyVoice 起不来 = 全站失声。
    (本机验证时被蒙过: 打包机恰好存在该路径, 所以 TTS 实测 200 也看不出问题。)
    修法: ref_wav 不存在时, 按 <包根>/audiocpp/webui/voice/<同名> 回落(包内确实带着这两个 ref)。
    """
    old = ('    reg = json.loads(open(path, encoding="utf-8").read())\n'
           '    for k, v in reg.items():\n'
           '        if not (v.get("ref_wav") and v.get("prompt_text") and os.path.exists(v["ref_wav"])):')
    new = ('    reg = json.loads(open(path, encoding="utf-8").read())\n'
           '    # 1020mini: ref_wav 可能是打包机绝对路径(换机后不存在) -> 回落到本包 ref 音目录\n'
           '    _vb = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path)))),\n'
           '                       "audiocpp", "webui", "voice")\n'
           '    for k, v in reg.items():\n'
           '        _rw = v.get("ref_wav")\n'
           '        if _rw and not os.path.exists(_rw):\n'
           '            _alt = os.path.join(_vb, os.path.basename(str(_rw).replace("\\\\", "/")))\n'
           '            if os.path.exists(_alt):\n'
           '                v["ref_wav"] = _alt\n'
           '        if not (v.get("ref_wav") and v.get("prompt_text") and os.path.exists(v["ref_wav"])):')
    n = txt.count(old)
    assert n == 1, "[mini] voice_registry.load 锚点命中 %d 次(期望 1)" % n
    return txt.replace(old, new)


RAW_PATCHERS = {
    "engine/tools/voice_registry.py": patch_voice_registry,
    "app/backend/server.py": patch_server_py,
    "engine/aiteacher_watchdog.py": patch_watchdog,
    "app/main.js": patch_main_js,
    "app/renderer/overlay.js": patch_overlay_js,
    "engine/web/desktop.html": patch_desktop_html,
    "tools/verify_vram.py": lambda t: patch_devtool(t, "tools/verify_vram.py"),
    "tools/verify_e2e_ui.py": lambda t: patch_devtool(t, "tools/verify_e2e_ui.py"),
    "tools/scripts/live_opener.py": lambda t: patch_devtool(t, "tools/scripts/live_opener.py"),
}


def patch_file(rel: str, txt: str):
    """按 rel 选补丁函数并应用(返回 None = 该文件无补丁)。

    必须先归一化 CRLF→LF: 仓库源码是 CRLF, 多行锚点里写 \\n 才不会漏匹配
    (1020 踩过: 4 空格 + \\n 的锚点在 CRLF 文件里恒为 0 命中)。
    """
    fn = RAW_PATCHERS.get(rel)
    if fn is None:
        return None
    return fn(txt.replace("\r\n", "\n"))

# --------------------------------------------------------------------------
# 3) 配置/JSON 覆写: 只留两位老师的键(打包期重算, 不依赖开发机状态)
# --------------------------------------------------------------------------
def _load_json(rel: str):
    with open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return json.load(f)


def _dump(o) -> str:
    return json.dumps(o, ensure_ascii=False, indent=2) + "\n"


def voice_registry_json() -> str:
    """只留 arthur/mia 音色。另保留 default 别名(= Arthur 参考音) —— 兼容任何遗留的
    'default' 合成请求, 但 /voices 由 tts 模板决定(模板里没有 default) → 界面只显示两位。"""
    d = _load_json("engine/data/voice_registry.json")
    if not isinstance(d, dict):
        raise SystemExit("[mini] voice_registry.json 结构异常")
    missing = [v for v in MINI_VOICES if v not in d]
    if missing:
        raise SystemExit("[mini] voice_registry 缺音色: %s" % missing)
    out = {k: v for k, v in d.items() if k in MINI_VOICES}
    out["default"] = json.loads(json.dumps(out[MINI_BOOT_REF]))   # 同 Arthur 条目, 不额外加键(服务端按值取字段)
    for _v in out.values():
        # 1020mini: ref_wav 统一转成 C:/... 正斜杠形态 —— first-run 的 portable_paths 只认这种形态,
        # JSON 转义的反斜杠形态它认不出(实测漏改 -> 换机后 TTS 起不来)。双保险: 读端也已做包内回落。
        if isinstance(_v.get("ref_wav"), str):
            _v["ref_wav"] = _v["ref_wav"].replace("\\", "/")
    return _dump(out)


def tts_calibration_json() -> str:
    """只留 arthur/mia 的语速/合成参数(+ 必需的 _default 兜底键)。"""
    d = _load_json("engine/data/tts_calibration.json")
    for key in ("target_wpm", "voice_params", "_fallback_factor"):
        if isinstance(d.get(key), dict):
            d[key] = {k: v for k, v in d[key].items()
                      if k in MINI_VOICES or str(k).startswith("_")}
    if isinstance(d.get("_comment"), str):
        d["_comment"] = d["_comment"] + " | 1020mini: 只保留 arthur/mia 两位老师的标定, 其余老师键已裁。"
    return _dump(d)


def tts_template_json() -> str:
    """TTS 模板(看门狗每次启动据此重渲 engine/data/tts_runtime.json)。
    只留 arthur/mia preset → 界面音色列表恰好两位(server.py /voices 由 preset 决定)。"""
    d = _load_json("tools/scripts/tts_demo_voice.json")
    for m in d.get("models", []):
        vp = m.get("voice_presets")
        if isinstance(vp, dict):
            m["voice_presets"] = {k: v for k, v in vp.items() if k in MINI_VOICES}
        if str(m.get("default_voice_preset", "")) not in MINI_VOICES:
            m["default_voice_preset"] = MINI_BOOT_REF
    return _dump(d)


def custom_config_json() -> str:
    """全局 custom_config.json 原指 data/customvideo/image(Brenda 演示序列, 已裁) → 指 Arthur 待机序列。"""
    return _dump([{"audiotype": 1, "imgpath": "data/customvideo/%s_idle" % MINI_BOOT_AVATAR}])


def app_json(cfg: dict) -> str:
    """出厂配置: 默认老师 = Arthur(与 8011 主位一致), 音色 = arthur, 清 key, 唤醒词去掉个人化的。"""
    c = dict(cfg)
    c["avatar_id"] = MINI_DEFAULT_AVATAR
    c["teacher_id"] = MINI_DEFAULT_AVATAR
    c["avatar_voice"] = MINI_BOOT_REF
    c["audiocpp_tts_voice"] = MINI_BOOT_REF
    c["llm_api_key"] = ""
    c["wake_words"] = list(MINI_WAKE_WORDS)
    return _dump(c)


# --------------------------------------------------------------------------
# 4) 出厂 soul(persona.json): 严格复刻 app/backend/server.py 的生成规则
# --------------------------------------------------------------------------
def _server_review_en() -> str:
    txt = open(SERVER_PY, encoding="utf-8", errors="ignore").read()
    m = re.search(r'REVIEW_EN\s*=\s*"""(.*?)"""', txt, re.S)
    if not m:
        raise SystemExit("[mini] server.py 里找不到 REVIEW_EN 字面量 —— 规则已变, 请核对")
    return m.group(1)


def _parse_teacher_md(path):
    """同 server.py parse_teacher_md: [字段]头 + 正文(去头保留 Markdown 原文)。"""
    fields, body, in_body = {}, [], False
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    for ln in lines:
        if not in_body:
            s = ln.strip()
            m = re.match(r"^\[([A-Za-z_]+)\]\s*(.*)$", s)
            if m:
                fields[m.group(1).lower()] = m.group(2).strip()
                continue
            if s.startswith("#") or not s:
                continue
            in_body = True
        if in_body:
            body.append(ln)
    return fields, "\n".join(body).strip()


def _shared_core(adult: bool = True) -> str:
    p = os.path.join(PERSONA_MD_DIR, "Shared-core-adult.md" if adult else "Shared-core.md")
    return _parse_teacher_md(p)[1]


def _render_system(core_body: str, t_body: str, fields: dict) -> str:
    t = core_body + "\n\n" + t_body
    g = fields.get("greeting", "")
    if g:
        t += ("\n\nYOUR FIXED INTRODUCTION LINE: \"" + g + "\"\n"
              "Use it ONLY when the student asks your name or asks you to introduce yourself. "
              "The system already speaks this line aloud once when the session opens, so NEVER "
              "repeat it on your own initiative and never open a reply with it — start directly "
              "by responding to what the student just said. Do not state your name in your "
              "first reply unless the student asks for it.")
    return t


def build_persona(avatar_id: str):
    fn = {"coach_arthur": "CoachArthur.md", "coach_mia": "CoachMia.md"}.get(avatar_id)
    if not fn:
        return None
    tp = os.path.join(PERSONA_MD_DIR, fn)
    if not os.path.isfile(tp):
        raise SystemExit("[mini] 人设 MD 缺失: " + tp)
    fields, t_body = _parse_teacher_md(tp)
    core = _shared_core(adult=True)          # Coach 系走成人底层(server.py 同规则)
    nm = fields.get("name") or os.path.splitext(fn)[0]
    return {
        "name": nm, "user": "同学", "language_default": "en",
        "teacher_id": avatar_id, "emoji": fields.get("emoji", ""),
        "title": fields.get("title", ""), "voice_preset": fields.get("voice_preset", ""),
        "avatar_id": avatar_id, "greeting": fields.get("greeting", ""),
        "system": _render_system(core, t_body, fields),
        "system_master": _render_system(core, t_body, fields),
        "session_review_mode": _server_review_en(),
        "stage": "adult_coach",
    }


def verify_persona_generator() -> str:
    """自检我的复刻: 用本模块规则重算 Mia, 与仓库现成 persona.json(真代码产物)逐字段比对。
    不一致就报错 —— 否则等于把一份"我以为是"的 soul 打进包。"""
    if not os.path.isfile(PERSONA_JSON):
        return "skip(无 persona.json 可比对)"
    cur = _load_json("app/backend/config/persona.json")
    if cur.get("teacher_id") != "coach_mia":
        return "skip(仓库 persona.json 当前不是 Mia: %s —— 换回 Mia 后重打包可做逐字段自检)" % cur.get("teacher_id")
    mine = build_persona("coach_mia")
    diff = sorted(k for k in set(list(cur.keys()) + list(mine.keys())) if cur.get(k) != mine.get(k))
    if diff:
        raise SystemExit("[mini] persona 生成器与 server.py 规则不一致, 差异字段: %s" % diff)
    return "ok(Mia 逐字段一致, %d 字段)" % len(mine)


# --------------------------------------------------------------------------
# 5) 出口: 覆写集合 + zip 自检清单
# --------------------------------------------------------------------------
def build_overrides() -> dict:
    return {
        "engine/data/custom_config.json": custom_config_json(),
        "engine/data/voice_registry.json": voice_registry_json(),
        "engine/data/tts_calibration.json": tts_calibration_json(),
        "tools/scripts/tts_demo_voice.json": tts_template_json(),
        "app/backend/config/app.json": app_json(_load_json("app/backend/config/app.json")),
        "app/backend/config/persona.json": _dump(build_persona(MINI_DEFAULT_AVATAR)),
    }


def forbidden_path(rel: str):
    """zip 自检: 命中 = 裁剪漏了(只看资产类路径; 代码注释里提到其他老师不算)。"""
    b = os.path.basename(rel)
    p = rel.split("/")
    if rel.startswith("engine/data/avatars/") and len(p) > 3 and p[3] not in MINI_TEACHERS:
        return "残留他师形象包"
    if rel.startswith("engine/data/customvideo/") and len(p) > 3 and p[3] not in MINI_IDLE_KEEP:
        return "残留他师待机序列"
    if rel.startswith("engine/data/custom_config_") and b != "custom_config_%s.json" % MINI_BOOT_AVATAR:
        return "残留他师引擎配置"
    if rel.startswith("engine/data/voice_anchor_") and not any(k in b for k in ("_arthur_ref", "_mia_ref")):
        return "残留他师音色锚点"
    if rel.startswith("audiocpp/webui/voice/") and not (b.startswith(("arthur_ref", "mia_ref")) or b == "prompt_text"):
        return "残留他师参考音频"
    if rel.startswith("avatar/persona/"):
        if len(p) != 3:
            return "残留人设备份子目录"
        return None if b in MINI_PERSONA_KEEP else "残留他师人设"
    if rel.startswith("app/backend/config/personas/"):
        return "残留默认老师灵魂备份"
    if rel.startswith("avatar/"):
        if b.startswith(MINI_MEDIA_KEEP):
            return None
        if len(p) == 3 and p[1] == "viseme" and b.endswith(".png"):
            return None
        if len(p) == 3 and p[1] == "persona":
            return None          # 上面已判定过白名单(这里只是别误判成人师的立绘)
        return "残留他师形象素材"
    return None


MUST_PATHS = [
    "engine/data/avatars/coach_arthur/avator_info.json",
    "engine/data/avatars/coach_mia/avator_info.json",
    "engine/data/custom_config_coach_arthur.json",
    "engine/data/voice_registry.json",
    "engine/data/tts_calibration.json",
    "audiocpp/webui/voice/arthur_ref.wav",
    "audiocpp/webui/voice/mia_ref.wav",
    "avatar/persona/CoachArthur.md",
    "avatar/persona/CoachMia.md",
    "avatar/Coach-Arthur.png",
    "avatar/Coach-Mia.png",
    "app/backend/config/persona.json",
    "app/backend/config/app.json",
    "tools/scripts/tts_demo_voice.json",
    "engine/web/desktop.html",
    "app/main.js",
    "engine/aiteacher_watchdog.py",
    "app/backend/server.py",
]
MUST_PREFIXES = ["engine/data/customvideo/coach_arthur_idle/"]


def check_names(arcs, require_assets: bool = True):
    """产物自检(zip 路径带 SpeakTosi/ 前缀, 目录树为相对路径 → 统一去前缀)。
    require_assets=False 用于 mini-code 仓库档: 大素材本来就不带, 不该算"缺件"。
    返回问题列表: 残留他师资产 / 缺关键文件。"""
    bad = []
    asset_pref = ("engine/data/avatars/", "engine/data/customvideo/")
    rels = [a.split("/", 1)[1] if a.startswith("SpeakTosi/") else a for a in arcs]
    for r in rels:
        why = forbidden_path(r)
        if why:
            bad.append("mini 自检: %s → %s" % (why, r))
    for p in MUST_PATHS:
        if not require_assets and p.startswith(asset_pref):
            continue
        if p not in rels:
            bad.append("mini 自检: 缺关键文件 %s" % p)
    if require_assets:
        for pre in MUST_PREFIXES:
            if not any(r.startswith(pre) for r in rels):
                bad.append("mini 自检: 缺关键目录 %s*" % pre)
    return bad


def _avatars_ids(src: str):
    """AST 精确取出 AVATARS 里的 avatar_id 列表(避免把 DEFAULT_CFG 等别处的字面量算进来)。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise SystemExit("[mini] 产物 server.py 语法错误: %s" % e)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "AVATARS" for t in node.targets):
            ids = []
            for el in getattr(node.value, "elts", []):
                if not isinstance(el, ast.Dict):
                    continue
                for k, v in zip(el.keys, el.values):
                    if isinstance(k, ast.Constant) and k.value == "avatar_id" and isinstance(v, ast.Constant):
                        ids.append(v.value)
            return ids
    return None


def audit_teacher_refs(texts):
    """产物自检(源码级): AVATARS 恰好两位、看门狗无旧主位/探针引用、音色恰好两位。
    texts = {rel: 文本内容}。返回问题列表 + 打印摘要。"""
    bad = []
    srv = texts.get("app/backend/server.py", "")
    if srv:
        ids = _avatars_ids(srv)
        others = [t for t in ("lingxue_cartoon", "lingfeng_cartoon", "lingyu_cartoon",
                              "lingyun_cartoon", "aiteacher_demo", "coach_marcus",
                              "coach_sarah", "coach_eleanor", "coach_kate", "coach_kai",
                              "coach_xiuxiu")
                  if '"avatar_id": "%s"' % t in srv]
        print("[mini] server.py AVATARS = %s; 他师 avatar_id 字面量残留: %s"
              % (sorted(ids) if ids else ids, others or "无"))
        if ids is None or set(ids) != set(MINI_TEACHERS) or others:
            bad.append("mini 自检: AVATARS 未收窄到两位(实为 %s, 残留 %s)" % (ids, others))
    wd = texts.get("engine/aiteacher_watchdog.py", "")
    if wd:
        faint = [t for t in ("'lingxue_cartoon'", "'aiteacher_demo'", "'REF_FILE', 'lingxue'",
                             "'REF_FILE', 'default'", "'voice': 'lingxue'") if t in wd]
        print("[mini] watchdog 残留旧主位/旧探针引用: %s" % (faint or "无"))
        if faint:
            bad.append("mini 自检: watchdog 仍有旧引用 %s" % faint)
    tts = texts.get("tools/scripts/tts_demo_voice.json", "")
    if tts:
        try:
            js = json.loads(tts)
            names = []
            for m in js.get("models", []):
                names += list((m.get("voice_presets") or {}).keys())
            print("[mini] 音色 preset: %s" % names)
            if sorted(names) != sorted(MINI_VOICES):
                bad.append("mini 自检: 音色 preset 不是 %s(实为 %s)" % (list(MINI_VOICES), names))
        except Exception as e:
            bad.append("mini 自检: tts 模板 JSON 解析失败 %s" % e)
    return bad