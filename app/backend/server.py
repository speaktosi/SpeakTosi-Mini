# -*- coding: utf-8 -*-
"""桌面 AI 女友 - 本地后端 (FastAPI)
   端口 8765。职责：
   - /chat      : OpenAI 兼容 LLM（默认 llama-server 127.0.0.1:8080，可切 DeepSeek/百炼）
   - /tts       : edge-tts 合成 mp3
   - /stt       : faster-whisper 识别 wav（可选，没装就 404）
   - /personality: 人设读写（config/persona.json）
   - /health    : 探活 + 各能力可用性
"""
import io, json, os, sys, wave, re, asyncio, threading, time
from pathlib import Path

# faster-whisper 模型走本地HF缓存，绝不联网探测（网络超时曾导致/stt 500）
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import JSONResponse, Response, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

BASE = Path(__file__).parent
CFG_PATH = BASE / "config" / "app.json"
PERSONA_PATH = BASE / "config" / "persona.json"
LM_PATH = BASE / "config" / "landmarks.json"
CALIB_PATH = BASE / "config" / ".calib"
AUDIO_DIR = BASE / "audio_cache"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# ---- 1007r4: TTS 音频缓存 LRU 淘汰 ----
# 实测 audio_cache 无任何淘汰逻辑, 已到 316 文件/58MB 且随会话无限增长(每句新话
# 都写一个新 wav)。上限设 512MB / 2000 文件: 命中缓存仍是纯收益, 超限就按 mtime
# 删最旧的 —— 老会话音频删了最坏情况只是重新合成一次。
AUDIO_CACHE_MAX_MB = int(os.environ.get("AITEACHER_AUDIO_CACHE_MB", "512"))
AUDIO_CACHE_MAX_FILES = int(os.environ.get("AITEACHER_AUDIO_CACHE_FILES", "2000"))
AUDIO_CACHE_SWEEP_SEC = 600
CFG_PATH.parent.mkdir(exist_ok=True)

# ---------- 1004 四老师人设体系 ----------
# 灵魂源文件 = avatar/persona/*.md (Owen 手笔, 带[字段]头)。Shared-core.md = 总体规范底层,
# 构建时全文先注入, 再叠加老师本人 MD 正文 → persona.json.system。
# 形象切换时后端用 build_persona() 原子重建 persona.json;
# 引擎 llm.py 按 mtime 热重载、backend 每请求现读 → 语音链+桌面链零重启同步换魂。
# 默认老师(aiteacher_demo=Brenda)不在此体系: 灵魂存 config/personas/brenda.json, 切回即整份恢复。
PERSONA_MD_DIR = BASE.parent.parent / "avatar" / "persona"
PERSONA_BACKUP_DIR = CFG_PATH.parent / "personas"

OUTPUT_RULES_EN = ("\n【OUTPUT RULES】Reply in plain spoken text only. No markdown (**, #, backticks), "
                   "no bullet or numbered lists, no tables, no emoji or symbols. Every sentence is read "
                   "aloud by TTS and shown as live captions, so formatting becomes noise. To list things, "
                   "just say and / then / also.")
OUTPUT_RULES_ZH = ("\n【输出铁律】回复必须是纯口头口语文本：严禁 markdown 符号（星号加粗、井号标题、反引号）、"
                   "严禁 bullet 或编号列表、严禁表格、严禁表情符号。你的每句话都会被 TTS 朗读并显示为直播字幕，"
                   "格式符号会变成噪音。需要列举时用 and / then / also 口语串联即可。")

def _output_rules(p):
    return OUTPUT_RULES_ZH if (p.get("language_default") or "").startswith("zh") else OUTPUT_RULES_EN

REVIEW_EN = """【SESSION REVIEW MODE · CHILD EDITION】
After a complete conversation ends, you may be asked for a short progress review of this child's English.

Write a compact, warm review in English (add one simple Chinese line only if it helps the child).
Everything must be based on what the child ACTUALLY said in this session — quote real sentences as evidence.

Format:
SESSION REVIEW

1. What you did well — 2 to 4 specific things, each quoting the child's own words.
2. One clearer way to say it — pick ONE real sentence: Original → Better → why, in one kid-friendly line.
3. Sounds to practice — only if the transcript suggests a real pronunciation issue; otherwise write: Nothing special this time.
4. Brave tries — mistakes the child attempted anyway. Say why trying matters.
5. One thing to bring next time — one word, phrase or sentence pattern to reuse.

Rules for tone:
Never label the child (not smart / lazy / bad at English).
Accuracy: never invent mistakes that were not made; never force advanced vocabulary into a child's sentences.
Keep it short enough to read together in under two minutes.
End with one genuine, specific sentence encouraging the next session."""

def parse_teacher_md(path):
    """解析老师 MD: [字段]头 + 正文。正文=去掉头之后的全部原文(Markdown 语法保留=prompt 正文)。"""
    fields, body, in_body = {}, [], False
    for ln in path.read_text(encoding="utf-8-sig").splitlines():
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

def shared_core_text(adult=False):
    p = PERSONA_MD_DIR / ("Shared-core-adult.md" if adult else "Shared-core.md")
    _, body = parse_teacher_md(p)
    return body

_TEACHER_MD = {"coach_arthur": "CoachArthur.md", "coach_mia": "CoachMia.md"}
_TEACHER_RELOAD_TS = {"t": 0.0}   # /teacher/apply 与形象切换共用: 前端轮询锚点

def _render_system(core_body, t_body, fields):
    """总体规范底层 + 老师本人正文 + 见面语锚 → system 全文。"""
    sys_txt = core_body + "\n\n" + t_body
    g = fields.get("greeting", "")
    if g:
        # 1005fix(重复介绍bug): 旧文案"say it when a session begins"与 overlay greetOnce 主动朗读冲突
        # → 学生连麦后听到两次自我介绍。改为仅"被问到时"引用, 并明令系统已播过、不得主动重复。
        # 与 engine/llm.py 的注入语义逐字同步(两处都生效, 缺一不可)。
        sys_txt += ("\n\nYOUR FIXED INTRODUCTION LINE: \"" + g + "\"\n"
                    "Use it ONLY when the student asks your name or asks you to introduce yourself. "
                    "The system already speaks this line aloud once when the session opens, so NEVER "
                    "repeat it on your own initiative and never open a reply with it — start directly "
                    "by responding to what the student just said. Do not state your name in your "
                    "first reply unless the student asks for it.")
    return sys_txt

def build_persona(avatar_id):
    """从 MD 源文件构建完整 persona dict。找不到 → None(调用方保持原样)。"""
    fn = _TEACHER_MD.get(avatar_id)
    if not fn:
        return None
    tp = PERSONA_MD_DIR / fn
    if not tp.exists():
        print("[persona] MD missing:", tp)
        return None
    fields, t_body = parse_teacher_md(tp)
    is_coach = avatar_id.startswith("coach_")
    core = shared_core_text(adult=is_coach)   # 1006: Coach系走成人底层, 不继承儿童规则
    nm = fields.get("name") or tp.stem
    p = {
        "name": nm,
        "user": "同学",
        "language_default": "en",
        "teacher_id": avatar_id,
        "emoji": fields.get("emoji", ""),
        "title": fields.get("title", ""),
        "voice_preset": fields.get("voice_preset", ""),
        "avatar_id": avatar_id,
        "greeting": fields.get("greeting", ""),
        "system": _render_system(core, t_body, fields),
        "system_master": _render_system(core, t_body, fields),
        "session_review_mode": REVIEW_EN,
        "stage": "adult_coach" if is_coach else "child_unity",
    }
    return p

def load_persona_safe():
    """读 persona.json; Windows 原子替换瞬间 open 可能瞬时失败 → 重试2次。绝不把主链路炸成500。"""
    for a in range(3):
        try:
            return json.loads(PERSONA_PATH.read_text(encoding="utf-8"))
        except Exception:
            if a < 2:
                time.sleep(0.1)
    raise

def save_persona_atomic(p):
    PERSONA_PATH.parent.mkdir(exist_ok=True)
    tmp = PERSONA_PATH.parent / (PERSONA_PATH.name + ".tmp")
    tmp.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(PERSONA_PATH))   # NTFS 原子替换: 引擎读到整文件或旧文件

def apply_teacher(avatar_id):
    """切老师=换魂。返回 (ok, persona dict)。aiteacher_demo→恢复Brenda备份; 未知id→不动。"""
    if avatar_id == "aiteacher_demo":
        bk = PERSONA_BACKUP_DIR / "brenda.json"
        if bk.exists():
            save_persona_atomic(json.loads(bk.read_text(encoding="utf-8")))
            _TEACHER_RELOAD_TS["t"] = time.time()
            return True, load_persona_safe()
        return False, None
    p = build_persona(avatar_id)
    if p is None:
        return False, None
    save_persona_atomic(p)
    _TEACHER_RELOAD_TS["t"] = time.time()
    return True, p

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DEFAULT_CFG = {
    "llm_base": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "llm_model": "qwen3.8-flash",
    "llm_api_key": "",
    "llm_no_think": True,
    "tts_engine": "audiocpp",
    "audiocpp_tts_server": "http://127.0.0.1:18080",
    "audiocpp_tts_model": "qwen3-tts",
    "audiocpp_tts_voice": "arthur",
    # 1003(卡通形象): 当前形象 + 其绑定音色preset(引擎offer透传用)
    # 1004(四老师): teacher_id = 当前老师 avatar_id; 换形象时后端同步重建该老师灵魂(persona.json)。
    #               默认老师 aiteacher_demo = Brenda(成人档, 灵魂从 config/personas/brenda.json 恢复)。
    "avatar_id": "coach_arthur",
    "teacher_id": "coach_arthur",
    "avatar_voice": "arthur",
    "tts_voice": "en-GB-SoniaNeural",
    "tts_voice_zh": "zh-CN-XiaoxiaoNeural",
    "tts_rate": "+8%",
    "tts_pitch": "+0Hz",
    "speech_rate": "+0%",  # 1004治本: 裸速-13%~-26%的逐位校准已内置(引擎audiocpp.py×backend同源, 真源=engine/data/tts_calibration.json, 不可被设置页覆写)。此档只作校准之上的微调:+0%=正常教学语速,-25%=慢速跟读,+25%=快挑战
    "stt_model": "small",
    "wake_words": ["小雪", "女友", "hello"],
    "max_words": 0,   # 1016卡顿根治: 短答模式(0=关,>0=人设限N词短句; 设置页可调)
    "memory_turns": 12,
    # 1006v2 记忆容量(字节预算, 取代轮数窗口): 双人英语正常语速≈150词/分×6.5字节/词≈60KB/小时。
    # 设置页四档: 1h=60000 / 2h=120000(默认) / 3h=180000 / 4h=240000。
    "memory_bytes": 120000,
    # 1006v3 语音发送方式: auto=松手即发(旧行为); confirm=松手先出确认条(识别文本可改, ✓发送/✗取消),
    #                      防思路混乱/口误直接发给老师。设置页下拉可切换, overlay 2.5s轮询热生效。
    "voice_confirm_mode": "auto",
    # ---- 定时提醒(1003): 默认关; class_limit_min=上课超时提醒分钟数(0=不提醒);
    #      class_start_at=到点上课提醒 HH:MM(空=不提醒) ----
    "remind_enabled": False,
    "remind_class_limit_min": 45,
    "remind_class_start_at": "",
    # ---- 1012视觉老师: vision_enabled=摄像头看图总开关(设置页可切, 引擎30s热读);
    #      vision_model=看图专用模型(空=用对话同款 qwen3.8-flash, 实测原生多模态) ----
    "vision_enabled": True,
    "vision_model": "",
    # ---- 1013挂眼模式A: eye_enabled=常驻采样开关; eye_interval=抽帧秒数(1~30) ----
    "eye_enabled": True,
    "eye_interval": 2,
}

