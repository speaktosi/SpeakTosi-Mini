import time
import os
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from avatars.base_avatar import BaseAvatar
from utils.logger import logger

# 1019 截断取证: 存在 flag 文件时把每个 SSE chunk 原样落盘(无 flag 时零开销)
_DBG_FLAG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'llm_stream_debug.on')
_DBG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'llm_stream_debug.log')


def _mk_dbg(sid, model, has_img, message):
    try:
        if not os.path.exists(_DBG_FLAG):
            return lambda *a, **k: None
        f = open(_DBG_LOG, 'a', encoding='utf-8')
        f.write('\n=== %s sid=%s model=%s img=%s msg=%r\n' % (time.strftime('%H:%M:%S'), sid, model, has_img, message))
        f.flush()

        def w(*parts):
            try:
                f.write(' '.join(str(p) for p in parts) + '\n')
                f.flush()
            except Exception:
                pass
        return w
    except Exception:
        return lambda *a, **k: None

# Built-in LLM providers. Each exposes an OpenAI-compatible chat completions
# endpoint; the active one is selected with --llm_provider (default: dashscope).
LLM_PROVIDERS = {
    "dashscope": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
    "tokenplan": {
        "api_key_env": "ALIBABA_CODING_PLAN_API_KEY",
        "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen3.8-flash",
    },
    "orcarouter": {
        "api_key_env": "ORCAROUTER_API_KEY",
        "base_url": "https://api.orcarouter.ai/v1",
        "default_model": "orcarouter/auto",
    },
    # local llama-server (Qwen3-8B-Instruct Q4_K_M GGUF), OpenAI-compatible
    "local": {
        "api_key_env": "LOCAL_LLM_DUMMY",
        "base_url": "http://127.0.0.1:8090/v1",
        "default_model": "qwen3-8b",
    },
}

# SpeakTosi 人设热加载（与桌面版共用同一份 persona.json，改一处两边生效）
PERSONA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "app", "backend", "config", "persona.json")
PERSONA_PATH = os.path.normpath(PERSONA_PATH)


def _cfg_json_path():
    """1020 后端 app.json 候选路径: 包内相对优先(解压/换机到任意盘都读自己的配置),
    再回退旧开发机绝对路径。engine/llm.py -> <包根>/app/backend/config/app.json"""
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.environ.get("SPEAKTOSI_APP_JSON") or "",
              os.path.normpath(os.path.join(here, "..", "app", "backend", "config", "app.json")),
              r"C:\Speaktosi\app\backend\config\app.json",
              r"D:\Software\ai-girlfriend\backend\config\app.json"):
        try:
            if p and os.path.exists(p):
                return p
        except Exception:
            pass
    return ""
_persona_cache = {"mtime": 0.0, "text": ""}

