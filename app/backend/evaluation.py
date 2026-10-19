# -*- coding: utf-8 -*-
"""SpeakTosi · 会话评测引擎 / Session evaluation engine.

会话 = 窗口开启到关闭的一段连续对话。门槛: >=5分钟 且 用户发言>=3句。
关闭窗口 => /session/end => **detached 子进程**跑 LLM 评测(不随 backend 被杀而亡),
生成中英双显 MD 报告, 落 sessions.json + eval_cache/reports/<sid>.md。
三份数据对应(Owen要求): 用户原始音频(eval_cache/audio) / ASR原文(turn.via=voice) /
教师最终说法(B/G段升级建议) => 发音维度不再受限于纯文本。

独立运行 / standalone:  python evaluation.py run <sid>
本模块不 import server(避免循环), 自备 cfg + key 解析(与 server 同规则)。
"""
import os, io, json, time, re, sys, threading, uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_DIR = os.path.join(BASE_DIR, "config")
SESSIONS_PATH = os.path.join(CFG_DIR, "sessions.json")
CONF_PATH = os.path.join(CFG_DIR, "app.json")   # 与 server.py CFG_PATH 同源
AUDIO_DIR = os.path.join(BASE_DIR, "eval_cache", "audio")
REPORT_DIR = os.path.join(BASE_DIR, "eval_cache", "reports")
MIN_SECONDS = 300          # 5 分钟
MIN_USER_TURNS = 3

_lock = threading.Lock()