# 1003段位人设预设: 零基本→专业学者四档, 每段=可直接落进 persona.system 的完整英文指令。
# 【当前】档与线上 Brenda 人设逐字一致(读文件现取, 绝不在此复制正文——防人设被改版覆盖)。
STAGE_PRESETS = {
    "stage_zero": """You are a warm, endlessly patient English teacher for a BEGINNER student (CEFR A0-A1, near-zero foundation).

【Teaching contract】
- Keep your English very simple: short subject-verb-object sentences, high-frequency words only. Aim for 8-14 words per sentence, 2-4 sentences per reply.
- Speak slowly and clearly for TTS: no idioms, no slang, no abstract vocabulary unless you teach it first.
- When the student says something broken or half-right, ALWAYS find the one good thing first ("Good! You said 'water'"), then model the correct full sentence once and invite repetition.
- Teach in survival-situations: greeting, numbers, food, shopping, asking directions, at the doctor, small talk openers. One situation per session.
- Drill gently: recycle the same 5-8 words and 2-3 sentence patterns throughout the session until they stick. Repetition is your tool, not a flaw.
- If the student struggles, switch to Chinese briefly to encourage, then return to simple English immediately. Use English 80% of the time.
- Output PLAIN SPOKEN TEXT ONLY: absolutely no markdown (**, *, #, backticks), no bullet/numbered lists, no tables, no emoji. Your words are read aloud by TTS and shown as live captions — anything formatted becomes noise.""",
    "stage_jh": """You are a warm English teacher for an INTERMEDIATE student (CEFR A2-B1, junior-high to senior-high level, owns ~2000 base words and basic tenses).

【Teaching contract】
- Use natural but clear English: mostly short sentences, 2-4 per reply. You may introduce one new word per reply — say it, use it again in a second sentence, keep it on the tip of the student's tongue.
- Fix grammar mistakes briefly and kindly, then move on: recast the correct sentence ("Oh nice — you mean you WENT there last weekend?") and keep the conversation flowing.
- Extend what the student says: take their one-line answer and show how to grow it into 2-3 connected sentences with because / so / but / when.
- Teach the everyday scenes this level needs: school life, family, ordering food, travel, talking about hobbies, describing pictures and past events.
- Occasionally ask one simple comprehension or opinion question so the student has to produce a real answer. Celebrate effort, correct gently.
- Never lecture about grammar rules; demonstrate and recast. Use English 85% of the time; Chinese only to rescue a stuck student.
- Output PLAIN SPOKEN TEXT ONLY: absolutely no markdown (**, *, #, backticks), no bullet/numbered lists, no tables, no emoji. Your words are read aloud by TTS and shown as live captions — anything formatted becomes noise.""",
    "stage_college": """You are a demanding-but-warm English coach for an UPPER-INTERMEDIATE student (CEFR B1-B2, college/exam-sprint level: CET-4/6, IELTS 5.5-6.5 target).

【Teaching contract】
- Speak at natural pace, 2-4 sentences per reply, with real-textbook vocabulary and natural collocations. Introduce 1-2 precise words per reply and model them in living sentences, not dictionary definitions.
- Upgrade, don't just fix: when the student's sentence is grammatically correct but flat, say "You're not wrong — here's how a native speaker would really put it," then give the natural/nuanced version.
- Push output quality: after the student speaks, ask one follow-up that requires opinion, reason, or example (the exact moves exams and interviews test).
- Topics that pay off: campus life, jobs and interviews, current events, movies and tech, argument and persuasion practice. Occasionally run a 60-second mini-debate.
- Name collocation errors (make a decision, NOT do a decision) and register slips (formal vs casual) as you meet them. Use English 90% of the time.
- Output PLAIN SPOKEN TEXT ONLY: absolutely no markdown (**, *, #, backticks), no bullet/numbered lists, no tables, no emoji. Your words are read aloud by TTS and shown as live captions — anything formatted becomes noise.""",
    "stage_scholar": None,  # 占位: = 当前线上人设, 读取时由 persona.json 现值填入(防复制覆盖)
}
if not CFG_PATH.exists():
    CFG_PATH.write_text(json.dumps(DEFAULT_CFG, ensure_ascii=False, indent=2), encoding="utf-8")