def _compose_persona(d):
    """1002fix: 把 persona.json 的『姓名』注入 system prompt（与 backend._sys_prompt 同步）。
    此前只读 system 正文, 设置页改名字对语音链完全无效——问'你叫什么'永远答人设里的旧名。
    1003: 无条件注入【输出铁律】纯文本禁令（与 backend 同文）——TTS+字幕不接受markdown,
    任何段位/手改灵魂都在此兜住。
    1004(四老师): 铁律按 language_default 选语种(少儿全英文人设→英文禁令, 防中英夹杂稀释人设);
    greeting 字段存在则钉死见面语口径——与 backend._sys_prompt 逐字同步。"""
    sp = d.get("system", "") or ""
    nm = (d.get("name") or "SpeakTosi").strip()
    lang = (d.get("language_default") or "")
    rules = ("\n【输出铁律】回复必须是纯口头口语文本：严禁 markdown 符号（星号加粗、井号标题、反引号）、"
             "严禁 bullet 或编号列表、严禁表格、严禁表情符号。你的每句话都会被 TTS 朗读并显示为直播字幕，"
             "格式符号会变成噪音。需要列举时用 and / then / also 口语串联即可。")
    if lang.startswith("en"):
        rules = ("\n【OUTPUT RULES】Reply in plain spoken text only. No markdown (**, #, backticks), "
                 "no bullet or numbered lists, no tables, no emoji or symbols. Every sentence is read "
                 "aloud by TTS and shown as live captions, so formatting becomes noise. To list things, "
                 "just say and / then / also.")
    _mw = _max_words()
    if _mw:
        sp = sp + _len_rule(_mw)
    base = (sp + "\n\n【你的名字】" + nm + "。学生叫你 " + nm + "，自我介绍或被问名字时必须用这个名字，"
            "不要使用任何其他老师名。") if sp else ("你是英语老师 " + nm + "，被问名字时回答 " + nm + "。")
    base += rules
    g = (d.get("greeting") or "").strip()
    if g:
        # 1005fix(重复介绍bug): 见面语由 overlay greetOnce 在连麦后主动朗读(/human echo),
        # 旧指令"At the start of a session open with exactly"让 LLM 第一轮又自我介绍一遍 → 学生听到两次。
        # 改为: 仅在被问名字/要求自我介绍时引用原文, 严禁主动开场或重复已播过的见面语。
        if str(d.get("language_default", "")).lower().startswith("en"):
            base += (f'\n[YOUR INTRODUCTION LINE] Your fixed self-introduction is: "{g}" '
                     f'Use it ONLY when the student asks your name or asks you to introduce yourself. '
                     f'The system already speaks this line aloud once when the session opens, so NEVER '
                     f'say it again on your own initiative and never open a reply with it — start directly '
                     f'by responding to what the student just said. Do not state your name in your first '
                     f'reply unless the student asks for it.')
        else:
            base += (f"\n【自我介绍口径】你的固定见面语是: {g}。仅当学生问你名字或要你自我介绍时才用这句原文。"
                     f"系统在会话开始时已经自动朗读过这句见面语，所以绝不要主动再说一遍、也绝不要用它开场——"
                     f"直接回应学生刚说的内容。")
    return base

def _len_rule(mw):
    """1020 回答长度档位铁律: 与 app/backend/server.py 逐字一致(两链同一口径, verify 脚本比对防漂移)。
    <=0 关(不限); <=60 极简 1-2 句(保开口速度); <=100 简答 2-4 句; >100 标准/长回答 3-6 句。"""
    n = int(mw or 0)
    if n <= 0:
        return ""
    if n <= 60:
        return (f"\n【长度铁律】Every reply must be at most {n} words in total, "
                f"split into short 1-2 sentence turns. Stop and let the student respond. "
                f"NEVER exceed {n} words, never list more than two items.")
    if n <= 100:
        return (f"\n【长度铁律】Every reply must be at most {n} words in total — usually 2-4 short "
                f"spoken sentences. React to the student's own words first, add one useful detail or "
                f"a gentle correction, then stop and let the student respond. NEVER exceed {n} words, "
                f"never list more than two items.")
    return (f"\n【长度铁律】Every reply must be at most {n} words in total — usually 3-6 short spoken "
            f"sentences. Start by reacting to the student's exact words, then add one concrete detail, "
            f"correction or example, and when it fits finish with one short question that invites them "
            f"to say more. NEVER exceed {n} words, never list more than two items.")


_MAXW_CACHE = [0, 0.0]
def _max_words():
    """1016卡顿根治: 热读 backend app.json 的 max_words(30s缓存, 与 _mem_bytes 同套路)。"""
    import time as _t, json as _j
    now = _t.time()
    if _MAXW_CACHE[1] and now - _MAXW_CACHE[1] < 30:
        return _MAXW_CACHE[0]
    v = 0
    try:
        _p = _cfg_json_path()
        if _p:
            d = _j.load(open(_p, encoding='utf-8'))
            v = int(d.get('max_words') or 0)
    except Exception:
        pass
    _MAXW_CACHE[0] = v; _MAXW_CACHE[1] = now
    return v