def _ensure_dirs():
    os.makedirs(CFG_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(REPORT_DIR, exist_ok=True)

def _load():
    try:
        with io.open(SESSIONS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save(d):
    _ensure_dirs()
    tmp = SESSIONS_PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SESSIONS_PATH)

# ── 生命周期 / lifecycle ───────────────────────────────────────
def _new_locked(d):
    """调用方已持锁并载入 d; 建会话、落盘、返回sid。"""
    sid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d[sid] = {"session_id": sid, "started": time.time(),
              "started_str": time.strftime("%Y-%m-%d %H:%M:%S"),
              "ended": None, "turns": [], "eval_status": "open",
              "eval_md": None, "audio_refs": []}
    _save(d)
    return sid

def new_session():
    _ensure_dirs()
    with _lock:
        return _new_locked(_load())

def add_turn(sid, role, text, ts=None, via="text", audio_ref=None):
    if not (text or "").strip():
        return
    with _lock:
        d = _load()
        s = d.get(sid) if sid else None
        if not s or s.get("ended"):
            # 兜底归组: sid缺失/已结算/Electron忘记sessionStart(断线重连) -> 挂到活动会话, 没有就静默开一场
            sid = _gactive(d)
            if not sid:
                sid = _new_locked(d)
            s = d.get(sid)
        if not s or s.get("ended"):
            return
        turn = {"role": role, "text": text.strip()[:4000],
                "ts": ts or time.strftime("%H:%M:%S"), "via": via}
        if role == "user":
            ref = audio_ref or s.pop("pending_audio", None)   # /stt 预存的音频配对到本轮
            if ref:
                turn["audio"] = ref
                s.setdefault("audio_refs", []).append(ref)
        s["turns"].append(turn)
        _save(d)

def stash_audio(wav_bytes, sid=""):
    """存原始音频。sid 空则挂到当前活动会话的 pending(由 /stt 调用)。返回文件名。"""
    try:
        _ensure_dirs()
        fn = time.strftime("%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".wav"
        with open(os.path.join(AUDIO_DIR, fn), "wb") as f:
            f.write(wav_bytes)
        if not sid:
            s = _active()
            if s:
                with _lock:
                    d = _load(); sid = _gactive(d)
                    if sid:
                        d[sid]["pending_audio"] = fn; _save(d)
        return fn
    except Exception:
        return None

def _gactive(d):
    k = None; best = -1.0
    for sid, v in d.items():
        if not v.get("ended") and v.get("started", 0) > best:
            k, best = sid, v.get("started", 0)
    return k

def _active():
    d = _load(); k = _gactive(d)
    return d.get(k) if k else None

def end_session(sid, launch=True):
    with _lock:
        d = _load()
        if not sid:
            sid = _gactive(d)            # 关窗兜底: 未传sid则结束当前活动会话
        s = d.get(sid)
        if s and s.get("ended"):
            act = _gactive(d)
            if act and act != sid:       # 传进来的是过期sid(断线重连遗留) -> 改关真正的活动会话
                sid, s = act, d[act]
        if not s:
            ok, reason = False, "会话不存在 / not found"
        elif s.get("ended"):
            ok = s.get("duration", 0) >= MIN_SECONDS and s.get("user_turns", 0) >= MIN_USER_TURNS
            reason = "重新评测中 / re-evaluating" if ok else "会话不达标 / session below threshold"
            if ok:
                s["eval_status"] = "running"; s.pop("eval_error", None); _save(d)
        else:
            now = time.time(); dur = now - s["started"]
            s["ended"] = now; s["ended_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
            s["duration"] = dur
            s["user_turns"] = sum(1 for t in s["turns"] if t["role"] == "user")
            if dur < MIN_SECONDS:
                s["eval_status"] = "skipped_short"
                _save(d); ok, reason = False, "会话不足5分钟(实际%d秒) / under 5 min" % int(dur)
            elif s["user_turns"] < MIN_USER_TURNS:
                s["eval_status"] = "skipped_thin"
                _save(d); ok, reason = False, "发言不足3句(%d句) / too few turns" % s["user_turns"]
            else:
                s["eval_status"] = "running"; s.pop("eval_error", None)
                _save(d); ok, reason = True, "评测生成中 / evaluating"
    if launch and ok:
        _launch(sid)
    return ok, reason

def _launch(sid):
    """独立子进程跑评测(不随backend退出而死); spawn失败回退daemon线程。"""
    import subprocess
    try:
        flags = 0x08000000              # CREATE_NO_WINDOW (DETACHED会黑框, 1002黑框教训)
        subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "evaluation.py"), "run", sid],
                         cwd=BASE_DIR, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        threading.Thread(target=evaluate_safe, args=(sid,), daemon=True).start()
        return False

def start_session():
    """新开一个会话; 顺带把未闭合的旧会话自动结束(页面重载/重启场景, 达标同样触发评测)。"""
    _ensure_dirs()
    d = _load()
    old = [k for k, v in d.items() if not v.get("ended")]
    empty = [k for k in old if not d[k].get("turns")]   # 从未说过话的壳会话: 直接删, 不留垃圾
    sid = new_session()
    for k in old:
        if k in empty:
            delete_session(k)
        else:
            end_session(k, launch=True)
    return sid

def delete_session(sid):
    with _lock:
        d = _load(); s = d.pop(sid, None); _save(d)
    if s:
        _drop_files(s)
        return True
    return False

def _drop_files(s):
    for fn in s.get("audio_refs", []):
        try:
            os.remove(os.path.join(AUDIO_DIR, fn))
        except Exception:
            pass
    try:
        os.remove(os.path.join(REPORT_DIR, (s.get("session_id") or "") + ".md"))
    except Exception:
        pass

def clear_all():
    with _lock:
        d = _load()
        for s in d.values(): _drop_files(s)
        _save({})

def list_sessions():
    d = _load(); out = []
    for sid, s in d.items():
        dur = s.get("duration") or (time.time() - s["started"])
        out.append({"session_id": sid, "started_str": s.get("started_str"),
                    "ended_str": s.get("ended_str"),
                    "duration_min": round(dur / 60.0, 1),
                    "turns": len(s.get("turns", [])),
                    "user_turns": s.get("user_turns", sum(1 for t in s["turns"] if t["role"] == "user")),
                    "eval_status": s.get("eval_status"), "has_report": bool(s.get("eval_md")),
                    "read": bool(s.get("read")),
                    "eval_error": (s.get("eval_error") or "")[:80] if s.get("eval_status") == "error" else "",
                    "open": not s.get("ended")})
    out.sort(key=lambda x: x["started_str"] or "", reverse=True)
    return out

def get_session(sid):
    return _load().get(sid)

def mark_read(sid):
    with _lock:
        d = _load()
        if sid in d:
            d[sid]["read"] = True; _save(d); return True
        return False

# ── LLM 评测(自持, 与 server 同源规则) ─────────────────────────
# 与 server.py DEFAULT_CFG 同源兜底 + .env 回读(独立子进程不继承 backend 注入的环境变量)
DEFAULT_CFG = {
    "llm_base": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    "llm_model": "qwen3.8-flash",
    "llm_api_key": "",
    "llm_no_think": True,
}
_ENV_KEYS = ("DASHSCOPE_API_KEY", "ALIBABA_CODING_PLAN_API_KEY", "BAILIAN_API_KEY")
_env_tried = False

def _load_env():
    global _env_tried
    if _env_tried: return
    _env_tried = True
    if all(os.environ.get(k) for k in _ENV_KEYS): return
    cands = [os.path.expanduser("~/AppData/Local/hermes/.env"),
             os.path.expanduser("~/.hermes/.env"),
             os.path.join(BASE_DIR, ".env")]
    home = os.environ.get("HERMES_HOME")
    if home: cands.insert(0, os.path.join(home, ".env"))
    for fp in cands:
        try:
            if not os.path.isfile(fp): continue
            for line in io.open(fp, encoding="utf-8", errors="replace"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k in _ENV_KEYS and v:
                    os.environ.setdefault(k, v)
        except Exception:
            pass

def _cfg():
    c = {}
    try:
        with io.open(CONF_PATH, encoding="utf-8") as f: c = json.load(f)
    except Exception: pass
    return {**DEFAULT_CFG, **c}

def _api_key(c):
    k = (c.get("llm_api_key") or "").strip()
    if k: return k
    _load_env()
    base = c.get("llm_base", "")
    # 与 server.py 同规则: token-plan 网关只认 ALIBABA_CODING_PLAN key
    order = ("ALIBABA_CODING_PLAN_API_KEY", "BAILIAN_API_KEY", "DASHSCOPE_API_KEY") if "token-plan" in base \
            else ("DASHSCOPE_API_KEY", "ALIBABA_CODING_PLAN_API_KEY", "BAILIAN_API_KEY")
    for var in order:
        v = os.environ.get(var, "").strip()
        if v: return v
    return "none"

SYSTEM_PROMPT = """你是 SpeakTosi的会话评测器。对一位中国学生本次英语口语对话做一份「语言体检」报告。全文中英双显：每个要点中文在前、英文随后；引用句子保留英文原句。

评估总纲（优先级）：Accuracy 准确 → Naturalness 自然 → Fluency 流畅 → Precision 精确 → Depth 深度。核心永远是「能否自然表达自己」，不是「用了多少高级词」。

五维度：
1) 用语准确性 Word Choice & Usage：把用户表达归入四档并区别对待——technically correct（技术成立但生硬）/ grammatically correct but unnatural（语法对但母语者不这么说，重点纠正，给更自然替代）/ natural and idiomatic（地道，绝对不要为了显示水平去改它）/ unusually precise or sophisticated（罕见地精确高级，高亮表扬）。
2) 发音与ASR Pronunciation & Speech Recognition：从转录找出：想说X被听成Y的词、同一词反复识别错、因重音/元音/辅音/连读/语速导致识别难的词。必须明确区分「Possible pronunciation issue」与「ASR recognition error」；仅凭文字无法定论时不得武断，标注 "Possible pronunciation issue — worth checking."；证据不足写 "Insufficient evidence from transcription alone."。若某轮标注了[有原始音频]，说明可复核发音，提示用户该音频留存位置即可，不要臆测声学细节。
3) 语法 Grammar：只挑真正值得改的错误，优先级：明确语法错误 > 重复出现的错误 > 正确但显得不自然的结构 > 更高级自然的句型替代。每条：原句 → 修改后 → 简短原因 → 更自然的表达（如有必要）。不要把本来正确自然的句子改复杂。
4) 逻辑条理与流畅 Clarity, Coherence & Fluency：观点是否清楚、信息是否组织合理、连接词是否推进思路、有无重复绕圈或突然跳跃、停顿修正后能否重新组织、conversational flow 是否自然。重点判断：听起来像翻译中文，还是直接用英语思考。逻辑乱处要给出更自然的组织方式。
5) 语言深度 Idiomatic & Advanced Expression：全面扫描用户主动使用的 idioms / phrasal verbs / collocations / nuanced vocabulary / relative, noun, adverbial clauses / participial constructions / conditionals / rhetorical devices（contrast, parallelism, emphasis, metaphor, understatement 等）。凡本次真正用得好的必须【高亮】并说明：为什么好、比普通表达高在哪、什么场景可复用。本次没用到的类别，不要批评「不够高级」，而是从本次话题挑2-4个最适合加入的表达，给：普通表达→更自然/更有层次→为什么（必须源自本次对话主题，禁止随机罗列高级词）。

报告结构（严格用以下中英标题）：
SESSION ENDED — ENGLISH REVIEW｜会话结束 · 英语评测
（头部一行：日期时间、时长、轮次 / Date, duration, turns）
A. What You Did Well｜本次亮点：3-5条，必须引用用户本次原句，说清好在哪。
B. Word Choice & Naturalness｜用语与自然度：最值得改的表达，格式 Original → Better → Why。
C. Pronunciation / ASR Flags｜发音与识别疑点：Word / Phrase → ASR heard → Possible issue（pronunciation issue / ASR error 二择并标明）→ Suggested pronunciation focus；无充分证据时明确写 "Insufficient evidence from transcription alone."。
D. Grammar｜语法：只列真正值得注意的，格式 Original → Corrected → Explanation。
E. Fluency & Logic｜流畅与逻辑：整体观察 + 1-3个最影响表达质量的问题 + 每个给改进组织方式。
F. Language Depth｜语言深度：高亮用户真实用好的 idioms/collocations/从句/修辞；再列2-4个「下次可升级」表达（结合本次话题造句）。
G. One-Level-Up Version｜升一档：选用户本次一段真实表达，提升到更自然、成熟、精确的成年人英语（不是文学腔）；给出「基础正确 → 自然 → 精确有层次」三级阶梯对照。
H. Final Takeaway｜三条建议：只给3条最重要的，必须能直接用到下次对话，禁止泛泛而谈。

语气：Mary Alice 式——温柔、敏锐、略带洞察力；诚实但不打击；指出问题不贴标签；偶尔一句有画面感的旁白总结本次表现。
原则：不因复杂词多就判水平高；不因基础错误否定整体；优秀表达必须来自用户真实使用；建议服务于下一次真实交流；不为制造学习内容而人为增错或强塞高级表达。

报告末尾必须逐字加上（中英两行）：
> "You don't need more complicated English. You need more precise English."
> 你需要的不是更复杂的英语，而是更精确的英语。

必要时再补：
> "The goal is not to sound impressive. The goal is to sound like yourself — only clearer, sharper, and more natural."

直接输出 Markdown 正文，不要寒暄、不要代码围栏包裹。"""

def build_messages(sess):
    meta = ("会话 Session: %s ｜ 时长 Duration: %.1f min ｜ 用户发言 Student turns: %d\n"
            % (sess.get("started_str"), (sess.get("duration") or 0) / 60.0,
               sess.get("user_turns", sum(1 for t in sess.get("turns", []) if t["role"] == "user"))))
    lines = [meta]
    for i, t in enumerate(sess.get("turns", []), 1):
        who = "STUDENT 学生" if t["role"] == "user" else "TEACHER 老师"
        tag = ""
        if t["role"] == "user":
            if t.get("via") == "voice":
                tag = " [语音ASR转录 transcribed%s]" % ("，有原始音频 audio saved" if t.get("audio") else "")
            else:
                tag = " [文字 typed]"
        lines.append("[t%d] %s%s: %s" % (i, who, tag, t["text"]))
    transcript = "\n".join(lines)
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": transcript[:12000]}]

def evaluate(sid):
    """跑一次真评测并落盘。供 detached 子进程与 backend 兜底共用。"""
    sess = get_session(sid)
    if not sess:
        return False
    c = _cfg()
    from openai import OpenAI
    client = OpenAI(base_url=c["llm_base"], api_key=_api_key(c))
    extra = {"chat_template_kwargs": {"enable_thinking": False}} if c.get("llm_no_think", True) else {}
    r = client.chat.completions.create(model=c["llm_model"], messages=build_messages(sess),
                                       temperature=0.4, max_tokens=3500, extra_body=extra)
    msg = r.choices[0].message
    md = (getattr(msg, "content", None) or "").strip()
    if not md:
        rc = (getattr(msg, "reasoning_content", None) or "").strip()
        if rc: md = rc
    if not md:
        raise RuntimeError("empty LLM reply")
    md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", md.strip())
    with _lock:
        d = _load()
        if sid in d:
            d[sid]["eval_md"] = md; d[sid]["eval_status"] = "done"
            d[sid]["eval_str"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _save(d)
    try:
        with io.open(os.path.join(REPORT_DIR, sid + ".md"), "w", encoding="utf-8") as f:
            f.write(export_md(sid))
    except Exception: pass
    return True

def evaluate_safe(sid):
    try:
        evaluate(sid); return True
    except Exception as e:
        try:
            with _lock:
                d = _load()
                if sid in d:
                    d[sid]["eval_status"] = "error"
                    d[sid]["eval_error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
                    _save(d)
        except Exception: pass
        return False

def launch_detached(sid):
    """独立子进程跑评测: 不随 backend/Electron 退出而死。失败回退线程。"""
    import subprocess
    exe = sys.executable
    script = os.path.join(BASE_DIR, "evaluation.py")
    try:
        flags = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW
        p = subprocess.Popen([exe, script, "run", sid], cwd=BASE_DIR,
                             creationflags=flags, close_fds=True)
        return p.pid
    except Exception:
        threading.Thread(target=evaluate_safe, args=(sid,), daemon=True).start()
        return None

def resume_orphans(max_age=180):
    """backend启动时调用: 崩溃遗留的 running 会话(max_age秒前)重新拉起。"""
    d = _load()
    for sid, s in list(d.items()):
        if s.get("eval_status") == "running" and s.get("ended") and (time.time() - s["ended"]) > max_age:
            launch_detached(sid)

def export_md(sid):
    s = get_session(sid)
    if not (s and s.get("eval_md")):
        return None
    header = ("<!-- SpeakTosi · 会话评测报告 / Session Review\n"
              "  Session: %s\n  %s → %s ｜ 时长 Duration: %.1f min\n-->\n\n"
              % (sid, s.get("started_str"), s.get("ended_str"), (s.get("duration") or 0) / 60.0))
    return header + s["eval_md"]

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "run":
        _ensure_dirs()
        time.sleep(1)                 # 等父进程把 sessions.json 落盘
        evaluate_safe(sys.argv[2])
        sys.exit(0)
    print("usage: python evaluation.py run <session_id>")