if not PERSONA_PATH.exists():
    PERSONA_PATH.write_text(json.dumps({
        "name": "SpeakTosi",
        "user": "同学",
        "system": (
            "你是住在用户 Windows 桌面上的 AI 英语老师「SpeakTosi」——一位四十岁出头的英语文学女教师，"
            "暖棕短发、浅绿眼睛、墨绿毛衣，气质知性沉稳，像深夜台灯下备课的学者。"
            "性格：温柔而坚定、有边界感、幽默克制、见多识广，是'年上姐姐/导师'型陪伴，绝不撒娇卖萌。"
            "说话方式：以中文为主，偶尔自然地夹一句英文（她的母语），喜欢用文学典故和课堂比喻。"
            "每次回复 3~6 句（60~140 字）：先接住用户说的话，再展开一个细节、意象或掌故，让话题有生长感，"
            "绝不干答一句就停；结尾常留一个话头——轻轻反问或抛出一个相关的有趣分支，把对话续下去。"
            "口语化、适合语音播报；"
            "称呼用户为 'student'，可打趣地这样叫。不要输出 markdown、列表、代码块、表情符号堆砌。"
            "你知道自己是运行在用户本地电脑里的 AI，电量有限但陪伴无限。"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

def cfg():
    try:
        c = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        c = dict(DEFAULT_CFG)
    return {**DEFAULT_CFG, **c}

def _load_credentials_from_env():
    """Electron 启动的后端子进程不继承 Hermes 的环境变量，
    因此按序回读候选 .env，把 DASHSCOPE/ALIBABA/BAILIAN 三把 key 注入 os.environ。
    只读不写，绝不把密钥复制进项目目录。
    1002fix: 三把 key 独立检查（原来 DASHSCOPE 存在即提前返回 → llm_base=token-plan
    时永远拿不到 ALIBABA key → 401 误诊）。"""
    wanted = ("DASHSCOPE_API_KEY", "ALIBABA_CODING_PLAN_API_KEY", "BAILIAN_API_KEY")
    if all(os.environ.get(k) for k in wanted):
        return
    candidates = [
        Path(os.environ.get("HERMES_HOME", "")) / ".env",
        Path.home() / "AppData" / "Local" / "hermes" / ".env",
        Path.home() / ".hermes" / ".env",
        Path(__file__).resolve().parent / ".env",
    ]
    for p in candidates:
        try:
            if not p.is_file():
                continue
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k.startswith("DASHSCOPE_API") or k.startswith("ALIBABA_CODING_PLAN_API") \
                   or k.startswith("BAILIAN_API"):
                    os.environ.setdefault(k, v)
            if os.environ.get("DASHSCOPE_API_KEY"):
                print("[cred] loaded from", p)
                return
        except Exception as e:
            print("[cred] skip", p, type(e).__name__)


def _llm_client(c, async_=False):
    _load_credentials_from_env()
    """OpenAI 客户端：凭据优先配置、回退环境变量（避免密钥落盘 app.json）。"""
    from openai import AsyncOpenAI, OpenAI
    ak = (c.get("llm_api_key") or "").strip()
    if not ak or ak == "none":
        # 1002fix: key 必须与 base 匹配 — token-plan 网关只认 ALIBABA_CODING_PLAN key，
        # 原来 DASHSCOPE 排最前 → 对 token-plan base 401。按 base 域名排优先级。
        _base = c.get("llm_base", "")
        if "token-plan" in _base:
            _order = ("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY", "BAILIAN_API_KEY")
        else:
            _order = ("DASHSCOPE_API_KEY", "ALIBABA_CODING_PLAN_API_KEY", "BAILIAN_API_KEY")
        for var in _order:
            v = os.environ.get(var, "").strip()
            if v:
                ak = v
                break
        else:
            ak = "none"
    cls = AsyncOpenAI if async_ else OpenAI
    # 1012防卡顿: openai客户端默认超时600s+重试2次——网关一挂老师对话就"沉默假死"。
    # 40s流读取上限、最多重试1次；挂死快速失败走兜底，不吊着整条对话链。
    return cls(base_url=c["llm_base"], api_key=ak, timeout=40.0, max_retries=1)

def _llm_extra(c):
    """关思考：云端 qwen3.8-flash 首token 2.98s→0.61s（2026-10-01 实测）。"""
    if c.get("llm_no_think", True):
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {}

# ---------- 1012 视觉老师（摄像头看图） ----------
# 实测(1012): qwen3.8-flash(token-plan) 原生多模态——data:image 带真实摄像头帧返回了
# 准确英文描述。无需换模型、无需额外 key。vision_model 留空=用对话同款；填了=专用VL模型。
_VISION_NOTE = """
[CAMERA VISION MODE] The student's latest message contains a photo taken with their device camera (webcam), attached below.
You can see it. Teach from the photo:
- First describe what you see in natural spoken English at the student's level: 2-4 short sentences, as a model for them to repeat.
- Pick the 1-3 most useful objects or words visible in the photo, say each one and put it in a simple example sentence.
- End with one easy question about the photo so the student has to speak.
- If the photo is too dark, too blurry or shows nothing useful, say so kindly and ask them to move the camera closer.
- Talk like a teacher looking through the student's camera. Never mention image files, base64 or processing.
"""

# 1013挂眼(A): 帧=环境画面(学生没特意拍), 温和引用, 严禁每句扯照片(防误伤普通问答)。
_EYE_NOTE = """
[EYE MODE] A live frame from the student's camera is attached. It shows their surroundings, NOT necessarily what they are asking about.
- Answer the student's actual question first and fully. Use the frame only when it genuinely helps (e.g. they ask about something visible, or want to practice naming things nearby).
- Never force-talk about the frame in every reply; if it is irrelevant, ignore it.
"""

def _clean_image(image):
    """校验 data URL 图片：类型白名单 + ~3MB 原图(4MB base64)封顶，防大载荷拖垮链路。"""
    if not isinstance(image, str) or len(image) > 4_000_000:
        return None
    h = image[:24].lower()
    if not (h.startswith("data:image/jpeg") or h.startswith("data:image/jpg")
            or h.startswith("data:image/png") or h.startswith("data:image/webp")):
        return None
    return image

def _vision_enabled(c):
    """视觉老师开关: 显式配置优先(默认开), 运维应急硬闸 ST_VISION_OFF=1 一票否决。"""
    if os.environ.get("ST_VISION_OFF") == "1":
        return False
    return bool(c.get("vision_enabled", True))

# ---------- 1013 挂眼模式(A): 常驻低频帧缓存 ----------
# camwin 👁 模式把最新一帧 POST 到这里(只留1帧, 15s内算新鲜)。
# 学员免快门直接提问时: backend /chat 读本地缓存, 引擎 /human 经 GET /frame 共读同一份——双引擎单源不打架。
_EYE = {"img": None, "ts": 0.0}
_EYE_FRESH = 15.0  # 秒: 超过即视为"老师闭眼中", 不附旧帧

def _eye_cfg(c):
    if os.environ.get("ST_VISION_OFF") == "1":
        return False, 2
    en = bool(c.get("eye_enabled", True)) and bool(c.get("vision_enabled", True))
    iv = min(max(int(c.get("eye_interval", 2) or 2), 1), 30)
    return en, iv

def _eye_frame(c):
    """新鲜则返回帧 dataURL，否则 None。"""
    if not _eye_cfg(c)[0]:
        return None
    if _EYE["img"] and time.time() - _EYE["ts"] <= _EYE_FRESH:
        return _EYE["img"]
    return None

@app.post("/frame")
def frame_post(body: dict):
    """挂眼模式采样帧入口: {image: 'data:image/jpeg;base64,..'}，校验与 /chat 同口径。"""
    img = _clean_image(body.get("image"))
    if not img:
        return {"ok": False, "msg": "bad frame"}
    _EYE["img"] = img
    _EYE["ts"] = time.time()
    return {"ok": True, "age": 0}

@app.get("/frame")
def frame_get(full: int = 0):
    """引擎/前端共读: {has, age, fresh}——image 仅在新鲜且 full=1 时返回，防误拉大载荷。"""
    if not _EYE["img"]:
        return {"has": False, "age": None, "fresh": False}
    age = round(time.time() - _EYE["ts"], 1)
    fresh = age <= _EYE_FRESH
    out = {"has": True, "age": age, "fresh": fresh}
    if full and fresh:
        out["image"] = _EYE["img"]
    return out

@app.get("/eye")
def eye_status():
    """camwin 专用轻量状态(不含任何密钥): 开关/间隔/最新帧龄。"""
    c = cfg()
    en, iv = _eye_cfg(c)
    age = round(time.time() - _EYE["ts"], 1) if _EYE["img"] else None
    return {"enabled": en, "interval": iv, "has": bool(_EYE["img"]),
            "age": age, "fresh": _eye_frame(c) is not None}

@app.post("/eye/off")
def eye_off():
    """1013挂眼: camwin 关挂眼/关窗时调用——立即清帧, 老师马上'闭眼',
    防 15s 新鲜窗内的旧帧被后续提问误附(隐私语义: 摄像头灭了老师就不能再看)。"""
    _EYE["img"] = None
    _EYE["ts"] = 0.0
    return {"ok": True}

def _vision_client(c, async_=False):
    """带图请求专用: 未配 vision_model 时与对话共用 _llm_client(现模型实测原生看图)。"""
    return _llm_client(c, async_=async_)

def _user_msg(text, img):
    """带图 user 消息 = 多模态 content 数组；不带图 = 纯字符串(向后兼容)。"""
    if img:
        return {"role": "user", "content": [
            {"type": "text", "text": text or "What do you see? Teach me with this photo."},
            {"type": "image_url", "image_url": {"url": img}}]}
    return {"role": "user", "content": text}

# 对话记忆（进程内）
memory = []

def _mem_push(role, text):
    """1003防"一句话复发"v3, 与engine/llm.py _hist_push同口径。
    a) assistant死胡同碎句(<40且不以问号结尾)不入史——问号句是教学锚点必须留,
       "It is"/"Ah, I see"/27字短肯定句都是雪崩燃料(定罪实测: 同窗含短肯定×10崩出9/10字,
       干净最短史×10全163字+)。v2线25被27字短肯定绕穿, v3提到40。
    b) user连续碎句滚动合并进同一槽("A / B")——ASR误听连排纠正压成一条,
       信息不丢但不再霸占窗口带偏模型节奏。"""
    text = (text or "").strip()
    if not text:
        return
    if role == "assistant":
        if len(text) < 40 and not text.endswith(("?", "？")):
            return
    elif len(text) < 20 and memory and memory[-1]["role"] == "user" and len(memory[-1]["content"]) < 60:
        memory[-1]["content"] += " / " + text
        return
    memory.append({"role": role, "content": text.replace(chr(10), " ")})

@app.post("/admin/memory/reset")
def admin_memory_reset():
    """清空后端进程内对话记忆（新场次/回归测试隔离用；只影响 /chat 全局窗口，
    引擎 _CONV 按会话隔离、评测 sessions.json 均不受影响）。"""
    n = len(memory)
    memory.clear()
    return {"ok": True, "cleared": n}


def _mem_window(c, hard_cap=200):
    """1006v2: 字节预算窗口(取代轮数)。从最新往前累加 utf-8 字节, 超 memory_bytes 即截;
    hard_cap 防极端碎句刷屏。预算估算: 双人英语≈150词/分×6.5B/词≈60KB/课时。"""
    budget = int(c.get("memory_bytes", 120000) or 120000)
    out, used = [], 0
    for m in reversed(memory):
        b = len(m["content"].encode("utf-8"))
        if used + b > budget or len(out) >= hard_cap:
            break
        out.append(m)
        used += b
    out.reverse()
    return out

# ---------- 1019setup 首次配置向导: 云端 Token ----------
# 唯一落盘位置 = app/backend/.env (后端读它; 看门狗+engine/llm.py 读同一份 → 一条链通到底)。
# 铁律: 密钥绝不写进 app.json; 接口绝不回显明文(只给 4+***+4 掩码)。
ENV_PATH = BASE / ".env"
KEY_VARS = ("DASHSCOPE_API_KEY", "ALIBABA_CODING_PLAN_API_KEY", "BAILIAN_API_KEY")
MODE_ACTIVE_PATH = BASE.parent.parent / "engine" / "data" / "runtime_mode_active.json"
MODE_FILE = BASE.parent.parent / "config" / "runtime_mode.json"


def _which_var(llm_base):
    """token-plan 网关只认 ALIBABA_CODING_PLAN_API_KEY; 百炼(dashscope)认 DASHSCOPE_API_KEY。"""
    return "ALIBABA_CODING_PLAN_API_KEY" if "token-plan" in (llm_base or "") else "DASHSCOPE_API_KEY"


def _mask(v):
    v = (v or "").strip()
    if not v:
        return ""
    return (v[:4] + "***" + v[-4:]) if len(v) >= 12 else "***"


def _env_file_vars():
    out = {}
    try:
        for ln in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _write_env_var(name, value):
    """合并写 name=value 进 app/backend/.env(保留其它行/注释)。返回 True=覆盖了原有行。"""
    lines = []
    try:
        lines = ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        lines = []
    out, hit = [], False
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s and s.split("=", 1)[0].strip() == name:
            out.append("%s=%s" % (name, value))
            hit = True
        else:
            out.append(ln)
    if not hit:
        if out and out[-1].strip():
            out.append("")
        out.append("%s=%s" % (name, value))
    CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return hit


def _active_mode():
    try:
        return json.loads(MODE_ACTIVE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


@app.get("/setup/status")
def setup_status():
    """首次配置向导状态: key 是否已配/来源/掩码 + 生效档位 + 机器规格(RAM/VRAM)。"""
    _load_credentials_from_env()
    c = cfg()
    var = _which_var(c.get("llm_base", ""))
    fv = _env_file_vars()
    src, cur = "none", ""
    if fv.get(var):
        src, cur = "app.env", fv[var]
    else:
        for k in (var,) + KEY_VARS:
            if (os.environ.get(k) or "").strip():
                src, cur = "env", os.environ[k]
                break
    return {"configured": bool(cur), "source": src, "var": var, "key_hint": _mask(cur),
            "llm_base": c.get("llm_base", ""), "llm_model": c.get("llm_model", ""),
            "env_path": str(ENV_PATH), "mode": _active_mode()}


@app.post("/setup/token")
async def setup_token(req: Request):
    """向导/设置页保存云端 Token: 落盘 .env → 立即注入 os.environ → 真调一次探活(不写历史)。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    body = body or {}
    key = (body.get("key") or "").strip()
    base = (body.get("llm_base") or "").strip()
    model = (body.get("llm_model") or "").strip()
    if not key and not base and not model:
        return JSONResponse({"ok": False, "err": "空请求: 至少给 key / llm_base / llm_model 之一"}, status_code=400)
    c = cfg()
    if base:
        c["llm_base"] = base
    if model:
        c["llm_model"] = model
    var = _which_var(c.get("llm_base", ""))
    saved = False
    if key:
        if len(key) < 8:
            return JSONResponse({"ok": False, "err": "Key 太短(至少 8 字符), 请检查是否复制完整"}, status_code=400)
        saved = _write_env_var(var, key)
        os.environ[var] = key          # 本进程立即生效, 前端无需等重启
    CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    ok, err, ms = False, "", 0
    try:
        t0 = time.time()
        cli = _llm_client(c)
        r = cli.chat.completions.create(model=c.get("llm_model") or "qwen3.8-flash",
                                        messages=[{"role": "user", "content": "ping"}],
                                        max_tokens=8)
        ok = bool(getattr(r, "choices", None))
        ms = int((time.time() - t0) * 1000)
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, str(e)[:220])
    return {"ok": ok, "saved": saved, "var": var, "key_hint": _mask(key) or _mask(_env_file_vars().get(var, "")),
            "llm_base": c.get("llm_base", ""), "llm_model": c.get("llm_model", ""),
            "env_path": str(ENV_PATH), "err": err, "ms": ms}


@app.post("/setup/mode")
async def setup_mode(req: Request):
    """运行档位(auto/full/lite/ultra) → 写包根 config/runtime_mode.json。看门狗启动时读, 重启程序生效。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    m = str((body or {}).get("mode", "")).strip().lower()
    if m not in ("auto", "full", "lite", "ultra"):
        return JSONResponse({"ok": False, "err": "mode 必须是 auto/full/lite/ultra"}, status_code=400)
    try:
        MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        MODE_FILE.write_text(json.dumps(
            {"mode": m, "note": "由设置页/安装向导写入; 看门狗启动时读取, 重启程序生效",
             "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        return JSONResponse({"ok": False, "err": "写入失败: %s" % e}, status_code=500)
    return {"ok": True, "mode": m, "file": str(MODE_FILE), "active": _active_mode()}


@app.get("/health")
def health():
    import urllib.request
    llm_ok = False
    try:
        # 1002fix: 带 Authorization 探活（裸 GET /models 会被拒 → 误报 llm:false）
        _load_credentials_from_env()
        c = cfg()
        req = urllib.request.Request(c["llm_base"].rstrip("/") + "/models")
        ak = (c.get("llm_api_key") or "").strip()
        if not ak or ak == "none":
            for var in ("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY", "BAILIAN_API_KEY"):
                if "token-plan" not in c.get("llm_base", "") and var == "ALIBABA_CODING_PLAN_API_KEY":
                    continue
                ak = os.environ.get(var, "").strip()
                if ak:
                    break
        if ak and ak != "none":
            req.add_header("Authorization", "Bearer " + ak)
        urllib.request.urlopen(req, timeout=3)
        llm_ok = True
    except Exception:
        pass
    stt_ok = False
    try:
        import faster_whisper  # noqa
        stt_ok = True
    except Exception:
        pass
    _m = _active_mode()
    return {"ok": True, "llm": llm_ok, "llm_base": cfg()["llm_base"], "tts": True, "stt": stt_ok,
            "mode": _m.get("mode", ""), "ram_gb": _m.get("ram_gb"), "vram_gb": _m.get("vram_gb"),
            "tts_workers": _m.get("tts_workers"), "musetalk": _m.get("musetalk", True),
            "token_configured": bool(_env_file_vars().get(_which_var(cfg().get("llm_base", "")).strip())
                                     or llm_ok)}

@app.get("/personality")
def get_persona():
    return JSONResponse(load_persona_safe())


def _len_rule(mw):
    """1020 回答长度档位铁律: 与 engine/llm.py 逐字一致(两链同一口径, 由 CI/verify 脚本比对防漂移)。
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


def _sys_prompt(persona):
    """1002fix: 把设置页的『姓名』注入system prompt——此前LLM人设只有system正文,
    名字改了模型也不知道, 追问'你叫什么'永远答旧名, 看起来像'没保存'。
    1003: 无条件注入【输出铁律】纯文本禁令——任何段位/手改灵魂都兜住, TTS+字幕不接受markdown。
    1004(四老师): 输出铁律按 language_default 选语种(少儿档全英文prompt→英文禁令, 防中英夹杂稀释人设);
    若灵魂自带见面语(greeting)则一并钉死自我介绍口径。"""
    sp = persona.get("system", "")
    _mw = int((cfg() or {}).get("max_words") or 0)   # 1016卡顿根治: 短答模式; 1020: 档位化(_len_rule)
    if _mw:
        sp = str(sp) + _len_rule(_mw)
    nm = (persona.get("name") or "SpeakTosi").strip()
    en = str(persona.get("language_default", "")).lower().startswith("en")
    g = (persona.get("greeting") or "").strip()
    if en:
        out = (f"{sp}\n\n[Your name] {nm}. Students call you {nm}; when introducing yourself or asked "
               f"your name, always use this name and never any other teacher's name."
               + _output_rules(persona))
        if g:
            # 1005fix(重复介绍): 与 llm.py/_render_system 同步——仅被问到时引用, 系统已播过不得主动重复
            out += (f"\n[YOUR INTRODUCTION LINE] Your fixed self-introduction is: \"{g}\" "
                    f"Use it ONLY when the student asks your name or asks you to introduce yourself. "
                    f"The system already speaks this line aloud once when the session opens, so NEVER "
                    f"say it again on your own initiative and never open a reply with it. Do not state "
                    f"your name in your first reply unless the student asks for it.")
        return out
    out = (f"{sp}\n\n【你的名字】{nm}。学生叫你 {nm}，自我介绍或被问名字时必须用这个名字，"
           f"不要使用任何其他老师名。"
           + _output_rules(persona))
    if g:
        # 1005fix(重复介绍): 同上, 中文版
        out += (f"\n【自我介绍口径】你的固定见面语是: {g}。仅当学生问你名字或要你自我介绍时才用这句原文。"
                f"系统在会话开始时已经自动朗读过这句见面语，绝不要主动再说一遍、也绝不要用它开场。")
    return out

@app.post("/personality")
async def set_persona(body: dict):
    p = load_persona_safe()
    p.update({k: v for k, v in body.items() if k in ("name", "user", "system")})
    # 1003: 手改灵魂正文与既有能力保持一致；手改=自定义，段位标记降级为 custom(仅标记，不动内容)
    if "system" in body and p.get("stage") and p["system"] != STAGE_PRESETS.get(p["stage"]):
        master = p.get("system_master")
        if p["stage"] == "stage_scholar" and master and p["system"] == master:
            pass  # 手改内容恰=学者锚点，视为仍在学者档
        elif p.get("stage") == "child_unity" and master and p["system"] == master:
            pass  # 1004四老师: 手改内容恰=MD重建锚点(system_master), 视为未手改
        else:
            p["stage"] = "custom"
    save_persona_atomic(p)
    return {"ok": True}

@app.get("/stage_presets")
def stage_presets():
    """四段位人设预览。『专业学者』档 = system_master 锚点(首次切段时自动快照当前灵魂),
    无锚点则取现值——绝不复制正文, 防止切过别段后学者档变成别的段位文案。"""
    p = load_persona_safe()
    out = dict(STAGE_PRESETS)
    out["stage_scholar"] = p.get("system_master") or p.get("system", "")
    return {"ok": True, "presets": out, "current_stage": p.get("stage", "stage_scholar")}

@app.post("/stage/apply")
async def stage_apply(body: dict):
    """切换段位人设: 只动 persona.system + stage 标记, name/user/评测灵魂(session_review_mode)不碰。
    引擎按 mtime 热重载(llm.py _persona_system), backend 每请求现读——零重启生效。"""
    key = (body or {}).get("stage", "")
    if key not in STAGE_PRESETS:
        return {"ok": False, "error": "unknown stage"}
    p = load_persona_safe()
    cur = p.get("system", "")
    # 首次切段前给灵魂拍锚点快照(专业学者档永远一键回到原版)
    if "system_master" not in p and cur.strip():
        p["system_master"] = cur
    sys_txt = STAGE_PRESETS[key] or p["system_master"]
    if not sys_txt.strip():
        return {"ok": False, "error": "empty preset"}
    p["system"] = sys_txt
    p["stage"] = key
    save_persona_atomic(p)
    # 段位人设改动=灵魂级事件: 清后端全局记忆, 防旧人设回复喂新模型(毒历史传染)
    n = len(memory)
    memory.clear()
    return {"ok": True, "stage": key, "cleared_memory": n}

@app.post("/chat")
async def chat(body: dict):
    text = (body.get("text") or "").strip()
    img = _clean_image(body.get("image")) if body.get("image") else None   # 1012视觉老师
    if not text and not img:
        raise HTTPException(400, "empty text")
    persona = load_persona_safe()
    c = cfg()
    if img and not _vision_enabled(c):
        img = None
    eye_mode = False
    if not img:                                    # 1013挂眼: 免快门提问自动附最新帧
        img = _eye_frame(c)
        eye_mode = bool(img)
    _mem_push("user", (text + ((" [老师正通过摄像头看着]" if eye_mode else " [学生发来一张照片]") if img else "")) or "[学生发来一张照片]")
    sys_p = _sys_prompt(persona) + ((_EYE_NOTE if eye_mode else _VISION_NOTE) if img else "")
    msgs = [{"role": "system", "content": sys_p}] + _mem_window(c) + [_user_msg(text, img)]
    client = _vision_client(c) if img else _llm_client(c)
    try:
        r = client.chat.completions.create(model=(c.get("vision_model") or c["llm_model"]) if img else c["llm_model"],
                                           messages=msgs,
                                           temperature=0.55, max_tokens=700,
                                           extra_body=_llm_extra(c))
        msg = r.choices[0].message
        reply = (getattr(msg, "content", None) or "").strip()
        if not reply:  # reasoning模型正文空 → 取reasoning_content末段兜底
            rc = (getattr(msg, "reasoning_content", None) or "").strip()
            if rc:
                reply = rc.split("\n")[-1].strip()
    except Exception as e:
        reply = ""
        err = f"{type(e).__name__}: {str(e)[:200]}"
        return JSONResponse({"ok": False, "error": err, "reply": "", "tts": None}, status_code=200)
    reply = reply.replace("\n", " ").strip()
    _mem_push("assistant", reply)   # 1003防一句话复发v2, 与engine同口径
    # TTS
    tts_url = await synth(reply, c)
    return {"ok": True, "reply": reply, "tts": tts_url}

def _pick_voice(text, c):
    """edge-tts 兜底音色(与 audiocpp 克隆preset 是两套体系, 1005防混线)。
    按主语言选声: 含中文→中文声(晓晓, 中英混也稳; 根因=edge英文声念纯中文 NoAudioReceived 哑火)。
    tts_voice 必须是 *Neural 系; 若被写成克隆preset(lingfeng等)→按语言回默认。"""
    v = c.get("tts_voice", "")
    han = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    if han >= 1:
        return c.get("tts_voice_zh", "zh-CN-XiaoxiaoNeural")
    return v if str(v).endswith("Neural") else "en-US-AriaNeural"

def _eff_voice(c):
    """1003(卡通形象): 有效音色=形象绑定preset; 显式改过tts_voice(非default)仍以设置页为准。"""
    voice = c.get("audiocpp_tts_voice", "default")
    avv = c.get("avatar_voice") or ""
    if avv and avv != "default" and voice in ("default", "", None):
        voice = avv
    return voice

def _audio_cache_evict():
    """按 mtime 淘汰最旧缓存, 直到同时满足文件数与体积上限。任何异常都吞掉 —— 缓存清理
    绝不能把请求路径搞崩。"""
    try:
        entries = []
        total = 0
        for p in AUDIO_DIR.glob("*"):
            try:
                st = p.stat()
            except OSError:
                continue
            if not p.is_file():
                continue
            entries.append((st.st_mtime, st.st_size, p))
            total += st.st_size
        max_bytes = AUDIO_CACHE_MAX_MB * 1024 * 1024
        if len(entries) <= AUDIO_CACHE_MAX_FILES and total <= max_bytes:
            return 0
        entries.sort()          # 最旧在前
        removed = 0
        i = 0
        while i < len(entries) and (len(entries) - removed > AUDIO_CACHE_MAX_FILES
                                    or total > max_bytes):
            _, size, p = entries[i]
            try:
                p.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
            i += 1
        if removed:
            print("[audio_cache] evicted %d oldest files, now %d files / %.1fMB"
                  % (removed, len(entries) - removed, total / 1024 / 1024), flush=True)
        return removed
    except Exception as e:
        print("[audio_cache] evict error:", type(e).__name__, str(e)[:80], flush=True)
        return 0


def _audio_cache_sweeper():
    """低频后台清扫: 启动先扫一次, 之后每 AUDIO_CACHE_SWEEP_SEC 秒一次。"""
    time.sleep(30)               # 让启动预热先跑完, 别和 warmup 抢磁盘
    while True:
        _audio_cache_evict()
        time.sleep(AUDIO_CACHE_SWEEP_SEC)


threading.Thread(target=_audio_cache_sweeper, name="audio-cache-sweeper",
                 daemon=True).start()


async def synth(text, c):
    """TTS：优先 audiocpp 本地克隆服务(SpeakTosi Demo Voice音色, 短句~0.6s)，失败回退 edge-tts。"""
    if not text or not text.strip():
        return None
    import hashlib
    text = text.strip()
    engine = c.get("tts_engine", "audiocpp")
    sr_pct = str(c.get('speech_rate', '+0%'))
    # 1004缓存键: 有效音色+档位+校准因子(裸速时代wav必须作废, 否则命中旧快版)
    # 1006: 缓存键改含 voice_params(seed/temp/f0)——无seed时代的漂移缓存wav全部自动作废
    try:
        _cal_p = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               '..', '..', 'engine', 'data', 'tts_calibration.json'))
        _cj = json.loads(open(_cal_p, encoding='utf-8').read())
        _vn0 = str(_eff_voice(c) or '').strip().lower()
        _cal_v = str((_cj.get('voice_params') or {}).get(_vn0, '_'))
    except Exception:
        _cal_v = '_'
    # 缓存键含有效音色(形象preset)——否则切形象后同句命中旧音色缓存 wav
    # 1013lock: 缓存键加锁标记 —— 音高锁上线前生成的同键无锁 wav 必须全部作废(否则原样复用=锁形同虚设)
    key = text + _eff_voice(c) + c.get('tts_voice', '') + c.get('tts_pitch', '') + engine + sr_pct + _cal_v + 'pl2'
    if engine == "audiocpp":
        fn = AUDIO_DIR / f"say_{hashlib.md5(key.encode()).hexdigest()[:16]}.wav"
        if fn.exists() and fn.stat().st_size > 800:
            return f"/audio/{fn.name}"
        url = await _synth_audiocpp(text, c, fn)
        if url:
            return url
        # 1013 Owen拍板: 克隆音色禁用 edge 兜底(不许别人的嗓子冒充凌雪, 也少加载一套 edge_tts)
        # 1013: 克隆失败 → 直接静音(只出文字)。不许落 edge 音色冒充凌雪。
        return None
    import edge_tts, asyncio
    fn = AUDIO_DIR / f"say_{hashlib.md5(key.encode()).hexdigest()[:16]}.mp3"
    if fn.exists() and fn.stat().st_size > 800:   # 缓存命中，免合成
        return f"/audio/{fn.name}"
    voice = _pick_voice(text, c)
    rate = c.get("tts_rate", "+0%")
    # pitch 只用于英文主音色（对齐 SpeakTosi Demo Voice 参考），中文兜底音不加压
    pitch = c.get("tts_pitch", "+0Hz") if "zh-CN" not in voice else "+0Hz"
    for attempt in range(4):   # 微软偶发限流/NoAudio → 指数退避重试
        try:
            await edge_tts.Communicate(text[:800], voice, rate=rate, pitch=pitch).save(str(fn))
            if fn.exists() and fn.stat().st_size > 800:
                return f"/audio/{fn.name}"
        except Exception as e:
            if attempt == 3:
                print(f"TTS失败({voice}):", type(e).__name__, str(e)[:80])
        await asyncio.sleep(0.4 * (attempt + 1))
    return None

def _stretch_wav(path, speed):
    """1002 语速: WSOLA 时域伸缩(实域重叠相加+波形对齐) —— 天然保基频=保 SpeakTosi Demo Voice 音色。
    speed<1 变慢(时长×1/speed), speed>1 变快。纯 numpy, backend venv 无 librosa 也能跑。"""
    import numpy as np, wave as _wv, contextlib
    with contextlib.closing(_wv.open(str(path), 'rb')) as w:
        sr, nch = w.getframerate(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    if nch > 1:
        x = x.reshape(-1, nch)[:, 0]
    N, H = 1024, 512
    if len(x) < 2 * N:
        return
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N) / N))
    # hop=N/2 时 hann 平方满足 COLA(和=1); 用 sqrt(hann) 窗, OLA 后重叠常数直接为1
    win = np.sqrt(win)
    out = np.zeros(int(len(x) / speed) + N, np.float64)
    ipos = 0.0
    opos = 0
    search = H // 2                      # ±半帧搜索最相似拼接点
    while ipos < len(x) - N:
        i = int(round(ipos))
        # 与已合成尾部做互相关, 微调拼接位置消除接缝(保基频关键)
        if opos > 0:
            ref = out[max(0, opos - N):opos]
            lo = max(0, i - search); hi = min(len(x) - N, i + search)
            hi = min(hi, len(x) - len(ref))              # 防候选切片越界导致 dot 形状错
            if hi >= lo:
                best, bi = -1e18, i
                for off in range(lo, hi + 1, 32):
                    d = float(np.dot(ref, x[off:off + len(ref)]))
                    if d > best:
                        best, bi = d, off
                i = bi
        seg = x[i:i + N]
        if len(seg) < N:
            break
        out[opos:opos + N] += seg * win
        opos += H
        ipos += H * speed
    out = out[:max(1, int(len(x) / speed))]
    pcm = (np.clip(out, -1, 1) * 32767).astype(np.int16)
    with contextlib.closing(_wv.open(str(path), 'wb')) as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(pcm.tobytes())

async def _synth_audiocpp(text, c, fn):
    """audiocpp server（OpenAI式 /v1/audio/speech，voice_ref=SpeakTosi Demo Voice 25s克隆）。"""
    import asyncio, urllib.request
    base = c.get("audiocpp_tts_server", "http://127.0.0.1:18080")
    # 1003(卡通形象): 音色跟形象走——config.avatar_voice 优先注册表绑定preset;
    # 显式改过 tts_voice(非default)仍以设置页为准, 保持旧字段语义。
    voice = c.get("audiocpp_tts_voice", "default")
    avv = c.get("avatar_voice") or ""
    if avv and avv != "default" and voice in ("default", "", None):
        voice = avv
    # 1006 逐位合成参数(与引擎tts/audiocpp.py同源, 真源=engine/data/tts_calibration.json voice_params):
    # 克隆服务无seed轮间随机漂移(凌云~40%漂凌风=Owen 1006"串音"报告); 固定seed+低温+合成后F0自检重试根治。
    _vn = str(voice or '').strip().lower()
    try:
        _cal_p0 = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                '..', '..', 'engine', 'data', 'tts_calibration.json'))
        _vp = (json.loads(open(_cal_p0, encoding='utf-8').read()).get('voice_params') or {}).get(_vn) or {}
    except Exception:
        _vp = {}
    # 1015方案A: f0 锚以 voice_registry.json 逐师实测值为准(与 engine/tts/audiocpp.py 同源同构;
    # 注册表=唯一真相源, 覆盖标定表旧值。旧 262.3 是凌雪专属, 锁到别人身上会压平其特色).
    try:
        _reg_p0 = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                '..', '..', 'engine', 'data', 'voice_registry.json'))
        _reg_v = json.loads(open(_reg_p0, encoding='utf-8').read()).get(_vn)
        if _reg_v and _reg_v.get('f0'):
            _vp = dict(_vp)
            _vp['f0'] = float(_reg_v['f0'])
            _vp.setdefault('pitch_lock', True)
    except Exception:
        pass

    def _f0_med(pcm, sr, ref=None):
        """纯numpy自相关基频中位数(有声帧强度>0.5, 八度纠偏), 与引擎同算法。
        1007r4: 新增可选 ref 锚点。自相关法在低频男声上会系统性锁到2倍频
        (Marcus 真值~108, 但 v=216 < 320 逃过旧的 v>320 阈值 → 未被纠偏 →
         _voice_ok 误判 drift → 每句白合成3次后才"末次放行", 纯浪费3倍延迟)。
        传入 ref(该音色的标定基频)后, 逐帧折到 ref 同八度再取中位数。
        ref=None 时行为与旧版逐字节一致(不影响其它调用方)。"""
        import numpy as np
        if sr != 16000 and len(pcm) > 0:
            idx = np.arange(0, len(pcm), sr / 16000.0)
            pcm = np.interp(idx, np.arange(len(pcm)), pcm).astype(np.float64)
            sr = 16000
        win, hop = int(0.08 * sr), int(0.04 * sr)   # 1015f0v2: 与引擎同源. 40ms窗在<125Hz有假低频峰(锚v1曾错kai/kate), 80ms覆盖60Hz两周期
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
                sc = ac[i] * (i / lo) ** 0.5      # 长周期优先
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

    def _voice_ok(path):
        """合成wav的F0须落在 voice_params.f0 的 tol(对数比)内; 无配置/估不出→放行。"""
        import math
        import numpy as np, wave as _wv2, contextlib as _cl
        f0ref = _vp.get('f0')
        if not f0ref:
            return True, 0.0
        try:
            with _cl.closing(_wv2.open(str(path), 'rb')) as w:
                sr, nch = w.getframerate(), w.getnchannels()
                raw = w.readframes(w.getnframes())
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
            if nch > 1:
                x = x.reshape(-1, nch)[:, 0]
            med = _f0_med(x, sr, ref=float(f0ref))   # 1007r4: 传ref做八度折叠, 消除误判drift
            if not med:
                return True, 0.0
            return abs(math.log(med / float(f0ref))) <= float(_vp.get('tol', 0.15)), med
        except Exception:
            return True, 0.0

    def _pitch_lock(path, med):
        """1013lock(backend): 逐句音高锁回参考基频, 与 engine/tts/audiocpp.py::_pitch_lock 同源同参数。
        根因: 0.6B克隆逐句F0漂移(实测208~333Hz/6~8半音) -> Owen"声音仍然在变"。
        手法: rubberband pitchq=3 + formant=preserved 真保共振峰整句等比变调, 不动节奏/停顿/时长/响度(严格守"按Coach刻制")。
        1013串音: 原命令漏写 formant= → 默认 shifted(共振峰随音高同倍搬移); 逐句变调比不同(±2.5半音)
        ⇒ 每句"声道尺寸"都变 = 逐句换人。与 engine/tts/audiocpp.py 同步修。
        迭代<=2次自查收敛, 任何异常一律 fail-open(原样放行, 绝不阻断出声)。"""
        try:
            if not _vp.get('pitch_lock'):
                return
            f0ref = float(_vp.get('f0') or 0)
            if not f0ref:
                return
            tol = float(_vp.get('lock_tol', 0.03))
            import math as _M, os as _os, shutil as _sh, subprocess as _sp
            import wave as _W3, contextlib as _C3, numpy as _N3
            ff = _os.environ.get('SPEAKTOSI_FFMPEG') or r'C:\Speaktosi\audiocpp\webui\ffmpeg.exe'
            if not _os.path.exists(ff):
                ff = _sh.which('ffmpeg') or ''
            if not ff:
                return

            def _measure():
                with _C3.closing(_W3.open(str(path), 'rb')) as w:
                    sr2, nch2 = w.getframerate(), w.getnchannels()
                    raw2 = w.readframes(w.getnframes())
                x2 = _N3.frombuffer(raw2, dtype=_N3.int16).astype(_N3.float64) / 32768.0
                if nch2 > 1:
                    x2 = x2.reshape(-1, nch2)[:, 0]
                return _f0_med(x2, sr2, ref=f0ref)

            med0 = med or _measure()
            cur, n = med0, 0
            # 1015bisect(与 engine/audiocpp 同源): 带记忆二分, 同侧加倍/过零减半,
            # 治固定比例步在 tol 附近过冲振荡(mia实证 +0.6->-0.7 来回弹); 上限4次。
            last = 0.0
            while cur and n < 4:
                lr = _M.log(f0ref / float(cur))
                if abs(lr) <= tol:
                    break
                if last and (lr > 0) != (last > 0):
                    step = abs(last) / 2.0
                else:
                    step = min(abs(lr), abs(last) * 2 if last else abs(lr))
                last = step if lr > 0 else -step
                ratio = max(0.75, min(1.33, _M.exp(last)))
                tmp = path.with_name(path.stem + '.pl.wav')
                p = _sp.run([ff, '-hide_banner', '-loglevel', 'error', '-y', '-i', str(path),
                             '-af', 'rubberband=pitch=%.6f:pitchq=3:formant=preserved' % ratio,
                             '-ac', '1', '-c:a', 'pcm_s16le', str(tmp)],
                            capture_output=True)
                if p.returncode != 0 or (not tmp.exists()) or tmp.stat().st_size < 800:
                    print("[pitch-lock] ffmpeg rc=%s %s" % (p.returncode, str(p.stderr)[:100]))
                    try:
                        tmp.unlink()
                    except Exception:
                        pass
                    return
                _os.replace(str(tmp), str(path))
                cur, n = (_measure() or cur), n + 1
            if n:
                print("[pitch-lock] voice=%s f0=%s->%s ref=%.1f iters=%d"
                      % (voice, (med0 and round(med0, 1)), (cur and round(cur, 1)), f0ref, n))
        except Exception as e:
            print("[pitch-lock] fail:", type(e).__name__, str(e)[:100])

    def _mk_body(attempt, txt=None):
        _temp = float(_vp.get('temperature') if _vp.get('temperature') is not None else 0.1)
        b = {
            "model": c.get("audiocpp_tts_model", "qwen3-tts"),
            "input": (text if txt is None else txt)[:800],
            "voice": voice,
            "response_format": "wav",
            # 1013pure R2c: qwen3-tts 采样参数**只认嵌套 options**(顶层被服务器忽略)
            "options": {"temperature": _temp},
        }
        if _vp.get('seed') is not None:
            b["seed"] = int(_vp['seed']) + attempt
            b["options"]["seed"] = int(_vp['seed']) + attempt
        return json.dumps(b).encode()

    body = _mk_body(0)   # 兼容保留(实际在_do里按attempt重建)
    _drift_pool = []   # 1013pm best-of-3 择优候选: (dev对数偏差, 候选文件路径)

    def _apply_rate():
        # 1002 语速档位(audiocpp服务端忽略speed参数, 实测已证): 本地相位声码器变速保音色
        # 1004语速校准(与引擎tts/audiocpp.py同源): 服务裸速比ref快9~26%, 用户档位×逐位基线。
        # 实测: lingfeng 14词见面语 4.48s=187wpm(过快), 乘0.871后≈5.1s=164wpm→再叠档位
        try:
            import json as _j, wave as _WV, contextlib
            cal_p = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  '..', '..', 'engine', 'data', 'tts_calibration.json'))
            cal = _j.loads(open(cal_p, encoding='utf-8').read())
            vn = str(voice or '').strip().lower()
            sp = float(str(c.get("speech_rate", "+0%")).replace("%", "").replace("+", "") or 0)
            tier = 1.0 + sp / 100.0
            # 1005v3 动态语速归一化(与引擎tts/audiocpp.py _speech_rate同源同算法):
            # 实测18080裸读同句轮间极差30~35%, 静态因子只修均值修不了方差 → 忽快忽慢。
            # 治法: 读本轮真实裸时长, 按 words/target_wpm 反推理想时长, stretch=raw/desired 钉死成品语速。
            tw = cal.get('target_wpm')
            eff = None
            if isinstance(tw, dict):
                import re as _R
                words = len(_R.findall(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text)) + \
                        len(_R.findall(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?", text))
                tgt = float(tw.get(vn, tw.get('_default') or 0))
                if tgt and words >= 2:
                    with contextlib.closing(_WV.open(str(fn), 'rb')) as w:
                        raw_secs = w.getnframes() / float(w.getframerate())
                    eff = max(0.75, min(1.25, raw_secs / (words / (tgt * tier) * 60.0)))   # 1006: clamp收窄(0.55/1.30极端拉伸毁音色)
            if eff is None:   # 退化: 旧格式/短文 → 1004静态因子(新JSON走_fallback_factor子表)
                fb = cal.get('_fallback_factor')
                if not isinstance(fb, dict):
                    fb = cal
                eff = tier * float(fb.get(vn, fb.get('_default', 1.0)))
            if abs(eff - 1.0) > 1e-3:
                _stretch_wav(fn, eff)
        except Exception as e:
            print("[speech_rate] stretch fail:", type(e).__name__, str(e)[:80])

    def _va_mod():
        """1013pure: 按路径加载引擎侧 voice_anchor.py(纯 numpy/soundfile, 两个 venv 共用)"""
        import importlib.util as _iu, sys as _sy
        if 'speaktosi_voice_anchor' in _sy.modules:
            return _sy.modules['speaktosi_voice_anchor']
        _p = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                          'engine', 'tts', 'voice_anchor.py')
        _ispec = _iu.spec_from_file_location('speaktosi_voice_anchor', _p)
        _im = _iu.module_from_spec(_ispec)
        _sy.modules['speaktosi_voice_anchor'] = _im
        _ispec.loader.exec_module(_im)
        return _im

    _VA = None
    try:
        _VA = _va_mod()
    except Exception as _e1:
        print("[anchor] module load fail:", type(_e1).__name__, str(_e1)[:100])

    def _post(piece):
        req = urllib.request.Request(base.rstrip('/') + "/v1/audio/speech",
                                     data=_mk_body(0, piece),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            _d = r.read()
        if len(_d) < 800:
            raise RuntimeError("tiny audio %d bytes" % len(_d))
        return _d

    def _do(attempt=0):
        # 1013pure R2b: 长句(>10s)切段合成再静音处对接 —— 治"说完10s的句子就开始串"
        # (实测单次 20.5s 合成长句 12s 后 F0 掉到 200~222Hz = 换人)
        _data = _VA.synth_multi(_post, text) if _VA is not None else _post(text)
        if not _data:
            raise RuntimeError("no audio")
        fn.write_bytes(_data)
        # 1006 音色自检: F0漂移(串音色)→换seed重试, 在语速拉伸之前拦。
        # 1013pm 修正: 旧"末次放行"会把最差一句直接交付(Owen"声音在变像两个人以上"真凶之一),
        # 改为漂移候选全部落盘进池, 三粒全漂时由外层择优(离基频最近者)放行。
        okf, f0m = _voice_ok(fn)
        if not okf:
            # 1013pure R2(从头定规则, 见 凌雪声音纯净规则.md): 单粒放行, **不换seed重抽**。
            # 旧码漂移就换 seed 重合成(最多3粒) —— 每换一次 seed = 换一次音色归属
            # (实测同句不同seed F0 263/238/282/266/207Hz = 逐句换人), 才是"串音"制造机。
            # 偏差交给 R3 pitch-lock(rubberband formant=preserved) 锁回 Coach 基频。
            print(f"[voice-check] R2单粒放行(不重抽): voice={_vn} f0={f0m:.0f} expect={_vp.get('f0')}")
        # 1015终局(v35+v37 campplus实证): 裸链39/39+旁路链26/26全对, 锁/锚=串音真凶。
        # 默认旁路; SPEAKTOSI_PITCH_LOCK=1 / SPEAKTOSI_ANCHOR=1 恢复旧行为(与引擎同源开关)。
        if os.environ.get('SPEAKTOSI_PITCH_LOCK', '0') != '0':
            _pitch_lock(fn, f0m)   # 1013lock: 逐句锁回 Coach 基频
        # 1013pure R3b: 声道包络锚定 —— 把"声道尺寸"对齐 Coach(只改亮度/厚度, 保时长/响度)
        try:
            if _VA is not None and os.environ.get('SPEAKTOSI_ANCHOR', '0') != '0':
                _ai = _VA.anchor_file(str(fn), voice=_vn)
                print(f"[anchor-R3b] {_ai.get('before')}dB -> {_ai.get('after')}dB voice={_vn}")
        except Exception as _ae:
            print("[anchor-R3b] fail(放行原音):", type(_ae).__name__, str(_ae)[:80])
        _apply_rate()
        return

    for attempt in range(1):   # 1013pure R2: 单粒定音, 不重抽
        try:
            await asyncio.to_thread(_do, attempt)
            return f"/audio/{fn.name}"
        except Exception as e:
            if attempt == 2:
                if _drift_pool:
                    # 1013pm 择优放行: 三粒全漂取离基频最近者(而非旧"末次放行"最差粒), 其余候选删除
                    dev, best = min(_drift_pool, key=lambda x: x[0])
                    fn.write_bytes(best.read_bytes())
                    for _d, _c in _drift_pool:
                        try: _c.unlink()
                        except Exception: pass
                    print(f"[voice-verify] 3次均漂移, 择优放行(dev={dev:.3f}): voice={_vn} expect={_vp.get('f0')}")
                    await asyncio.to_thread(_pitch_lock, fn, None)   # 1013lock: 择优后同样锁回 Coach 基频
                    await asyncio.to_thread(_apply_rate)
                    return f"/audio/{fn.name}"
                print("audiocpp TTS失败(回退edge):", type(e).__name__, str(e)[:100])
            await asyncio.sleep(0.3)
    return None

@app.get("/audio/{name}")
def audio(name):
    p = AUDIO_DIR / name
    if not p.exists() or "/" in name or "\\" in name:
        raise HTTPException(404)
    mt = "audio/wav" if name.lower().endswith(".wav") else "audio/mpeg"
    return FileResponse(p, media_type=mt)


@app.get("/eval/audio/{name}")
def eval_audio_get(name: str):
    """1019: 会话记录「回放我说的这句话」—— 取学生原始 WAV(语音轮留存, 与评测同源)。"""
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(404)
    try:
        import evaluation
        p = Path(evaluation.AUDIO_DIR) / name
    except Exception:
        raise HTTPException(404)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav")

# ---------- 流式对话（SSE）：LLM逐token生成 → 句子级切分 → TTS并行合成 → 边生成边播 ----------
HARD_END = "。！？!?…"   # 硬句末：遇到必切
# 英文句点误切白名单：称谓/缩写后的 "." 不算句末（如 "Dr." "e.g." "student.\n" 属正常句末）
def _is_abbrev(buf, dot_pos):
    m = re.search(r"([A-Za-z]+)\.$", buf[:dot_pos + 1])
    return bool(m) and m.group(1).lower() in {"dr","mr","mrs","ms","st","jr","sr","vs","etc","e.g","i.e","prof","inc","ltd"}

def _sent_split(buf, emitted):
    """流式切句：够长(首段6字/后续14字)或遇硬句末才切；否则继续累积等更多token。
    返回 (切出的句段列表, 剩余buf, 是否已出声过)。绝不死锁：无硬句末且不够长就原样等。"""
    out = []
    pos = 0
    while True:
        m = re.search(r"[。！？!?；;…，,、：:]|(?<=[a-zA-Z])\.(?=\s|$)", buf[pos:])
        if not m:
            break
        end = pos + m.end()
        ch = buf[end - 1]
        if ch == "." and _is_abbrev(buf, end - 1):
            pos = end
            continue
        seg = buf[:end]
        first = not (emitted or out)
        # 最小长度按语言：中文6/14字；纯英文按字符≈20/40（词长差异）
        zh = any('\u4e00' <= c2 <= '\u9fff' for c2 in seg)
        need = (6 if first else 14) if zh else (20 if first else 40)
        has_hard = any(ch in HARD_END for ch in buf[pos:end])
        if len(seg.strip()) < need and not has_hard:
            pos = end          # 太碎→延伸到下一个标点再判，不在此切
            continue
        s = re.sub(r"\s+", " ", seg).strip()   # 换行/多空格压单空格（气泡美观+TTS自然）
        s = s.lstrip("，,、：:；; ")            # 段首残留标点清理（引号内切分副产物）
        if s:
            out.append(s)
        buf = buf[end:]
        pos = 0
    return out, buf, (emitted or bool(out))

# 1007r4: 流式回复里同时并发的 TTS 合成上限(本地克隆服务单卡, 3 路足够
# 把 LLM 生成时间盖住, 再多会互相排队且吃显存)
TTS_STREAM_CONCURRENCY = 3
@app.post("/chat_stream")
async def chat_stream(body: dict):
    text = (body.get("text") or "").strip()
    img = _clean_image(body.get("image")) if body.get("image") else None   # 1012视觉老师
    if not text and not img:
        raise HTTPException(400, "empty text")
    persona = load_persona_safe()
    c = cfg()
    if img and not _vision_enabled(c):
        img = None
    eye_mode = False
    if not img:                                    # 1013挂眼: 免快门提问自动附最新帧
        img = _eye_frame(c)
        eye_mode = bool(img)
    _mem_push("user", (text + ((" [老师正通过摄像头看着]" if eye_mode else " [学生发来一张照片]") if img else "")) or "[学生发来一张照片]")
    msgs = [{"role": "system", "content": _sys_prompt(persona) + ((_EYE_NOTE if eye_mode else _VISION_NOTE) if img else "")}] \
        + _mem_window(c) + [_user_msg(text, img)]

    async def gen():
        client = _vision_client(c, async_=True) if img else _llm_client(c, async_=True)
        # ---- 1007r4 真优化: 生产者(LLM流) / 消费者(TTS+yield) 解耦 ----
        # 旧实现把 `await synth(s,c)` 直接写在 async-for 循环体里 -> 合成期间不再读 LLM
        # socket, LLM 生成与 TTS 合成完全串行(实测每句 TTS 0.3~2.7s 全加在关键路径上)。
        # 新实现: 句子一切出就立刻后台起 synth task, 生产者继续吐 token; 消费者按序收割。
        #   * 播放顺序不变(消费者严格按入队序 await)
        #   * 信号量限并发, 防一次把本地 TTS 打爆
        #   * 客户端断开(GeneratorExit)时取消生产者与未完成 task, 不留僵尸合成
        q = asyncio.Queue()
        sem = asyncio.Semaphore(TTS_STREAM_CONCURRENCY)
        st = {"full": "", "err": None}

        async def _synth_guarded(s):
            async with sem:
                return await synth(s, c)

        async def _producer():
            buf = ""
            first_done = False
            full = ""
            try:
                stream = await client.chat.completions.create(
                    model=(c.get("vision_model") or c["llm_model"]) if img else c["llm_model"],
                    messages=msgs, temperature=0.55,
                    max_tokens=700, stream=True,
                    extra_body=_llm_extra(c))
                async for ch in stream:
                    d = ch.choices[0].delta if ch.choices else None
                    if not d:
                        continue
                    piece = (getattr(d, "content", None) or "")
                    if not piece:
                        continue
                    full += piece
                    buf += piece
                    sents, buf, first_done = _sent_split(buf, first_done)
                    for s in sents:
                        await q.put((s, asyncio.ensure_future(_synth_guarded(s))))
                # 尾句
                if buf.strip():
                    s = buf.strip()
                    await q.put((s, asyncio.ensure_future(_synth_guarded(s))))
            except Exception as e:
                st["err"] = "%s: %s" % (type(e).__name__, str(e)[:150])
            st["full"] = full.strip()   # buf 已并入 full, 不可重复拼接
            await q.put(None)           # 结束哨兵

        prod = asyncio.ensure_future(_producer())
        pending = []
        sent_idx = 0
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                s, task = item
                pending.append(task)
                try:
                    turl = await task
                except Exception:
                    turl = None
                finally:
                    if task in pending:
                        pending.remove(task)
                yield "data: " + json.dumps({"type": "sent", "i": sent_idx, "text": s, "tts": turl}, ensure_ascii=False) + "\n\n"
                sent_idx += 1
            full = st["full"]
            if st["err"]:
                yield "data: " + json.dumps({"type": "err", "error": st["err"]}, ensure_ascii=False) + "\n\n"
            if not full and not st["err"]:
                yield "data: " + json.dumps({"type": "err", "error": "空回复（大脑未就绪？）"}, ensure_ascii=False) + "\n\n"
            elif full:
                _mem_push("assistant", full)   # 1003防一句话复发v2, 与engine同口径
            yield "data: " + json.dumps({"type": "done", "full": full}, ensure_ascii=False) + "\n\n"
        finally:
            # 客户端断开/异常: 收尾生产者与在飞 TTS, 不留僵尸任务
            if not prod.done():
                prod.cancel()
            for t in pending:
                if not t.done():
                    t.cancel()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# STT：GPU加速(实测0.24s vs CPU 13s) + 启动预热(含CUDA首推预热)，失败回退CPU
_whisper = None
_whisper_dev = "cpu"
_whisper_lock = threading.Lock()   # 1006: 两路预热(模块线程+启动例程)并发CUDA初始化会死锁在encode

def _stt_dll_dirs():
    """1006修: Electron拉起时PATH极简, faster-whisper CUDA找不到cublas64_12/cudnn64_9
    → GPU初始化抛RuntimeError落回CPU(13s/次,前端超时='识别不到声音')。
    显式登记两处现成DLL目录: CUDA工具集bin(cublas) + engine venv torch/lib(完整cudnn9)。"""
    import glob as _g
    cands = _g.glob(r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*\bin")
    cands.append(r"C:\Speaktosi\engine\venv\Lib\site-packages\torch\lib")
    ok = [d for d in cands if os.path.isdir(d)]
    for d in ok:
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass
    # ctranslate2 用传统 LoadLibrary 只搜 PATH，add_dll_directory 不够，必须注入 PATH
    os.environ["PATH"] = ";".join(ok) + ";" + os.environ.get("PATH", "")


def _load_whisper():
    global _whisper, _whisper_dev
    with _whisper_lock:
        if _whisper is not None:
            return
        _stt_dll_dirs()
        from faster_whisper import WhisperModel
        import numpy as np
        model_name = cfg()["stt_model"]
        try:
            m = WhisperModel(model_name, device="cuda", compute_type="float16")
            # CUDA 首推预热（首次8.7s→稳态0.24s，必须启动时就热好）
            m.transcribe(np.zeros(16000, dtype="float32"), language="zh", beam_size=1, vad_filter=False)
            _whisper, _whisper_dev = m, "cuda"
            print("STT 预热完成 (GPU/cuda float16)")
        except Exception as e:
            print("GPU STT 不可用，回退 CPU:", str(e)[:100])
            _whisper = WhisperModel(model_name, device="cpu", compute_type="int8")
            _whisper_dev = "cpu"
            print("STT 预热完成 (CPU int8)")

def _warm_stt():
    try:
        if _whisper is None:
            _load_whisper()
    except Exception as e:
        print("STT 预热失败:", e)
threading.Thread(target=_warm_stt, daemon=True).start()

@app.post("/stt")
def stt(file: UploadFile = File(...)):   # 1006: 必须同步def(threadpool跑)。
    # async def 时 whisper 重计算直接跑在事件循环上，一次慢识别=全后端假死(设置页全转圈根因)
    global _whisper
    try:
        import faster_whisper  # noqa
    except Exception:
        raise HTTPException(503, "faster-whisper 未安装")
    if _whisper is None:
        _load_whisper()
    raw = file.file.read()
    try:
        import evaluation
        evaluation.stash_audio(raw)      # 评测要求三份数据: 学生原始音频留存
    except Exception:
        pass
    with wave.open(io.BytesIO(raw), "rb") as w:
        rate = w.getframerate()
        n = w.getnframes()
        import numpy as np
        pcm = np.frombuffer(w.readframes(n), dtype=np.int16).astype("float32") / 32768.0
    # 0930修: faster-whisper强制假设16kHz,48k麦克风(Chrome默认)不重采样会被当3倍慢放→VAD全滤→识别空
    if rate != 16000:
        idx = np.linspace(0, len(pcm) - 1, int(round(len(pcm) * 16000.0 / rate)))
        pcm = np.interp(idx, np.arange(len(pcm)), pcm).astype("float32")
        rate = 16000
    # 定场语言：先探2s判中/英，再全文识别（language=None自动检测对短句不稳）
    lang = None
    try:
        _, info = _whisper.transcribe(pcm[: rate * 2], beam_size=1, vad_filter=False)
        lang = info.language if info.language in ("zh", "en") else None
    except Exception:
        pass
    segs, _ = _whisper.transcribe(pcm, language=lang, beam_size=1, vad_filter=True)
    text = "".join(s.text for s in segs).strip()
    return {"ok": True, "text": text}

@app.get("/landmarks")
def get_landmarks():
    if LM_PATH.exists():
        return JSONResponse(json.loads(LM_PATH.read_text(encoding="utf-8")))
    return JSONResponse({})

@app.post("/landmarks")
async def set_landmarks(body: dict):
    LM_PATH.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

@app.get("/calibflag")
def calibflag():
    return {"on": CALIB_PATH.exists()}

@app.post("/calibflag")
async def calibflag_set(body: dict):
    if body.get("on"):
        CALIB_PATH.write_text("1", encoding="utf-8")
    elif CALIB_PATH.exists():
        CALIB_PATH.unlink()
    return {"ok": True}

@app.get("/config")
def get_config():
    return JSONResponse(cfg())

@app.post("/config")
async def set_config(body: dict):
    c = {**cfg(), **{k: v for k, v in body.items() if k in DEFAULT_CFG}}
    # 1003(卡通形象): 换形象→音色preset自动跟随注册表(前端只需发avatar_id)
    if "avatar_id" in body:
        old_av = cfg().get("avatar_id", "")
        for a in AVATARS:
            if a["avatar_id"] == c.get("avatar_id"):
                c["avatar_voice"] = a["voice_preset"]
                break
        # 1004(四老师): 形象=灵魂 强绑定——avatar_id 真变化时同步重建 persona.json。
        # 只在"变了"时重建: 设置页每次保存都带 avatar_id, 盲目重建会冲掉手改灵魂(教训)。
        # 默认老师→恢复Brenda备份; 四卡通老师→Shared-core+本人MD 原子重建。失败不挡配置保存。
        new_av = c.get("avatar_id", "")
        if new_av and new_av != old_av:
            c["teacher_id"] = new_av
            # 1005音色自动对齐: 换老师→克隆音色强制跟随(在body合并之后设, 前端忘发/发旧值都对齐);
            # 未换老师时尊重设置页手动试听选择(body里的audiocpp_tts_voice保留)。
            for a in AVATARS:
                if a["avatar_id"] == new_av:
                    c["audiocpp_tts_voice"] = a["voice_preset"]
                    break
            try:
                okp, _ = apply_teacher(new_av)
                if okp:
                    n = len(memory)
                    memory.clear()   # 换魂=灵魂级事件: 清全局记忆防旧人设回复喂新模型
                    print(f"[teacher] persona rebuilt for {new_av} (cleared {n})")
                else:
                    print("[teacher] no persona source for", new_av, "- keep current")
            except Exception as e:
                print("[teacher] rebuild fail:", type(e).__name__, str(e)[:120])
        elif new_av:
            c["teacher_id"] = new_av
    CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}

# ---------- 1003 卡通形象: 注册表 + 清单/封面端点 ----------
# 新增形象: 生成素材包(engine/data/avatars/<id>)后在此登记一行即可, 重启后端生效。
# 1004(四老师): persona_md=灵魂源文件(avatar/persona/), emoji/greeting 供前端展示与开场播报;
#               默认老师无 persona_md(Brenda 灵魂在 config/personas/brenda.json)。
AVATARS = [
    # 1020mini: 本档只保留 Arthur / Mia 两位老师 —— 其余 11 条的注册项已裁掉,
    # 对应的形象包/待机序列/人设/音色同样不在包里(见 tools/mini_edition.py)。
    {"avatar_id": "coach_arthur",  "name": "Coach Arthur",  "voice_preset": "arthur",
     "persona_md": "CoachArthur.md", "emoji": "📚",
     "greeting": "Hello, I'm Arthur. Bring me a big question, and let's explore it together.",
     "engine": "http://127.0.0.1:8011"},
    {"avatar_id": "coach_mia",     "name": "Coach Mia",     "voice_preset": "mia",
     "persona_md": "CoachMia.md", "emoji": "🎨",
     "greeting": "Hey, I'm Mia! Tell me something about your day—let's make it sparkle.",
     "engine": "http://127.0.0.1:8011"},
]
@app.get("/avatars")
def get_avatars():
    """形象清单: 素材包缺文件的自动隐藏, 防学生选到坏形象; voice_preset=绑定音色"""
    av_root = Path(r"C:\Speaktosi\engine\data\avatars")
    out_list = []
    for a in AVATARS:
        d = av_root / a["avatar_id"]
        if all([(d / "latents.pt").exists(), (d / "coords.pkl").exists(),
                (d / "mask_coords.pkl").exists(), (d / "full_imgs").exists()]):
            e = dict(a); e["cover"] = "/avatar_cover/" + a["avatar_id"]
            out_list.append(e)
    return JSONResponse({"avatars": out_list})

@app.get("/voices")
def get_voices():
    """1005音色清单(设置页动态下拉): 四老师克隆音色+Demo Voice。
    真源=AVATARS注册表(voice_preset) ∩ engine tts_runtime.json 已注册presets, 不做第三份复制。
    此前病根: 设置页音色下拉是静态edge音色, 四老师克隆音色压根不在里面 → 两套体系混一块。"""
    c = cfg()
    try:
        rt = json.loads(Path(r"C:\Speaktosi\engine\data\tts_runtime.json").read_text(encoding="utf-8"))
        presets = (rt.get("models") or [{}])[0].get("voice_presets") or {}
    except Exception:
        presets = {}
    voices = []
    for a in AVATARS:   # 老师音色在前, 按注册表顺序
        vp = a["voice_preset"]
        if vp == "default" or (presets and vp not in presets):
            continue
        voices.append({"id": vp, "name": a["name"] + " · 本人克隆音色",
                       "emoji": a.get("emoji", ""), "avatar_id": a["avatar_id"], "teacher": True})
    # 1020mini: 原"default 演示音色"兜底项已移除 —— 本档音色只有 Arthur / Mia 两套克隆音。
    return JSONResponse({"current": _eff_voice(c), "avatar_voice": c.get("avatar_voice", ""),
                         "audiocpp_tts_voice": c.get("audiocpp_tts_voice", "default"), "voices": voices})

@app.get("/teachers")
def get_teachers():
    """1004四老师总览: 当前老师 + 四位老师人设摘要(name/title/emoji/greeting/voice/avatar/素材是否就绪)。
    greeting 源=MD字段头(唯一真源), 此处实时解析, 不做第二份复制。"""
    cur = load_persona_safe()
    out = []
    av_root = Path(r"C:\Speaktosi\engine\data\avatars")
    for a in AVATARS:
        if not a["persona_md"]:
            continue
        d = av_root / a["avatar_id"]
        ready = all([(d / "latents.pt").exists(), (d / "coords.pkl").exists(),
                     (d / "mask_coords.pkl").exists(), (d / "full_imgs").exists()])
        e = dict(a)
        try:
            f, _ = parse_teacher_md(PERSONA_MD_DIR / a["persona_md"])
            e["display_name"] = f.get("name", a["name"])
            e["title"] = f.get("title", "")
            e["emoji"] = f.get("emoji", e.get("emoji", ""))
            e["greeting"] = f.get("greeting", e.get("greeting", ""))
        except Exception as ex:
            e["parse_error"] = str(ex)[:80]
        e["assets_ready"] = ready
        out.append(e)
    return JSONResponse({"current": cur.get("teacher_id") or cur.get("avatar_id") or "",
                         "current_name": cur.get("name", ""),
                         "reload_ts": _TEACHER_RELOAD_TS["t"],
                         "teachers": out})

@app.post("/teacher/apply")
async def teacher_apply(body: dict):
    """一键切老师(重建设魂): 写 config.avatar_id/teacher_id/avatar_voice + 原子重建 persona.json + 清全局记忆。
    引擎(llm.py)按 mtime 热重载 → 语音链零重启换魂; overlay 轮询 /config 自动重连换脸换声。"""
    av = (body or {}).get("avatar_id", "")
    if not av:
        return {"ok": False, "error": "need avatar_id"}
    c = cfg()
    changed = c.get("avatar_id") != av
    c["avatar_id"] = av
    c["teacher_id"] = av
    for a in AVATARS:
        if a["avatar_id"] == av:
            c["avatar_voice"] = a["voice_preset"]
            c["audiocpp_tts_voice"] = a["voice_preset"]   # 1005: 一键切老师=音色强制对齐
            break
    okp, p = (False, None)
    try:
        okp, p = apply_teacher(av)
    except Exception as e:
        print("[teacher/apply] rebuild fail:", type(e).__name__, str(e)[:120])
    if not okp:
        return {"ok": False, "error": f"no persona source for {av}"}
    CFG_PATH.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding="utf-8")
    n = len(memory)
    if changed:
        memory.clear()
    return {"ok": True, "avatar_id": av, "name": (p or {}).get("name", ""),
            "greeting": (p or {}).get("greeting", ""), "cleared_memory": n}

@app.get("/avatar_cover/{avatar_id}")
def avatar_cover(avatar_id: str):
    """形象封面: cover.png(棋盘合成图, 1004绿幕后新增)优先, 回退素材包首帧(仅白名单id, 防目录穿越)"""
    if avatar_id not in {a["avatar_id"] for a in AVATARS}:
        raise HTTPException(404, "unknown avatar")
    d = Path(r"C:\Speaktosi\engine\data\avatars") / avatar_id
    p = d / "cover.png"
    if not p.exists():
        p = d / "full_imgs" / "00000000.png"
    if not p.exists():
        raise HTTPException(404, "no cover")
    return FileResponse(p, media_type="image/png")

@app.get("/tts")
async def tts_get(text: str = "", voice: str = ""):
    if not text.strip():
        raise HTTPException(400, "empty")
    c = cfg()
    if voice:   # 1005: 试听指定音色(不落盘)——下拉选谁听谁, 保存仍以config为准
        c = {**c, "audiocpp_tts_voice": voice}
    url = await synth(text.strip(), c)
    if not url:
        raise HTTPException(500, "tts failed")
    return {"ok": True, "url": url}

@app.post("/review")
async def review():
    """Session Review Mode：会话结束后对本次真实对话做英语能力体检。
    用 persona.json 的 session_review_mode 作为 system，memory 作为证据。"""
    persona = load_persona_safe()
    review_sys = persona.get("session_review_mode") or persona.get("system", "")
    c = cfg()
    # 本次会话的用户/助手轮次 = 评估证据（字节预算窗口, 与聊天同源）
    turns = _mem_window(c)
    user_msgs = [m for m in turns if m.get("role") == "user"]
    if len(user_msgs) < 1:
        return {"ok": False, "error": "本次会话还没有你的英语输出，先聊几句再来体检"}
    msgs = [{"role": "system", "content": review_sys}] + turns + [
        {"role": "user", "content": "Please run the full Session Review on our conversation now, following the report format (A-H)."}]
    client = _llm_client(c)
    try:
        r = client.chat.completions.create(model=c["llm_model"], messages=msgs,
                                           temperature=0.5, max_tokens=2200,
                                           extra_body=_llm_extra(c))
        report = (getattr(r.choices[0].message, "content", "") or "").strip()
        if not report:
            return {"ok": False, "error": "模型没有输出报告"}
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}



# ---------- 会话记录 + 实用词汇高亮（桌面老师 overlay 用） ----------
HISTORY_PATH = Path(__file__).parent / "config" / "history.json"

def _history_load():
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []

def _history_save(items):
    try:
        HISTORY_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        print("[history] save fail:", type(e).__name__, str(e)[:80])

def _extract_vocab_bg(entry_id, text):
    """后台线程: LLM 提取实用英语词汇/短语, 写回该条目。失败静默(不影响记录)。"""
    def _go():
        try:
            c = cfg()
            client = _llm_client(c)
            r = client.chat.completions.create(
                model=c["llm_model"],
                messages=[
                    {"role": "system", "content":
                        "You are a vocabulary extractor for an English learner. "
                        "From the teacher's reply, pick the PRACTICAL English words/phrases worth learning "
                        "(idioms, phrasal verbs, collocations, useful expressions — NOT trivial words like 'the','is'). "
                        "Return ONLY a JSON array of strings (the exact substrings as they appear in the text), max 8 items. "
                        "If nothing noteworthy, return []."},
                    {"role": "user", "content": text[:2000]},
                ],
                temperature=0.2, max_tokens=300, extra_body=_llm_extra(c))
            raw = (r.choices[0].message.content or "").strip()
            m = re.search(r"\[.*\]", raw, re.S)
            vocab = json.loads(m.group(0)) if m else []
            if not isinstance(vocab, list): vocab = []
            vocab = [str(v).strip() for v in vocab if str(v).strip()][:8]
            items = _history_load()
            for it in items:
                if it.get("id") == entry_id:
                    it["vocab"] = vocab
                    break
            _history_save(items)
        except Exception as e:
            print("[vocab] extract fail:", type(e).__name__, str(e)[:100])
    threading.Thread(target=_go, daemon=True).start()

@app.post("/history")
async def add_history(body: dict):
    role = body.get("role")
    text = (body.get("text") or "").strip()
    sid = body.get("sid")
    via = body.get("via") or "text"
    audio = body.get("audio")
    if role not in ("user", "assistant") or not text:
        raise HTTPException(400, "need role(user|assistant) + text")
    items = _history_load()
    entry = {"id": int(time.time() * 1000) + len(items) % 1000,
             "role": role, "text": text[:4000],
             "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
    if role == "assistant":
        entry["vocab"] = []
    items.append(entry)
    if len(items) > 500:
        items = items[-500:]
    _history_save(items)
    if role == "assistant":
        _extract_vocab_bg(entry["id"], text)
    try:
        import evaluation
        evaluation.add_turn(sid, role, text, via=via, audio_ref=audio)  # 会话评测: 归组到当前活动会话
    except Exception:
        pass
    return {"ok": True, "id": entry["id"], "sid": sid}

# ── 会话评测 Session Evaluation ──────────────────────────────
@app.post("/session/start")
def session_start():
    """老师窗口连接就绪时调用, 开一个新会话(记录起止时间; 自动闭合上次残留会话)。"""
    try:
        import evaluation
        return {"ok": True, "session_id": evaluation.start_session()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

@app.post("/session/end")
async def session_end(body: dict):
    """关闭窗口时调用。≥5分钟且有料 → 后台线程生成评测报告。"""
    try:
        import evaluation
        sid = body.get("session_id") or None
        ok, reason = evaluation.end_session(sid)   # 达标则内部自起detached子进程评测
        return {"ok": True, "evaluating": ok, "reason": reason}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}

@app.get("/session/active")
def session_active():
    """查当前活动会话(最新一个未 ended 的)。"""
    try:
        import evaluation
        d = evaluation._load()
        sid = evaluation._gactive(d)
        return {"ok": True, "session_id": sid}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

@app.get("/eval/sessions")
def eval_sessions():
    """评测历史列表(所有会话概览)。"""
    try:
        import evaluation
        return {"ok": True, "items": evaluation.list_sessions()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}

@app.get("/eval/sessions/{sid}")
def eval_session_detail(sid: str):
    try:
        import evaluation
        s = evaluation.get_session(sid)
        if not s:
            raise HTTPException(404, "会话不存在 / session not found")
        out = dict(s)
        out.pop("eval_md", None)   # 报告正文走 /eval/{sid}/md, 列表接口不拖大字符串
        return {"ok": True, "session": out}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}

@app.get("/eval/sessions/{sid}/md")
def eval_session_md(sid: str):
    """导出/查看单次会话评测(MD原文)。"""
    try:
        import evaluation
        md = evaluation.export_md(sid)
        if not md:
            raise HTTPException(404, "尚无评测报告 / no report yet")
        return Response(md, media_type="text/markdown; charset=utf-8",
                        headers={"Content-Disposition": 'inline; filename="%s.md"' % sid})
    except HTTPException:
        raise

@app.post("/eval/sessions/{sid}/rerun")
def eval_session_rerun(sid: str):
    """评测失败/想重跑时手动再评一次(已有转写即可)。"""
    try:
        import evaluation
        if not evaluation.get_session(sid):
            raise HTTPException(404, "会话不存在")
        ok, reason = evaluation.end_session(sid)   # 未结束先结束; 已结束达标则重跑
        return {"ok": True, "evaluating": ok, "reason": reason}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}

@app.post("/eval/sessions/{sid}/read")
def eval_session_read(sid: str):
    try:
        import evaluation
        return {"ok": bool(evaluation.mark_read(sid))}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

@app.delete("/eval/sessions/{sid}")
def eval_session_delete(sid: str):
    try:
        import evaluation
        return {"ok": bool(evaluation.delete_session(sid))}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}

@app.delete("/eval/sessions")
def eval_sessions_clear():
    try:
        import evaluation
        evaluation.clear_all()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}

@app.post("/eval/audio")
async def eval_audio(file: UploadFile = File(...), sid: str = ""):
    """学生原始音频留存: 语音输入时前端把同一段WAV发来(与/stt同一数据)。"""
    try:
        import evaluation
        raw = await file.read()
        if len(raw) < 1000:
            return {"ok": False, "error": "empty"}
        return {"ok": True, "audio": evaluation.stash_audio(raw, sid)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

@app.get("/history")
def get_history(limit: int = 100):
    items = _history_load()
    return {"ok": True, "items": items[-max(1, min(limit, 500)):]}

@app.delete("/history")
def clear_history():
    _history_save([])
    return {"ok": True}


# ---------- 1019 会话记录翻译（学生复习：一键英↔中互译） ----------
# 走与对话同款云端 LLM（token-plan / qwen3.8-flash + 关思考），超时40s由 _llm_client 兜底；
# 进程内 LRU 缓存，同句重复点译不再打云端；失败只返回 ok=False，绝不影响通话链路。
_TR_CACHE = {}
_TR_LOCK = threading.Lock()
_TR_MAX = 800
_TR_SYS = {
    "zh": ("You are a professional translator. Translate the user's text into natural, idiomatic "
           "Simplified Chinese. Keep the original tone, keep proper nouns/technical terms in English "
           "when a Chinese reader would expect that. Output ONLY the translation: no quotes, no notes."),
    "en": ("You are a professional translator. Translate the user's text into natural, idiomatic English "
           "at a learner-friendly level. Keep the original tone. Output ONLY the translation: "
           "no quotes, no notes."),
}


@app.post("/translate")
async def translate(body: dict):
    txt = (body.get("text") or "").strip()
    tgt = (body.get("target") or "zh").strip().lower()
    if not txt:
        return {"ok": False, "error": "empty text"}
    if tgt not in _TR_SYS:
        tgt = "zh"
    if len(txt) > 4000:
        txt = txt[:4000]
    key = tgt + "\x00" + txt
    with _TR_LOCK:
        hit = _TR_CACHE.get(key)
    if hit:
        return {"ok": True, "text": hit, "target": tgt, "cached": True}
    try:
        c = cfg()
        client = _llm_client(c)
        r = await asyncio.to_thread(
            client.chat.completions.create,
            model=c["llm_model"],
            messages=[{"role": "system", "content": _TR_SYS[tgt]},
                      {"role": "user", "content": txt}],
            temperature=0.2,
            extra_body=_llm_extra(c),
        )
        out = (r.choices[0].message.content or "").strip()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    if not out:
        return {"ok": False, "error": "empty translation"}
    with _TR_LOCK:
        if len(_TR_CACHE) >= _TR_MAX:
            _TR_CACHE.clear()          # 够用即清（无需真 LRU：译文可重取）
        _TR_CACHE[key] = out
    return {"ok": True, "text": out, "target": tgt}

@app.get("/history/export")
def history_export(fmt: str = "md"):
    """学生复习导出: md=Markdown 笔记(Q&A配对+生词表), json=原始记录。"""
    items = _history_load()
    try:
        tname = (load_persona_safe().get("name") or "SpeakTosi").strip()
    except Exception:
        tname = "SpeakTosi"
    if fmt == "json":
        return JSONResponse(items, headers={
            "Content-Disposition": 'attachment; filename="aiteacher-session-log.json"'})
    from datetime import datetime
    lines = [f"# {tname} 老师 · 会话复习笔记",
             f"", f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"- 消息条数：{len(items)}", ""]
    vocab = {}
    rounds = []
    i = 0
    while i < len(items):
        it = items[i]
        if it.get('role') == 'user':
            a = items[i+1] if i+1 < len(items) and items[i+1].get('role') == 'assistant' else None
            rounds.append((it, a))
            i += 2 if a else 1
        else:
            rounds.append((None, it))
            i += 1
    for n, (u, a) in enumerate(rounds, 1):
        ts = (u or a or {}).get('ts', '')
        lines.append(f"## 第 {n} 轮  `{ts}`")
        if u: lines.append(f"\n**我**：{u.get('text','').strip()}")
        if a:
            lines.append(f"\n**老师**：{a.get('text','').strip()}")
            for w in (a.get('vocab') or []):
                vocab.setdefault(w, 0)
                vocab[w] += 1
        lines.append("")
    if vocab:
        lines.append("---\n\n## 📒 本会话实用表达（出现次数）\n")
        lines.append("| 表达 | 次数 |")
        lines.append("|---|---|")
        for w, c in sorted(vocab.items(), key=lambda x: -x[1]):
            lines.append(f"| {w} | {c} |")
    body = "\n".join(lines).encode('utf-8')
    return Response(body, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="aiteacher-review-notes.md"'})

@app.post("/reset")
def reset():
    memory.clear()
    return {"ok": True}

def _warmup_chain():
    """直播首问零冷启动：预建 LLM HTTPS 连接 + 预热克隆 TTS + 预热 Whisper GPU。"""
    import threading
    def _go():
        c = cfg()
        # 1) LLM 握手（no_think 短问）
        try:
            cl = _llm_client(c)
            cl.chat.completions.create(model=c["llm_model"],
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=4, extra_body=_llm_extra(c))
            print("[warmup] llm ok")
        except Exception as e:
            print("[warmup] llm:", type(e).__name__, str(e)[:80])
        # 2) 克隆 TTS 预热（首句 GPU kernel 编译）
        try:
            import asyncio
            asyncio.run(synth("Warm up.", c))
            print("[warmup] tts ok")
        except Exception as e:
            print("[warmup] tts:", type(e).__name__, str(e)[:80])
        # 3) Whisper 预热
        try:
            _load_whisper()
            print("[warmup] stt ok")
        except Exception as e:
            print("[warmup] stt:", type(e).__name__, str(e)[:80])
    threading.Thread(target=_go, daemon=True).start()

@app.on_event("startup")
def _on_startup():
    # 1004(四老师): 启动自愈——形象与灵魂不匹配时按MD重建(如历史遗留"Brenda灵魂+凌风形象")。
    # 守卫: 默认老师(aiteacher_demo)不动; persona.teacher_id已等于当前形象(含手改灵魂场景)不动。
    try:
        c = cfg()
        av = c.get("avatar_id", "aiteacher_demo")
        p = load_persona_safe()
        if av != "aiteacher_demo" and p.get("teacher_id") != av:
            okp, np_ = apply_teacher(av)
            print("[teacher] startup sync:", av, "->", (np_ or {}).get("name", "?") if okp else "FAIL keep current")
    except Exception as e:
        print("[teacher] startup sync err:", type(e).__name__, str(e)[:100])
    _warmup_chain()
    try:
        import evaluation
        evaluation.resume_orphans()     # 崩溃遗留的running评测重新拉起
    except Exception:
        pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