def _persona_system():
    """读 SpeakTosi persona；文件变了自动重载。失败退回通用助手人设。
    1004: mtime+size 双判据(防同秒覆写漏重载) + 读失败保留旧缓存(防原子替换瞬间撕裂误降级)。"""
    try:
        st = os.stat(PERSONA_PATH)
        sig = (st.st_mtime, st.st_size)
        if sig != _persona_cache.get("sig"):
            import json
            with open(PERSONA_PATH, encoding="utf-8") as f:
                d = json.load(f)
            _persona_cache["text"] = _compose_persona(d)
            _persona_cache["sig"] = sig
            _persona_cache["mtime"] = st.st_mtime
            logger.info(f"persona loaded from {PERSONA_PATH} ({len(_persona_cache['text'])} chars)")
        if _persona_cache["text"]:
            return _persona_cache["text"]
    except Exception as e:
        logger.warning(f"persona load failed: {type(e).__name__} {e}")
    return "你是一个知识助手，尽量以简短、口语化的方式输出"

def _load_env_keys():
    """Electron/cmd 启动的进程没有 Hermes 环境变量 → 回读 .env 注入云端 LLM key。
    1002: tokenplan 改用 ALIBABA_CODING_PLAN_API_KEY，一并回读。"""
    if os.environ.get("DASHSCOPE_API_KEY") and os.environ.get("ALIBABA_CODING_PLAN_API_KEY"):
        return
    import pathlib
    home = pathlib.Path.home()
    # 1019setup: 包内 app/backend/.env 是首次配置向导写入的唯一真源 —— 新机没有 Hermes 环境,
    # 必须回读它(否则引擎(--llm_provider tokenplan)拿不到 key)。
    for p in [home / "AppData" / "Local" / "hermes" / ".env", home / ".hermes" / ".env",
              pathlib.Path(__file__).resolve().parents[1] / "app" / "backend" / ".env"]:
        try:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip(); v = v.strip().strip('"').strip("'")
                if k.startswith("DASHSCOPE_API") or k.startswith("ALIBABA_CODING_PLAN_API"):
                    os.environ.setdefault(k, v)
            if os.environ.get("ALIBABA_CODING_PLAN_API_KEY"):
                logger.info(f"[cred] loaded from {p}")
                return
        except Exception:
            pass


def _llm_provider(opt) -> str:
    """Return the configured provider name, defaulting to dashscope."""
    return getattr(opt, 'llm_provider', 'dashscope') or 'dashscope'


def _llm_client(opt):
    """Create the OpenAI-compatible client for the configured provider."""
    from openai import OpenAI
    _load_env_keys()
    cfg = LLM_PROVIDERS.get(_llm_provider(opt), LLM_PROVIDERS['dashscope'])
    # 1012防卡顿: 默认600s超时会把对话吊死——40s流读取上限+最多重试1次。
    return OpenAI(
        api_key=os.getenv(cfg['api_key_env']) or 'sk-local',
        base_url=cfg['base_url'],
        timeout=40.0,
        max_retries=1,
    )


def _llm_model(opt) -> str:
    """Resolve the model name, falling back to the provider default."""
    cfg = LLM_PROVIDERS.get(_llm_provider(opt), LLM_PROVIDERS['dashscope'])
    return getattr(opt, 'llm_model', '') or cfg['default_model']


# ─── 会话记忆 ──────────────────────────────────────────────────────────────
# 1003修复"完全没记忆": 此前只发 system+当前单条user, 老师每句都当新对话。
# 按 sessionid 维护滚动历史(最近12条≈6轮), 与后端/chat口径一致。
_CONV = {}
_CONV_MAX = 200   # 1006v2: 硬顶防碎句刷屏; 主窗口改字节预算(与后端 /chat 同源同值)

# 1006v2 记忆容量: 读后端 app.json 的 memory_bytes(30s缓存热生效, 设置页改完不用重启引擎)。
# 预算估算: 双人英语正常语速≈150词/分×6.5字节/词≈60KB/课时, 默认120000=2课时。
_MEM_BYTES_CACHE = [None, 0.0]

