"""SpeakTosi 直播栈看门狗 — 自我修复机制
职责：拉起/守护 LiveTalking口型引擎(8011) + CosyVoice2多师TTS(18091)。
(1015retire: audiocpp克隆TTS 18080 已退役, 全部音色走 voice_registry.json)
策略：
  - 20s 巡检：端口不在 → 拉起（带启动宽限期，绝不误杀正在加载模型的服务）
  - 端口在但健康检查连败3次 → 杀进程重拉
  - 每5分钟对 TTS 做一次真实合成深检（30s超时）→ 卡死即重启
  - 崩溃风暴保护：10分钟内重启≥5次 → 退避60s再试并大声记日志
  - 单实例锁：绑 18999 失败即退出（防双看门狗打架）
用法：pythonw aiteacher_watchdog.py   （无窗口后台）
日志：D:\\Software\\LiveTalking\\watchdog.log （5MB轮转）
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))          # 包根/engine
PACK = os.path.dirname(BASE)                                # 整合包根
LEGACY = r'D:\AI-ModeLs\audiocpp-portable'                # 开发机旧位置(回退)
LOGF = os.path.join(BASE, 'watchdog.log')
LOCK_PORT = 18999
CYCLE = 20
# ---- 1007r4 冷启动风暴修复(三处真bug, watchdog.log 23:22~23:29 铁证) ----
# BUG1 惊群: 开机后3个GPU服务在3秒内同时抢显存(23:22:48/49/51) → 互相拖慢加载,
#      tts grace=90s 却在98s后才首次FAIL, musetalk 撑到363s仍被杀。
#      对策: 串行闸门 —— 同一时刻只允许一个服务处于加载期, 前一个健康了再放下一个。
BOOT_STAGGER = True
# BUG2 深探无宽限(致命): last_deep=0.0 让首轮必然深探, 且该分支完全没有grace豁免 →
#      tts#3 启动仅35秒就被深探杀掉(grace=90s), 形成"起→端口开→深探→还在加载→杀→再起"死循环。
#      对策: 深探只在过了grace之后做, 且必须连败 DEEP_FAILS_NEEDED 次才动手
#      (单次失败可能只是首次合成冷启/GC, 不该判死刑)。
# 1013: 2→3 —— 本机冷启动期单次深探失败很常见(首次合成冷启), 2次就杀会加剧死循环
DEEP_FAILS_NEEDED = 3
# BUG3 看门狗自身重启后 launched=0.0 → 对"正在加载中的既有服务"没有任何宽限保护,
#      3次FAIL(72s)就杀掉一个还需200s加载的musetalk。
#      对策: 启动时"收养"已在监听的端口 —— 用端口占用者的进程创建时间当加载起点。
ADOPT_RUNNING = True
# 串行闸门窗口：看门狗启动后该时长内，同一时刻只允许一个服务处于加载期。
# 取三服务grace总和上限(90+150+300)偏保守值：正常冷启动全部就绪≈8分钟。
BOOT_GATE_WINDOW = 540
WD_START = 0.0   # main() 里置为看门狗启动时刻
# ---- 1018 空闲自动释放 ----
# 托盘"退出界面"默认保留服务栈(热态秒开 8.2s), 但长闲置会占着 ~13G 显存挡住生图等任务。
# Electron 每 30s touch .ui_alive_<pid>, 退出即删; 看门狗见无新鲜心跳且持续 IDLE_EXIT_SEC
# 秒 -> 停服务栈并退出(下次打开程序自动重拉, 冷启动 ~42s 且有进度提示)。0=纯常驻。
IDLE_EXIT_SEC = int(os.environ.get('AITEACHER_IDLE_EXIT', '2700'))
UI_HB_MAX_AGE = 90
LAST_ACTIVE = 0.0
DETACHED = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
# 1002黑框教训: DETACHED_PROCESS(0x8)让子进程无console, uv venv启动器re-exec base
# 解释器时被迫新建可见console=任务栏黑框; CREATE_NO_WINDOW给隐藏console供继承

TTS_EXE = os.path.join(PACK, 'audiocpp', 'gpu', 'audiocpp_server.exe')
if not os.path.exists(TTS_EXE): TTS_EXE = os.path.join(LEGACY, 'gpu', 'audiocpp_server.exe')
def _render_tts_cfg():
    '''tts配置模板({AUDIOCPP_ROOT}占位符) -> 本机实际路径, 每次启动重渲染'''
    tpl = os.path.join(PACK, 'tools', 'scripts', 'tts_demo_voice.json')
    root = LEGACY
    # 1019lite: 判据从"有没有 audiocpp_server.exe"改成"目录在不在" —— 低配版包刻意不带
    # audiocpp\\gpu|models|venv(约 7.7G 运行时不用的死重), 若仍按 exe 存在判定,
    # 渲染出的 root 会回退到开发机 D:\\AI-ModeLs\\audiocpp-portable → 新机上是个死路径。
    for cand in (os.path.join(PACK, 'audiocpp'), LEGACY):
        if os.path.isdir(cand): root = cand; break
    root = root.replace(os.sep, '/')   # 统一正斜杠: JSON模板里拼接的是 /models/...
    out = os.path.join(BASE, 'data', 'tts_runtime.json')
    try:
        s = open(tpl, encoding='utf-8').read().replace('{AUDIOCPP_ROOT}', root.replace(os.sep, '/'))
        try:
            cur = open(out, encoding='utf-8').read()
        except OSError:
            cur = None
        if cur != s:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            # 1013probe: 原子替换 —— 直接 open('w') 时 exe 可能读到半截/占位中,
            # 报 "audiocpp_server failed: failed to open text file" 后立即退出
            # (11:28~11:40 急杀急拉期铁证) -> 端口永不就绪 -> 深探10061 -> 再杀, 死循环。
            _tmp = out + '.tmp%d' % os.getpid()
            with open(_tmp, 'w', encoding='utf-8') as _f:
                _f.write(s)
            os.replace(_tmp, out)
    except OSError:
        pass
    return out   # exe必须拿渲染后的runtime配置, 不是模板
TTS_CFG = _render_tts_cfg()   # 1002整合包: 模板渲染, 包内audiocpp优先
LT_PY = os.path.join(BASE, 'venv', 'Scripts', 'python.exe')
# 1013cv 真突破: 凌雪专用 TTS = CosyVoice2-0.5B zero-shot (Voice-Pro 包内权重; 音色一次注册,
# 不随文本抽签)。qwen3-tts-0.6B 实测句间音色距离 0.58(=换人级), CosyVoice2 = 0.09(=同一人)。
# 回滚备份: aiteacher_watchdog.py.pre_1013cv
# 1019portable: 克隆语音(Voice-Pro)根目录 —— 包内 runtime/voice-pro 优先,
#   环境变量 LX_VP 可覆盖, 开发机 D: 路径仅作兜底。完整包已把整个运行环境打进包内。
def _resolve_vp_dir():
    e = os.environ.get('LX_VP')
    if e and os.path.isdir(e):
        return e
    try:
        cand = os.path.join(os.path.dirname(BASE), 'runtime', 'voice-pro')
        if os.path.isdir(cand):
            return cand
    except Exception:
        pass
    return r'D:\AI-ModeLs\Voice-Pro'

VP_DIR = _resolve_vp_dir()
CVPY = os.path.join(VP_DIR, 'installer_files', 'env', 'Scripts', 'python.exe')
LX_TTS_PORT = 18091

SERVICES = [
    # 1002: llama(Qwen3-8B @8090) 服务块已移除 — LLM 切云端 tokenplan，腾 5.1GB 显存打破溢出死循环
    # （回滚: aiteacher_watchdog.py.bak-cloudllm-1002）
    # 1015retire: tts(18080 qwen3) 服务块已移除 — 12师+demo 全部迁 CosyVoice2@18091（唯一真相源=voice_registry.json,
    # 现13键含 default=Brenda演示位）。省 5~6GB 显存, 消灭双音色拓扑。
    # 回滚: aiteacher_watchdog.py.pre_1015retire + 恢复 SERVICES[0] 注释块 + 深探段改回 18080。
    # 1020mini: 原 8010 卡通系 viseme 引擎块已整块移除 —— 本档只有 Coach Arthur / Coach Mia
    # 两位 musetalk 老师, 由 8011 兼做 engine/web/desktop.html(桌面窗口)的宿主。
    # 回滚: 用 tools/package_share.py --lowmem 重新打包(13 师 + 卡通系)。
    # 1017: livetalking_musetalk(8011) 块已移到本列表末尾 —— 见下方 "1017 顺序修正" 注释。
    {
        # 1015retire: 曾要求排在SERVICES[0]=audiocpp之后(18080深探引用); 现深探已改指本服务自身, 无顺序依赖
        'name': 'lx_tts', 'port': LX_TTS_PORT,
        'boot_group': 1,   # 1018: 并行启动组(同组并行加载/组间串行; 未标=按列表序号)
        'health': 'http://127.0.0.1:%d/health' % LX_TTS_PORT,
        'expect': '"ok"',
        # 1017: v2 -> v3 (AR快路 lm fp16 + 跳过lm_head白算; 支持 seed 确定性)
        #       实测 AR 34.6->44.3 tok/s, RTF 0.85->0.72; seed 让引擎侧锁句种子真正生效。
        #       回滚 = 改回 cosyvoice_lx_server_v2.py (文件仍在, 协议完全兼容)
        'cmd': [CVPY, os.path.join(BASE, 'tools', 'cosyvoice_lx_server_v3.py')],
        'cwd': VP_DIR,
        'grace': 420,            # 冷加载 20~60s(首次冷 ~57s) + 首句预热
        'deep': False,           # 端口只在模型加载完成后监听 -> health 即代表真能合成
    },
    {
        # 1016双worker: 第二个 CosyVoice2 实例(18092), 供引擎并行预取/轮询(治"说两句就卡")
        # 引擎侧端点表: engine/data/tts_endpoints.json (热更新; 删掉即回单口)
        'name': 'lx_tts2', 'port': 18092,
        'boot_group': 1,   # 1018: 并行启动组(同组并行加载/组间串行; 未标=按列表序号)
        'health': 'http://127.0.0.1:18092/health',
        'expect': '"ok"',
        'cmd': [CVPY, os.path.join(BASE, 'tools', 'cosyvoice_lx_server_v3.py'), '--port', '18092'],
        'cwd': VP_DIR,
        'grace': 420,
        'deep': False,
    },
    {
        # 1017 顺序修正: 本块原在 lx_tts 之前 —— 冷启动闸门(boot_gate_ready)要求"前序服务健康才放行后序",
        # 而 musetalk 加载最重(grace 360s, 实测曾 363s)、显存紧张时反复失败 → 它永久不健康 → 
        # lx_tts 被永久挡住(当日 18091 死于 21:13 后 20 分钟都拉不起来, 根因即此)。
        # 两个引擎都依赖 TTS, 故 TTS 必须先起; 最重的 musetalk 放最后, 它失败也不再连坐。
        # 1020mini: 主位=Coach Arthur(与本档两位老师一致); 1017 实录: musetalk 素材喂 viseme 引擎会卡死,
        # 故本档只留这一个真人引擎 —— 原 8010 卡通引擎块已整块移除(见文件顶部 1020mini 说明)。
        'name': 'livetalking_musetalk', 'port': 8011,
        'boot_group': 1,   # 1018b 实测: 全并行(F)/或改回2=musetalk独占(G)   # 1018: 并行启动组(同组并行加载/组间串行; 未标=按列表序号)
        'health': 'http://127.0.0.1:8011/',
        'expect': None,
        # 1009fix(Arthur抢主位) 与 1015bs8(batch_size=8) 见 git 历史/回滚位: aiteacher_watchdog.py.pre_1015retire
        'cmd': [LT_PY, 'app.py', '--transport', 'webrtc', '--model', 'musetalk',
                '--avatar_id', 'coach_arthur', '--tts', 'audiocpp',
                '--batch_size', '8',
                '--TTS_SERVER', 'http://127.0.0.1:18091', '--REF_FILE', 'arthur',
                '--llm_provider', 'tokenplan',
                '--listenport', '8011', '--max_session', '4'],
        'cwd': BASE,
        # 1018c: expandable_segments 让缓存块按需增长/复用, 避免 fp16 权重被大块夹碎后
        # 反复预留新块(12G 卡余量敏感, 实测有效)。
        'env': {'PYTORCH_CUDA_ALLOC_CONF': 'expandable_segments:True'},
        'grace': 360,            # musetalk GPU模型(whisper+vae+unet)加载慢 (1013: 300→360, 实测曾需363s)
        'deep': False,
    },
]

# 1015retire: 深探 grace 基准(唯一真源=SERVICES里lx_tts的grace, 防两处数字漂移)
# ---- 1018c 显存自适应: 12G 卡也跑得动 ----
# 全栈实测净占用(逐个杀测 delta): musetalk 9005 + lx_tts 2033 + lx_tts2 2031 + viseme 0 = 13069 MiB(12.76G)。
# 12G 装不下(差 781 MiB) -> 自动砍副 TTS worker(省 2031 MiB) -> 11038 MiB(10.78G), 留约 1.2G 余量。
# 代价: 失去 1016 双 worker 的并行预取(治说两句就卡)。12G 场景能跑优先于最优。
# 覆盖: 环境变量 AITEACHER_TTS_WORKERS=1|2 (0/未设 = 按显存自动)
def _vram_total_gb():
    try:
        o = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=10).stdout
        nums = [int(x) for x in o.split() if x.strip().isdigit()]
        return (nums[0] / 1024.0) if nums else 0.0
    except Exception:
        return 0.0


# ---- 1019lite 运行档位: auto / full / lite / ultra  (16~24G 机器可用) ----
# 决策轴 = RAM(内存) 与 VRAM(显存) 双轴, 先取更紧的一轴:
#   full : 双TTS worker + 双引擎(viseme+musetalk)  实测 ~12.8G VRAM / ~21G RAM
#   lite : 单TTS worker + 双引擎                    实测 ~10.8G VRAM / ~17G RAM  <- 16~24G 机器
#   ultra: 单TTS worker + 仅卡通引擎(不起 musetalk) 实测 ~5.3G VRAM / ~11G RAM   <- 16G 机器兜底
#         注意: ultra 下真人系老师(aiteacher_demo / coach_*)不可用, 只能用卡通系(lingxue 等)。
# 档位来源(优先级): 环境变量 SPEAKTOSI_MODE > 包根 config/runtime_mode.json > auto
# 兼容: 老环境变量 AITEACHER_TTS_WORKERS=1|2 仍可单独压 worker 数。
MODE_FILE = os.path.join(PACK, 'config', 'runtime_mode.json')
MODE_ACTIVE_FILE = os.path.join(BASE, 'data', 'runtime_mode_active.json')
MODE_ORDER = ('full', 'lite', 'ultra')          # 能力从高到低
ENGINE_VISEME = 'livetalking'
ENGINE_MUSETALK = 'livetalking_musetalk'


def _ram_total_gb():
    """物理内存总量(GB)。ctypes 读 GlobalMemoryStatusEx —— 零依赖(psutil 装了也走这条)。"""
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

        st = _MS()
        st.dwLength = ctypes.sizeof(_MS)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return st.ullTotalPhys / (1024.0 ** 3)
    except Exception:
        pass
    try:
        import psutil
        return psutil.virtual_memory().total / (1024.0 ** 3)
    except Exception:
        return 0.0


def _auto_mode(vram, ram):
    """双轴自动判定: 内存<18G 或 显存<11G 只能 ultra; 任一 <29G/16G 则 lite; 否则 full。"""
    if (ram and ram < 18) or (vram and vram < 11):
        return 'ultra'
    # 显存探测不到(nvidia-smi 缺失/驱动异常) → 保守走 lite, 不冒险开双 worker
    if (ram and ram < 29) or (vram < 1) or (vram < 16):
        return 'lite'
    return 'full'


def _resolve_mode(vram, ram):
    """返回 (mode, requested, source)。requested=用户要的档; mode=实际生效档。"""
    req = (os.environ.get('SPEAKTOSI_MODE') or '').strip().lower()
    src = 'env'
    if req not in MODE_ORDER:
        req, src = '', 'auto'
        try:
            with open(MODE_FILE, encoding='utf-8') as f:
                j = json.load(f) or {}
            m = str(j.get('mode', 'auto')).strip().lower()
            if m in MODE_ORDER:
                req, src = m, 'file'
            elif m in ('auto', '', 'none'):
                src = 'file:auto'
        except Exception:
            pass
    if req:
        return req, req, src
    return _auto_mode(vram, ram), 'auto', src


def _tts_endpoints_sync():
    """1018c: 引擎端点表跟随实际 worker 数; 单 TTS 时把双口登记移开(=回到单口)。"""
    fp = os.path.join(BASE, 'data', 'tts_endpoints.json')
    try:
        if TTS_WORKERS < 2:
            if os.path.exists(fp):
                os.replace(fp, fp + '.disabled_1018')
                return '双口登记已移开(单 TTS)'
        elif os.path.exists(fp + '.disabled_1018') and not os.path.exists(fp):
            os.replace(fp + '.disabled_1018', fp)
            return '双口登记已恢复'
    except Exception as e:
        return 'endpoints 同步失败: %s' % e
    return ''


VRAM_GB = _vram_total_gb()
RAM_GB = _ram_total_gb()
MODEL_MODE, MODE_REQ, MODE_SRC = _resolve_mode(VRAM_GB, RAM_GB)
# worker 数: 档位决定(full=2, lite/ultra=1); AITEACHER_TTS_WORKERS 可再压一层(只减不增)
TTS_WORKERS = 2 if MODEL_MODE == 'full' else 1
_env_w = int(os.environ.get('AITEACHER_TTS_WORKERS', '0') or 0)
if _env_w:
    TTS_WORKERS = min(TTS_WORKERS, _env_w)
if TTS_WORKERS < 2:
    SERVICES = [s for s in SERVICES if s['name'] != 'lx_tts2']
# 1020mini: 本档没有卡通引擎 —— ultra(只跑卡通)无意义, 请求 ultra 时按 lite 跑(最低需 >=12G 显存)
if MODEL_MODE == 'ultra':
    MODEL_MODE = 'lite'


def _write_active_mode(note=''):
    """把生效档位写到 engine/data/runtime_mode_active.json —— 后端 /health 读它回报界面。"""
    try:
        os.makedirs(os.path.dirname(MODE_ACTIVE_FILE), exist_ok=True)
        j = {'mode': MODEL_MODE, 'requested': MODE_REQ, 'source': MODE_SRC,
             'ram_gb': round(RAM_GB, 1), 'vram_gb': round(VRAM_GB, 1),
             'tts_workers': TTS_WORKERS, 'musetalk': MODEL_MODE != 'ultra',
             'engines': [s['name'] for s in SERVICES if s['name'].startswith('livetalking')],
             'note': note, 'pid': os.getpid(),
             'ts': datetime.now().isoformat(timespec='seconds')}
        with open(MODE_ACTIVE_FILE, 'w', encoding='utf-8') as f:
            json.dump(j, f, ensure_ascii=False, indent=2)
        return j
    except Exception as e:
        print('[mode] write active fail: %s' % e, flush=True)
        return {}


def _inject_env_keys():
    """1019setup: 首次配置向导把云端 token 写在 app/backend/.env。
    看门狗起服务前把它注入 os.environ → 两个引擎(--llm_provider tokenplan)与 TTS 子进程直接继承,
    新机无需任何手写环境变量。(engine/llm.py 亦自带同一份回读, 双保险。)
    返回注入的变量个数; 只认 3 个白名单前缀, 不吞别的行。"""
    n = 0
    try:
        p = os.path.join(PACK, 'app', 'backend', '.env')
        if not os.path.isfile(p):
            return 0
        with open(p, encoding='utf-8', errors='replace') as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith('#') or '=' not in ln:
                    continue
                k, v = ln.split('=', 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if v and k.startswith(('DASHSCOPE_API_', 'ALIBABA_CODING_PLAN_API_', 'BAILIAN_API_')):
                    os.environ[k] = v
                    n += 1
    except Exception as e:
        print('[cred] inject fail: %s' % e, flush=True)
    return n


LX_TTS_GRACE = next(s['grace'] for s in SERVICES if s['name'] == 'lx_tts')


def log(msg):
    line = '%s %s' % (datetime.now().strftime('%m-%d %H:%M:%S'), msg)
    try:
        if os.path.exists(LOGF) and os.path.getsize(LOGF) > 5 * 1024 * 1024:
            os.replace(LOGF, LOGF + '.old')
        with open(LOGF, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line, flush=True)


def port_listening(port):
    s = socket.socket()
    s.settimeout(1.5)
    try:
        return s.connect_ex(('127.0.0.1', port)) == 0
    finally:
        s.close()


def health_ok(svc):
    try:
        req = urllib.request.Request(svc['health'], headers={'User-Agent': 'watchdog'})
        r = urllib.request.urlopen(req, timeout=8)
        body = r.read(200).decode(errors='replace')
        if svc['expect'] and svc['expect'] not in body:
            return False
        return True
    except urllib.error.HTTPError as e:
        return e.code < 500      # 302/404 = 服务活着
    except Exception as e:
        # 1013probe: 超时/忙 != 死。GPU占满/合成排队时 /health 会超时,
        # 旧逻辑判 FAIL -> 3连FAIL(60s) -> 把正在干活的服务杀掉
        # (watchdog.log 11:28~11:40 连杀5次 = "掉词/没声音"根因之一)。
        # 忙 -> 返回 None(未知), 不计数; 由深探仲裁(见 main)。
        m = str(e).lower()
        if isinstance(e, (TimeoutError, socket.timeout)) or 'timed out' in m or 'timeout' in m:
            return None
        return False        # 连接被拒/坏响应 = 真死


def deep_tts_ok():
    """真实合成一句（卡死的服务端口在但不干活，只有真调用能揭穿）。
    1015retire: 18080(qwen3) -> 18091(CosyVoice2 v2, voice=lingxue)。回滚见 pre_1015retire。"""
    try:
        body = json.dumps({'model': 'cosyvoice2', 'input': 'Watchdog probe.',
                           'voice': 'arthur', 'response_format': 'wav'}).encode()
        req = urllib.request.Request('http://127.0.0.1:%d/v1/audio/speech' % LX_TTS_PORT,
                                     data=body, headers={'Content-Type': 'application/json'})
        # 1013probe: 30s 太短 —— 实测单句合成最长 31.5s, 排在真合成后面必然超时 ->
        # 误判"卡死" -> 杀健康服务。放到 75s(>31.5s x2) 留足排队余量。
        r = urllib.request.urlopen(req, timeout=75)
        return len(r.read()) > 800
    except Exception as e:
        log('deep TTS probe FAIL: %s %s' % (type(e).__name__, str(e)[:100]))
        return False


def pid_on_port(port):
    try:
        out = subprocess.run('netstat -ano | findstr LISTENING | findstr ":%d "' % port,
                             shell=True, capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(':%d' % port):
                return int(parts[4])
    except Exception:
        pass
    return None


def is_our_proc(pid):
    """1019guard: only kill OUR stack procs (python + SpeakTosi scripts).
    Third-party apps grabbing our port (measured: Feishu.exe bound 0.0.0.0:8011)
    must NEVER be killed by the watchdog."""
    try:
        import psutil
        pr = psutil.Process(pid)
        if not (pr.name() or '').lower().startswith('python'):
            return False
        cmd = ' '.join(pr.cmdline() or []).lower()
        return ('speaktosi' in cmd) or ('app.py' in cmd) or ('aiteacher_watchdog' in cmd) or ('cosyvoice' in cmd)
    except Exception:
        return False


def kill_pid(pid):
    subprocess.run('taskkill /F /T /PID %d' % pid, shell=True,
                   capture_output=True, timeout=20)


def kill_svc(svc_name, pid, port):
    """杀「记录pid + 端口真占用者」双保险；等待端口释放，仍在则补杀。
    修 1002 风暴根因：st['pid'] 可能是早已死掉的陈旧值，真占用者(如 10132)杀不到，
    半死服务占端口 -> health 永远 FAIL -> 每轮 kill 空气 + relaunch -> venv python.exe 往外跳。"""
    targets = set()
    if pid and is_our_proc(pid):
        targets.add(pid)
    op = pid_on_port(port)
    if op and is_our_proc(op):
        targets.add(op)
    elif op:
        log('%s port %d held by EXTERNAL pid=%d -> never kill third-party; engine rebinds loopback to coexist' % (svc_name, port, op))
    for t in targets:
        kill_pid(t)
    for _ in range(8):
        time.sleep(1)
        op = pid_on_port(port)
        if not op:
            return
        if op not in targets:
            if not is_our_proc(op):
                log('%s port %d held by EXTERNAL pid=%d -> never kill third-party' % (svc_name, port, op))
                return
            log('%s port still held by %d -> extra kill' % (svc_name, op))
            targets.add(op)
            kill_pid(op)
    log('%s WARN port %d still held after 8s' % (svc_name, port))


def pid_start_time(pid):
    """进程创建时间(epoch秒)。BUG3收养用：看门狗重启后靠它还原加载起点。"""
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        return None


def launch(svc, st):
    svclog = open(os.path.join(BASE, 'watchdog_%s.out.log' % svc['name']), 'ab')
    # 净化环境：父进程(Hermes终端)的 PYTHONPATH/PYTHONHOME 会污染服务 venv
    # （实测症状: numpy._core._multiarray_umath 缺失）
    # 1015storm: 同理 HTTP(S)_PROXY/ALL_PROXY 泄漏(如10808代理)会让 urllib 健康检查
    # 连"本机端口"都绕道代理 -> 代理一挂全判死 -> 误杀健康服务(19:04~19:35 连杀4轮根因)。
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP',
                                'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY')}
    env.update(svc.get('env') or {})   # 1018: 服务级环境变量覆盖(如 PYTORCH_CUDA_ALLOC_CONF)
    p = subprocess.Popen(svc['cmd'], cwd=svc['cwd'], stdout=svclog,
                         stderr=subprocess.STDOUT, creationflags=DETACHED,
                         close_fds=True, env=env)
    st['pid'] = p.pid
    st['launched'] = time.time()
    st['fails'] = 0
    st['restarts'].append(time.time())
    log('LAUNCH %s pid=%d' % (svc['name'], p.pid))


def _boot_group(svc, SERVICES):
    """1018: 该服务的启动组号。显式 boot_group 优先; 未标则按列表序号自成一组(=旧串行行为)。"""
    g = svc.get('boot_group')
    if g is not None:
        return g
    return [s['name'] for s in SERVICES].index(svc['name'])


def ui_alive():
    """1018: 界面是否开着。双判据:
      1) Electron 每 30s touch engine/.ui_alive_<pid>, 退出即删 —— 主判据;
      2) Electron 的本地后端 8765 在监听 —— 兜底(界面在后端必然在)。
    注意: main.js 里 LT_BASE = 包根/engine, 所以心跳文件落在 engine/ 而不是包根。
    """
    try:
        for f in os.listdir(BASE):
            if f.startswith('.ui_alive_'):
                if time.time() - os.path.getmtime(os.path.join(BASE, f)) < UI_HB_MAX_AGE:
                    return True
    except Exception:
        pass
    return port_listening(8765)


def boot_gate_ready(states, SERVICES, svc):
    """BUG1 串行闸门 —— 1018 升级为"并行分组"版: 只等【更早的组】健康, 同组服务并行加载。

    动机(Owen 1018 反馈"打开后一直提示连接失败, 过好久才连上"; 实测冷启动 129s):
      旧版全串行排队 = viseme 6s + lx_tts 45s + lx_tts2 39s + musetalk 32s ≈ 122s。
      但 lx_tts / lx_tts2 是两个只吃 ~2GB 显存的同构进程, 冷启动瓶颈在磁盘+CPU 读权重,
      不在显存 → 让它们互相等待纯属浪费。组1(viseme + 双TTS 并行) → 组2(musetalk 9GB 大户独占)。
    预期 129s → ~77s(-40%)。回滚 = 删 SERVICES 里的 boot_group 键, 或 BOOT_STAGGER=False。"""
    if not BOOT_STAGGER:
        return True
    me = svc['name']
    myg = _boot_group(svc, SERVICES)
    for other in SERVICES:
        on = other['name']
        if on == me:
            return True            # 轮到自己了
        if _boot_group(other, SERVICES) >= myg:
            continue               # 同组/后组不阻塞(并行加载)
        ost = states[on]
        if ost['pid'] or port_listening(other['port']):
            if not health_ok(other):
                return False       # 更早的组还在加载 → 等它, 别挤
    return True


def main():
    # 单实例锁
    lock = socket.socket()
    try:
        lock.bind(('127.0.0.1', LOCK_PORT))
        # 1006: 必须listen——Electron main.js wdRunning()用TCP connect探测锁,
        # 只bind不listen时探测永远失败 → 每次开机重复拉看门狗(靠bind冲突兜底退出, 虽不打架但日志脏)
        lock.listen(1)
    except OSError:
        print('another watchdog is running, exit')
        return
    log('=== watchdog start (pid %d) policy=1013probe busy-not-dead ===' % os.getpid())

    # BUG1/BUG2 需要的冷启动时间基准：看门狗自身启动时刻 + 串行闸门窗口
    global WD_START
    WD_START = time.time()
    log('1019lite RAM=%.1fG VRAM=%.1fG mode=%s(req=%s via %s) engines=%s -> TTS_WORKERS=%d %s'
        % (RAM_GB, VRAM_GB, MODEL_MODE, MODE_REQ, MODE_SRC,
           ','.join(s['name'] for s in SERVICES if s['name'].startswith('livetalking')),
           TTS_WORKERS, _tts_endpoints_sync()))
    _write_active_mode()
    log('token vars injected=%d (app/backend/.env)' % _inject_env_keys())
    global LAST_ACTIVE
    LAST_ACTIVE = time.time()

    states = {s['name']: {'pid': None, 'launched': 0.0, 'fails': 0, 'busy': 0,
                          'restarts': [], 'backoff_until': 0.0} for s in SERVICES}
    # BUG2: 初值给 now 而非 0.0 —— 0.0 会让首轮立即触发深探, 那时TTS往往还在加载
    last_deep = time.time()

    # BUG3 收养：看门狗(重)启动时，端口已在监听的服务不是"死的"，
    # 用端口占用者进程的创建时间当加载起点，给它应有的grace宽限，
    # 否则72s内3连FAIL就会杀掉一个还在加载模型的musetalk。
    if ADOPT_RUNNING:
        for svc in SERVICES:
            op = pid_on_port(svc['port'])
            if op:
                ct = pid_start_time(op)
                states[svc['name']]['pid'] = op
                states[svc['name']]['launched'] = ct or time.time()
                log('ADOPT %s pid=%d (port %d already listening, load_start=%s)'
                    % (svc['name'], op, svc['port'],
                       datetime.fromtimestamp(ct).strftime('%H:%M:%S') if ct else 'now'))

    while True:
        now = time.time()
        for svc in SERVICES:
            st = states[svc['name']]
            if now < st['backoff_until']:
                continue
            up = port_listening(svc['port'])
            if not up:
                # 未监听：在宽限期内认为是正常启动中，否则拉起
                if st['launched'] and now - st['launched'] < svc['grace']:
                    continue
                # 崩溃风暴检测
                st['restarts'] = [t for t in st['restarts'] if now - t < 600]
                if len(st['restarts']) >= 5:
                    st['backoff_until'] = now + 60
                    log('!! %s restarted 5x in 10min — backoff 60s (check watchdog_%s.out.log)'
                        % (svc['name'], svc['name']))
                    continue
                # BUG1 冷启动串行闸门：前序服务还在加载就等，别3个一起抢显存
                if not boot_gate_ready(states, SERVICES, svc):
                    continue
                launch(svc, st)
                continue
            # 端口在：健康检查 (三态: True活 / None忙·未知 / False死)
            _h = health_ok(svc)
            if _h is True:
                st['fails'] = 0
                st['busy'] = 0
            elif _h is None:
                # 1013probe: 忙/超时不判死, 只记 busy 计数(连续6次≈2分钟才触发深探仲裁)
                st['busy'] = st.get('busy', 0) + 1
                continue
            else:
                # 1002修重启风暴: 服务先绑端口再慢慢加载GPU模型(5090上1~3分钟),
                # 加载期 /health 超时属正常状态。grace 宽限必须同样豁免 health FAIL,
                # 否则72秒3连FAIL就杀→重载→再杀, 每次重载=整卡显存重分配=全系统卡顿
                if st['launched'] and now - st['launched'] < svc['grace']:
                    continue   # 还在加载, 不计数
                st['fails'] += 1
                log('%s health FAIL %d/3' % (svc['name'], st['fails']))
                if st['fails'] >= 3:
                    pid = st['pid'] or pid_on_port(svc['port'])
                    log('%s hung -> kill pid=%s (+port owner) & relaunch' % (svc['name'], pid))
                    kill_svc(svc['name'], st['pid'], svc['port'])
                    st['launched'] = 0.0
                    st['fails'] = 0
        # 深度探测：TTS 真实合成
        # BUG2 修复：① 深探前先过grace(加载中不探) ② last_deep 初始给 now,
        # 避免首轮立即深探 ③ 必须连败 DEEP_FAILS_NEEDED 次才杀, 单次冷启失败不判死刑
        # 1015retire: 探测对象 18080/tts -> 18091/lx_tts。SERVICES[0] 现为 livetalking。
        if now - last_deep > 300 and port_listening(LX_TTS_PORT):
            tstate = states['lx_tts']
            in_grace = tstate['launched'] and now - tstate['launched'] < LX_TTS_GRACE
            # 1013probe: 只在"看起来不对"时做真合成探针。
            # 常态每5分钟真合成一次会抢占唯一的合成线程 -> 给正在对话的用户加卡顿/吞字。
            looks_bad = tstate['fails'] > 0 or tstate.get('busy', 0) >= 6
            if not in_grace and looks_bad:
                last_deep = now
                if deep_tts_ok():
                    # 深探成功 = 服务确实活着 -> 撤销FAIL/忙计数, 绝不误杀
                    tstate['deep_fails'] = 0
                    tstate['fails'] = 0
                    tstate['busy'] = 0
                else:
                    tstate['deep_fails'] = tstate.get('deep_fails', 0) + 1
                    if tstate['deep_fails'] >= DEEP_FAILS_NEEDED:
                        pid = tstate['pid'] or pid_on_port(LX_TTS_PORT)
                        log('TTS deep probe failed %d/%d -> kill pid=%s & relaunch'
                            % (tstate['deep_fails'], DEEP_FAILS_NEEDED, pid))
                        kill_svc('lx_tts', tstate['pid'], LX_TTS_PORT)
                        tstate['launched'] = 0.0
                        tstate['fails'] = 0
                        tstate['deep_fails'] = 0
        if IDLE_EXIT_SEC > 0:   # 1018 空闲自动释放
            if ui_alive():
                LAST_ACTIVE = now
            elif now - LAST_ACTIVE > IDLE_EXIT_SEC:
                log('IDLE %ds 无界面心跳(最后活跃 %s) -> 停服务退还显存后退出, 下次打开会自动重拉'
                    % (IDLE_EXIT_SEC, datetime.fromtimestamp(LAST_ACTIVE).strftime('%m-%d %H:%M:%S')))
                import psutil as _ps
                for svc in SERVICES:
                    op = pid_on_port(svc['port'])
                    if not op:
                        continue
                    try:
                        _ps.Process(op).kill()
                        log('  idle-stop %s pid=%d' % (svc['name'], op))
                    except Exception as e:
                        log('  idle-stop %s pid=%d 失败: %s' % (svc['name'], op, e))
                time.sleep(2)
                return
        time.sleep(CYCLE)


if __name__ == '__main__':
    main()