def _mem_bytes():
    import time as _t, json as _j, os as _o
    now = _t.time()
    if _MEM_BYTES_CACHE[0] is not None and now - _MEM_BYTES_CACHE[1] < 30:
        return _MEM_BYTES_CACHE[0]
    v = 120000
    try:
        p = _cfg_json_path()
        if _o.path.exists(p):
            v = int(_j.load(open(p, encoding='utf-8')).get('memory_bytes', 120000) or 120000)
    except Exception:
        pass
    _MEM_BYTES_CACHE[0] = v
    _MEM_BYTES_CACHE[1] = now
    return v

def _hist_trim(h):
    """字节预算窗口: 从最新往前累加 utf-8 字节, 超预算砍最老; 同时受 _CONV_MAX 硬顶。"""
    budget = _mem_bytes()
    used = 0
    keep = len(h)
    for m in reversed(h):
        b = len(m['content'].encode('utf-8'))
        if used + b > budget:
            break
        used += b
        keep -= 1
    n_drop = max(keep, len(h) - _CONV_MAX)
    if n_drop > 0:
        del h[:n_drop]

def _hist_get(sid):
    return _CONV.setdefault(sid, [])

def _hist_push(sid, role, content):
    content = (content or "").strip()
    if not content:
        return
    # 1003防"一句话复发"v3(定罪实测: v2线25拦不住27字短肯定"Yes! That is the right one."入史,
    # 同窗×10崩出22/9/10/9碎句=雪崩燃料; 最短史对照组×10全163+。线提到40, 问号锚点仍豁免):
    # a) assistant死胡同碎句(<40且不以问号结尾)不入史——问号句是教学锚点必须留,
    #    "It is"/"Ah, I see"/短肯定句这类无钩子碎片都是雪崩燃料(干净史147字 vs 毒史1字)。
    # b) user连续碎句滚动合并进同一槽("A / B")——ASR误听连排纠正无论几轮压成一条,
    #    信息不丢但不再霸占12条窗口带偏模型节奏(线上翻车真因正是用户侧碎语刷屏)。
    h = _hist_get(sid)
    if role == 'assistant':
        if len(content) < 40 and not content.endswith(('?', '？')):
            # 1004答非所问根因fix: 碎assistant丢弃时, 连带回滚它对应的本轮user,
            # 否则历史堆积"连续user"→模型串台回应上一条消息(实证:问phrase答"thanks compliment")
            # 且失去长回复示范越答越短。保证严格user/assistant交替 > 保留毒user。
            if h and h[-1]['role'] == 'user':
                h.pop()
            return
    elif len(content) < 20 and h and h[-1]['role'] == 'user' and len(h[-1]['content']) < 60:
        h[-1]['content'] += ' / ' + content
        return
    h.append({'role': role, 'content': content})
    _hist_trim(h)

def note_spoken(sid, text):
    """1005重复介绍fix(结构性根因): /human echo 说出的话(overlay greetOnce 见面语等)
    必须记入本会话对话史——否则模型不知道'自己已经自我介绍过', 学生第一句话就又介绍一遍。
    prompt禁令实测只能压到75%(4轮漏1), 让模型'看见'自己说过的话才是治本。
    不走 _hist_push: 其assistant碎句过滤(<40字丢弃+回滚user)会吞掉短见面语。
    历史保持交替: 上一条已是assistant → 合并(同一轮连播多句fixed line)。"""
    text = (text or "").strip()
    if not text:
        return
    h = _hist_get(str(sid or 'default'))
    if h and h[-1]['role'] == 'assistant':
        h[-1]['content'] = (h[-1]['content'] + ' ' + text).strip()
    else:
        h.append({'role': 'assistant', 'content': text})
    _hist_trim(h)

# ---------- 1012 视觉老师(摄像头看图) ----------
# 配置热读后端 app.json(与 _mem_bytes 同套路, 30s 缓存)——看门狗命令行零改动。
_VISION_CFG_CACHE = [None, 0.0]
_EYE_CFG_CACHE = [None, 0.0]
_EYE_FRAME_CACHE = [None, 0.0]     # (dataURL或None, 取回时刻)

def _vision_cfg():
    import time as _t, json as _j
    now = _t.time()
    if _VISION_CFG_CACHE[0] is not None and now - _VISION_CFG_CACHE[1] < 30:
        return _VISION_CFG_CACHE[0]
    v = {"enabled": True, "model": ""}
    try:
        p = _cfg_json_path()
        if os.path.exists(p):
            d = _j.load(open(p, encoding='utf-8'))
            v = {"enabled": bool(d.get("vision_enabled", True)),
                 "model": str(d.get("vision_model") or "")}
    except Exception:
        pass
    _VISION_CFG_CACHE[0] = v; _VISION_CFG_CACHE[1] = now
    return v


def _eye_cfg():
    """挂眼开关热读(30s缓存, 与 _vision_cfg 同套路): eye_enabled 且 vision_enabled 才开。"""
    import json as _j
    now = time.time()
    if _EYE_CFG_CACHE[0] is not None and now - _EYE_CFG_CACHE[1] < 30:
        return _EYE_CFG_CACHE[0]
    v = True
    try:
        p = _cfg_json_path()
        if os.path.exists(p):
            d = _j.load(open(p, encoding='utf-8'))
            v = bool(d.get("eye_enabled", True)) and bool(d.get("vision_enabled", True))
    except Exception:
        pass
    _EYE_CFG_CACHE[0] = v; _EYE_CFG_CACHE[1] = now
    return v


def _eye_frame():
    """1013挂眼: 向 backend(8765) 共读最新采样帧(双引擎单源)。
    缓存3s防连环问打爆backend; 失败静默降级纯文本——挂眼是增强, 绝不拖垮主链路。"""
    if not _eye_cfg():
        return None
    now = time.time()
    if _EYE_FRAME_CACHE[1] and now - _EYE_FRAME_CACHE[1] < 3.0:
        return _EYE_FRAME_CACHE[0]
    img = None
    try:
        import urllib.request, json as _j2
        with urllib.request.urlopen('http://127.0.0.1:8765/frame?full=1', timeout=2) as r:
            d = _j2.loads(r.read().decode('utf-8', 'replace'))
        if d.get("has") and d.get("fresh"):
            img = d.get("image")
            if not (isinstance(img, str) and img[:24].lower().startswith("data:image/") and len(img) < 4_000_000):
                img = None
    except Exception:
        img = None
    _EYE_FRAME_CACHE[0] = img; _EYE_FRAME_CACHE[1] = now
    return img

_VISION_NOTE = """
[CAMERA VISION MODE] The student's latest message contains a photo taken with their device camera (webcam), attached below.
You can see it. Teach from the photo:
- First describe what you see in natural spoken English at the student's level: 2-4 short sentences, as a model for them to repeat.
- Pick the 1-3 most useful objects or words visible in the photo, say each one and put it in a simple example sentence.
- End with one easy question about the photo so the student has to speak.
- If the photo is too dark, too blurry or shows nothing useful, say so kindly and ask them to move the camera closer.
- Talk like a teacher looking through the student's camera. Never mention image files, base64 or processing.
"""

# 1013挂眼(A): 帧是环境画面而非学生特意拍的问题对象——老师以回答学生本身为主,
# 只有当画面与学生问题相关时才引用, 严禁每句都扯"你照片里的xx"(防误伤普通问答)。
_EYE_NOTE = """
[EYE MODE] A live frame from the student's camera is attached. It shows their surroundings, NOT necessarily what they are asking about.
- Answer the student's actual question first and fully. Use the frame only when it genuinely helps (e.g. they ask about something visible, or want to practice naming things nearby).
- Never force-talk about the frame in every reply; if it is irrelevant, ignore it.
"""

_TERM = ".!?\u3002\uff01\uff1f\u2026"
_QWORDS = ("do you", "did you", "what", "which", "who", "where", "when", "why",
           "how", "is your", "are you", "can you", "would you", "shall we",
           "shall i", "should we", "is it", "was it")


def _close_sentence(txt):
    """截断兜底: 结尾没有终止标点时补一个(疑问句补? 其余补.)。"""
    t = (txt or '').strip()
    if not t or t[-1] in _TERM:
        return t
    low = t.lower()
    return t + ('?' if any(w in low for w in _QWORDS) else '.')


def _join_cont(tail, cont):
    """把续写片段接到被截断的句尾: 去重叠 + 补分隔。"""
    c = (cont or '').strip()
    if not c:
        return tail
    words = tail.split()
    for k in range(min(6, len(words)), 0, -1):
        frag = ' '.join(words[-k:])
        if c.lower() == frag.lower():
            return tail                      # 只是一次自我重复, 不算续写
        if c.lower().startswith(frag.lower()):
            c = c[len(frag):].lstrip(' ,.')
            break
    if not c:
        return tail
    if c[0] in ",.!?'\u3002\uff0c\uff1f\uff01":
        return tail + c
    return tail + ' ' + c


def _continue_truncated(client, messages, partial, model_name, timeout=6.0):
    """截断自愈: 让模型从截断处续写, 只取缺失的词。失败返回 ''。"""
    if not partial or len(partial) > 800:
        return ''
    msgs = []
    for m in messages:
        c = m.get('content')
        if isinstance(c, list):
            c = ' '.join(p.get('text', '') for p in c
                         if isinstance(p, dict) and p.get('type') == 'text')
        msgs.append({'role': m.get('role'), 'content': c})
    _words = partial.split()
    _last = ' '.join(_words[-6:])
    msgs += [
        {'role': 'assistant', 'content': partial},
        {'role': 'user', 'content': '[SYSTEM] The reply above was cut off by the server mid-sentence. '
                                    'Its last words were: "' + _last + '". '
                                    'Write ONLY the words that come next, starting with the very next word '
                                    '(do not repeat any of those last words, no preamble, no quotes). '
                                    'Finish the sentence in as few words as possible.'},
    ]
    out = ''
    st = client.chat.completions.create(
        model=model_name, messages=msgs, stream=True, temperature=0.3,
        max_tokens=60, timeout=timeout,
        extra_body={'chat_template_kwargs': {'enable_thinking': False}})
    for ch in st:
        if len(ch.choices) > 0:
            d = ch.choices[0].delta.content
            if d:
                out += d
    return out.strip()

def llm_response(message, avatar_session:'BaseAvatar', datainfo:dict={}):
    try:
        opt = avatar_session.opt
        sid = str(getattr(opt, 'sessionid', 'default') or 'default')
        start = time.perf_counter()
        client = _llm_client(opt)
        model = _llm_model(opt)
        end = time.perf_counter()
        logger.info(f"llm Time init: {end-start}s,{message}")
        history = list(_hist_get(sid))
        # ---- 1012视觉老师: datainfo['image'] 带摄像头照片 → 多模态消息 ----
        # 记忆只存文字标记(不存base64, 防撑爆历史窗口); vision_enabled=false 时静默降级为纯文本。
        img = (datainfo or {}).get('image')
        vcfg = _vision_cfg()
        if img and not vcfg["enabled"]:
            img = None
        if img and not isinstance(img, str):
            img = None
        eye_mode = False
        if not img:                                    # 1013挂眼: /human 免快门自动附最新帧
            img = _eye_frame()
            eye_mode = bool(img)
        # 挂眼帧=环境画面(学生没特意拍), 用温和的 _EYE_NOTE; 主动发照片才用强教学 _VISION_NOTE
        sys_content = _persona_system() + ((_EYE_NOTE if eye_mode else _VISION_NOTE) if img else "")
        if img:
            user_msg = {"role": "user", "content": [
                {"type": "text", "text": message or "What do you see? Teach me with this photo."},
                {"type": "image_url", "image_url": {"url": img}}]}
        else:
            user_msg = {"role": "user", "content": message}
        messages = [{'role': 'system', 'content': sys_content}] + history + [user_msg]
        _hist_push(sid, 'user', (message + (" [老师正通过摄像头看着]" if eye_mode else " [学生发来一张照片]")) if img else message)
        completion = client.chat.completions.create(
            model=(vcfg["model"] or model) if img else model,
            messages=messages,
            stream=True,
            # 1004大脑fix: temp 0.9→0.55 教学场景要稳(0.9飘→串台/短); max_tokens兜底防网关默认截断
            temperature=0.55,
            max_tokens=600,
            # no_think: 云端 qwen 关思考，首token 2.98s→0.61s（直播延迟生命线）
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            # Display token usage in the last line of the streamed response.
            stream_options={"include_usage": True}
        )
        result=""
        full_parts = []   # 1003记忆修复: 流完后整段assistant回复入历史
        _dbg = _mk_dbg(sid, model, bool(img), message)
        first = True
        sent_n = 0   # 1016卡顿根治v2: 已推送句数(0=首句快切, 其后并批)
        for chunk in completion:
            if len(chunk.choices)>0:
                #print(chunk.choices[0].delta.content)
                if first:
                    end = time.perf_counter()
                    logger.info(f"llm Time to first chunk: {end-start}s")
                    first = False
                msg = chunk.choices[0].delta.content
                if msg is None:
                    _dbg('NOCHUNK-CONTENT fin=%r' % (getattr(chunk.choices[0], 'finish_reason', None),))
                    continue
                _dbg('CHUNK %r fin=%r' % (msg, getattr(chunk.choices[0], 'finish_reason', None)))
                full_parts.append(msg)   # 1003记忆: 攒整段回复, 流结束后入历史
                lastpos=0
                #msglist = re.split('[,.!;:，。！?]',msg)
                for i, char in enumerate(msg):
                    if char in ",.!;:，。！？：；、" :
                        result = result+msg[lastpos:i+1]
                        lastpos = i+1
                        # 1016卡顿根治v2: 首句短切=快开口; 后续并批(110字符≈2-3句)=省每请求固定开销+躲开极短句cross→zero双跑
                        han = any('\u4e00' <= ch <= '\u9fff' for ch in result)
                        if sent_n == 0:
                            need = 14 if han else 45
                        else:
                            need = 16 if han else 42
                        hard = char in "。！？!?…"
                        if len(result) > need or (hard and len(result) > (10 if han else (32 if sent_n == 0 else 14))):
                            txt = result.strip()
                            if txt:
                                _dbg('FLUSH %r' % (txt,))
                                logger.info(txt)
                                avatar_session.put_msg_txt(txt, datainfo)
                            result = ""
                            sent_n += 1
                result = result+msg[lastpos:]
        end = time.perf_counter()
        logger.info(f"llm Time to last chunk: {end-start}s")
        _dbg('TAIL %r  FULL %r' % (result.strip(), ''.join(full_parts)))
        # 1019 截断自愈: 网关/模型偶发在句子中间就 finish_reason=stop(实测 ~7%),
        # 表现为老师说半句就停(如 "Which Disney movie is your" 缺 "favorite?")。
        # 检测到尾部无终止标点 → 以已输出内容为 assistant 前缀续写一次补齐。
        _tail = result.strip()
        if _tail and (_tail[-1] not in ".!?。！？…"):
            try:
                _ct = _continue_truncated(client, messages, ''.join(full_parts).strip(),
                                          (vcfg["model"] or model) if img else model)
                _dbg('CONT %r' % (_ct,))
                _new = _close_sentence(_join_cont(_tail, _ct))
                if _new != _tail:
                    result = _new
                    full_parts.append(_new[len(_tail):])
                    logger.info(f'llm 截断自愈: {_tail!r} + {_ct!r} -> {_new!r}')
            except Exception:
                logger.exception('llm continuation fail:')
        if result.strip():
            avatar_session.put_msg_txt(result.strip(),datainfo)
        # 1003记忆修复: 整段assistant回复入会话历史, 下一轮才看得到"刚才说过的话"
        _hist_push(sid, 'assistant', "".join(full_parts))

    except Exception as e:
        logger.exception('llm exceptiopn:')
        return
