// 桌面老师 - Electron 主进程
// 特性：透明无边框置顶窗口、鼠标穿透（随角色包围盒动态切换）、托盘、设置窗口、开机自启开关
const { app, BrowserWindow, Tray, Menu, ipcMain, screen, shell, nativeImage, dialog, Notification, clipboard } = require('electron');
const path = require('path');
const fs = require('fs');
const net = require('net');
const { spawn } = require('child_process');

// 文件日志（秒退排查用）
const LOGF = path.join(__dirname, 'main-log.txt');
function mlog(msg) { try { fs.appendFileSync(LOGF, new Date().toISOString() + " " + msg + "\n"); } catch {} }
mlog("boot main.js pid=" + process.pid);
if (process.env.AITEACHER_DEBUG) app.commandLine.appendSwitch("remote-debugging-port", "9222");
process.on('uncaughtException', (e) => mlog("UNCAUGHT " + (e && e.stack || e)));
process.on('unhandledRejection', (e) => mlog("REJECT " + (e && e.stack || e)));

const CFG_DIR = path.join(__dirname, 'config');
const CFG_FILE = path.join(CFG_DIR, 'electron.json');
fs.mkdirSync(CFG_DIR, { recursive: true });

// 1002fix: 老师名字动态读 persona.json（此前托盘/标题硬编码SpeakTosi，改名单像"没保存"）
function teacherName() {
  try {
    const p = JSON.parse(fs.readFileSync(path.join(__dirname, 'backend', 'config', 'persona.json'), 'utf8'));
    return (p.name || 'SpeakTosi').trim();
  } catch { return 'SpeakTosi'; }
}
function loadCfg() {
  const d = { autostart: false, bounds: null, model: 'AvatarSample_B.vrm', scale: 1.0, bubbleW: 86, x: 0.82, y: 0.0 };
  try { return { ...d, ...JSON.parse(fs.readFileSync(CFG_FILE, 'utf8')) }; } catch { return d; }
}
function saveCfg(c) { try { fs.writeFileSync(CFG_FILE, JSON.stringify(c)); } catch {} }
let cfg = loadCfg();

let win = null;       // 老师透明窗
let settings = null;  // 设置窗
let liveWin = null;   // 直播效果窗（WebRTC 口型+克隆声，与直播间同链路）
let tray = null;
let backend = null;
let backendRestarts = [];   // 1007r4: backend 自愈退避计数(60s滚动窗口)
let backendLastSpawn = 0;   // 1007r4: 上次 spawn 时刻(探测式自愈冷却用)
const BACKEND_RESPAWN_COOLDOWN = 25000;   // 1007r4: 探测式自愈冷却25s(backend冷启~10s, 留足余量防抖动重拉)
let quitting = false;

// ---------- 后端生命周期 ----------
function startBackend() {
  // 健康探测必须裸socket直连: fetch会走系统代理(1002病根: 代理假200→backend永不拉起)
  const s = net.connect(8765, '127.0.0.1');
  s.once('connect', () => { s.destroy(); });
  s.once('error', () => spawnBackend());
  s.setTimeout(1200, () => { s.destroy(); spawnBackend(); });
}
function spawnBackend() {
  const py = path.join(__dirname, 'backend', '.venv', 'Scripts', 'python.exe');
  const srv = path.join(__dirname, 'backend', 'server.py');
  if (!fs.existsSync(py)) { console.warn('后端 venv 未就绪'); return; }
  backendLastSpawn = Date.now();   // 1007r4: 探测式自愈的冷却锚点
  backend = spawn(py, [srv], { cwd: path.join(__dirname, 'backend'), windowsHide: true,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' } });
  backend.stdout.on('data', d => process.stdout.write('[backend] ' + d));
  backend.stderr.on('data', d => process.stderr.write('[backend!] ' + d));
  // 1007r4: backend 崩溃自愈。原来只 console.warn 就完事 → 会话中途崩了 UI 静默死掉，
  // 必须用户重开 App。现在带退避自动重拉（60s 内≥4次才停手并提示，防崩溃风暴）。
  backend.on('exit', (code, sig) => {
    backend = null;
    if (quitting) return;
    const now = Date.now();
    backendRestarts = backendRestarts.filter(t => now - t < 60000);
    backendRestarts.push(now);
    if (backendRestarts.length >= 4) {
      console.warn('后端 60s 内重启 4 次，停止自愈（双击 start_speaktosi.bat 或查 backend 日志）');
      mlog('BACKEND restart storm — giving up');
      return;
    }
    const wait = 2000 * backendRestarts.length;
    mlog(`BACKEND exited code=${code} sig=${sig} — respawn in ${wait}ms (attempt ${backendRestarts.length})`);
    setTimeout(() => { if (!quitting && !backend) spawnBackend(); }, wait);
  });
}
function stopBackend() {
  if (backend) { try { backend.kill(); } catch {} backend = null; }
}

// ---------- 看门狗生命周期（1002收编：桌面bat整合的核心——以前它靠桌面bat用 cmd /c 起，
// 常驻一个最小化黑框；现在由本程序用 pythonw 无窗口直拉，退出时可一并收掉） ----------
// 1002整合包: 引擎目录=包根/engine，探测不到再回退开发机旧路径
const LT_BASE = fs.existsSync(path.join(__dirname, '..', 'engine', 'aiteacher_watchdog.py'))
  ? path.resolve(__dirname, '..', 'engine')
  : 'D:\\Software\\LiveTalking';
function wdRunning() {   // 18999 单实例锁被占 = 看门狗活着
  return new Promise(res => {
    const s = net.connect(18999, '127.0.0.1');
    s.once('connect', () => { s.destroy(); res(true); });
    s.once('error', () => res(false));
    s.setTimeout(1200, () => { s.destroy(); res(false); });
  });
}
async function startWatchdog() {
  if (await wdRunning()) return 'already';
  const pyw = path.join(LT_BASE, 'venv', 'Scripts', 'pythonw.exe');
  const wd = path.join(LT_BASE, 'aiteacher_watchdog.py');
  if (!fs.existsSync(pyw) || !fs.existsSync(wd)) { mlog('看门狗文件缺失，无法拉起'); return 'missing'; }
  const env = { ...process.env, PYTHONIOENCODING: 'utf-8' };
  delete env.PYTHONPATH; delete env.PYTHONHOME;   // 防外部解释器污染 venv（1001铁律）
  const logf = fs.openSync(path.join(LT_BASE, 'watchdog_electron.log'), 'a');
  const p = spawn(pyw, [wd], { cwd: LT_BASE, env, detached: true, windowsHide: true,
    stdio: ['ignore', logf, logf] });
  p.unref(); fs.closeSync(logf);
  mlog('watchdog launched pid=' + p.pid + ' (pythonw, 无黑框)');
  return 'launched';
}
function stopStack() {   // 连根拔：看门狗进程树（含其拉起的 TTS/LT 子进程）
  // PS5.1 的 Stop-Process 没有 -Tree，用 taskkill /T 逐pid树杀
  const ps = 'Get-CimInstance Win32_Process -Filter "Name=\'pythonw.exe\' or Name=\'python.exe\'" ' +
    '| Where-Object { $_.CommandLine -like \'*aiteacher_watchdog*\' } | ForEach-Object ' +
    '{ taskkill /F /T /PID $_.ProcessId 2>&1 | Out-Null; "stopped " + $_.ProcessId }';
  const p = spawn('powershell.exe', ['-NoProfile', '-Command', ps], { windowsHide: true });
  let out = '';
  p.stdout.on('data', d => { out += d; });
  p.on('close', () => mlog('stopStack: ' + (out.trim() || 'watchdog not found')));
}

// ---------- 服务栈状态灯（托盘每5s刷新：backend/看门狗/数字人引擎/克隆声） ----------
async function tcpUp(port, ms = 900) {
  return new Promise(res => {
    const s = net.connect(port, '127.0.0.1');
    s.once('connect', () => { s.destroy(); res(true); });
    s.once('error', () => res(false));
    s.setTimeout(ms, () => { s.destroy(); res(false); });
  });
}
let svcState = { backend: false, watchdog: false, engine: false, tts: false };
async function probeSvcs() {
  const [b, w, e, t] = await Promise.all([tcpUp(8765), tcpUp(18999), tcpUp(8011), tcpUp(18091)]);  // 1015retire: 18080->18091
  svcState = { backend: b, watchdog: w, engine: e, tts: t };
  // 1007r4 探测式自愈：exit 回调只管得住"Electron 亲手 spawn 的" backend。
  // 但 backend 常由别处先起来(看门狗/手起/上次遗留的孤儿进程) → Electron 探测到 8765 已活
  // 就不 spawn，此时 backend 一旦崩掉没有任何人会拉它(实测本轮就撞上了：owner 是孤儿 env.exe 链)。
  // probeSvcs 每 5s 跑一次，是天然的监管点：端口不在 && 非退出中 && 冷却已过 → 拉起。
  if (!b && !quitting && Date.now() - backendLastSpawn > BACKEND_RESPAWN_COOLDOWN) {
    mlog('probeSvcs: 8765 DOWN -> respawn backend (探测式自愈)');
    spawnBackend();
  }
  if (tray) { tray.setToolTip(`${teacherName()}｜后端${b ? '●' : '○'} 引擎${e ? '●' : '○'} 克隆声${t ? '●' : '○'}｜右键管理`); refreshTrayMenu(); }
  return svcState;
}
const dot = k => (svcState[k] ? '🟢' : '⚪️');

// ---------- 老师窗口 ----------
function workArea() {
  try { return screen.getPrimaryDisplay().workArea; }
  catch { return { x: 0, y: 0, width: 1920, height: 1080 }; }
}
function createWindow() {
  const wa = workArea();
  const w = Math.round(wa.width * 0.30);
  const h = Math.round(wa.height * 0.88);
  // 竖版视频窗默认：高≈工作区76%，宽按 704:896
  const h0 = Math.round(wa.height * 0.76);
  const w0 = Math.round(h0 * 704 / 896);
  // overlayVideo 版本迁移：旧静态版的 bounds 比例不对，作废一次
  const saved = (cfg.overlayVideo === 2) ? (cfg.bounds || savedBounds) : null;
  const x = saved ? saved.x : Math.round(wa.x + wa.width - w0 - 10);
  const y = saved ? saved.y : Math.round(wa.y + wa.height - h0 - 10);
  win = new BrowserWindow({
    width: saved ? saved.width : w0, height: saved ? saved.height : h0, x, y,
    transparent: true, frame: false, alwaysOnTop: true, resizable: true, skipTaskbar: true,
    hasShadow: false, show: false, focusable: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false, backgroundThrottling: false,
      autoplayPolicy: 'no-user-gesture-required',
    },
  });
  win.setAlwaysOnTop(true, 'screen-saver');
  win.setMenu(null);
  win.once('ready-to-show', () => { if (mode !== 'hidden') win.showInactive(); });
  win.webContents.on('console-message', (e, level, msg) => mlog('RENDER[' + level + '] ' + String(msg).slice(0, 300)));
  win.webContents.on('did-fail-load', (e, code, desc) => mlog('FAILLOAD ' + code + ' ' + desc));
  win.webContents.once('dom-ready', () => {
    mlog("dom-ready");
    setTimeout(() => {
      win.webContents.executeJavaScript("window.__blink && __blink(2600)").catch(()=>{});
      const js = `(() => { const r=(el)=>{if(!el)return null;const b=el.getBoundingClientRect();return [Math.round(b.left),Math.round(b.top),Math.round(b.width),Math.round(b.height)];}; return JSON.stringify({ win:[innerWidth,innerHeight], img:r(document.getElementById('bust')), wrap:r(document.getElementById('teacherWrap')), lid:getComputedStyle(document.getElementById('lidL')||document.body).background.slice(0,80), lidOp:(document.getElementById('lidL')?getComputedStyle(document.getElementById('lidL')).opacity:null), skin:window.__SKIN||null, dnd:window.__DND===undefined?null:window.__DND }); })()`;
      win.webContents.executeJavaScript(js).then(s => mlog("AUDIT " + s)).catch(e => mlog("AUDIT ERR " + e));
    }, 1900);
    // 麦克风探针：真测 Electron 内 getUserMedia（语音输入链路第一环，此前从未验证）
    setTimeout(() => {
      const mic = `(async () => { const o = { hasMD: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) };
        try { const d = await navigator.mediaDevices.enumerateDevices(); o.audioInputs = d.filter(x=>x.kind==='audioinput').length; } catch(e){ o.enumErr = e.name; }
        try { const s = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1}}); o.gUM = 'OK:'+s.getAudioTracks().length; s.getTracks().forEach(t=>t.stop()); } catch(e){ o.gUM = 'FAIL:'+e.name+':'+e.message; }
        return JSON.stringify(o); })()`;
      win.webContents.executeJavaScript(mic).then(s => mlog("MICPROBE " + s)).catch(e => mlog("MICPROBE ERR " + e));
    }, 2600);
  });
  win.loadFile(path.join(__dirname, 'renderer', 'overlay.html'));   // 视频版老师（WebRTC + aiteacher_demo）
  win.once('did-finish-load', () => { mlog("bounds " + JSON.stringify(win.getBounds())); });
  // 麦克风权限：显式放行（语音输入链路）
  win.webContents.session.setPermissionRequestHandler((wc, perm, cb) => cb(perm === 'media' || perm === 'audioCapture'));
  win.webContents.session.setPermissionCheckHandler((wc, perm) => perm === 'media' || perm === 'audioCapture');

  // 保存位置
  const persist = () => { if (win && !win.isDestroyed()) { const b = win.getBounds(); cfg.bounds = b; saveCfg(cfg); } };
  win.on('moved', persist); win.on('resized', persist);
  win.on('close', (e) => { if (!quitting) { e.preventDefault(); win.hide(); } });
}

// 鼠标穿透：渲染层报告多个命中矩形（头/麦克风/输入条/气泡），任一含光标即可交互，否则穿透
let ignoreAll = false;
ipcMain.on('hitbox', (ev, rects) => {
  if (!win || win.isDestroyed()) return;
  const list = Array.isArray(rects) ? rects : [rects];
  const pt = screen.getCursorScreenPoint();
  const [wx, wy] = win.getPosition();
  let inside = false;
  for (const r of list) {
    if (pt.x >= wx + r.x && pt.x <= wx + r.x + r.w && pt.y >= wy + r.y && pt.y <= wy + r.y + r.h) { inside = true; break; }
  }
  const next = !inside;
  if (next !== ignoreAll) { ignoreAll = next; win.setIgnoreMouseEvents(ignoreAll, { forward: true }); }
});

ipcMain.on('teacher:hide', () => { if (win) win.hide(); });
// 自检：把渲染层关键元素几何+lid色写日志（远程验收用）
ipcMain.handle('teacher:audit', async () => {
  if (!win || win.isDestroyed()) return null;
  const js = `(() => { const r=(el)=>{if(!el)return null;const b=el.getBoundingClientRect();return [Math.round(b.left),Math.round(b.top),Math.round(b.width),Math.round(b.height)];}; return JSON.stringify({ win:[innerWidth,innerHeight], img:r(document.getElementById('bust')), wrap:r(document.getElementById('teacherWrap')), lidL:(document.getElementById('lidL')?getComputedStyle(document.getElementById('lidL')).background.slice(0,60):null), lidColor:window.__SKIN||null }); })()`;
  const s = await win.webContents.executeJavaScript(js);
  mlog("AUDIT " + s); return s;
});
// ---------- 老师可见性：hidden(托盘)/mini(小头像)/full(半身) ----------
// cfg.mode: 'hidden' | 'mini' | 'full'，默认 hidden —— 启动绝不挡屏幕
let mode = cfg.mode || 'hidden';
cfg.overlayVideo = 2;   // 视频版 overlay（bounds 迁移标记）
let savedBounds = cfg.savedBounds || null;
function applyMode(m, persist = true) {
  const prev = mode; mode = m;
  if (persist) { cfg.mode = m; saveCfg(cfg); }
  if (!win || win.isDestroyed()) return;
  if (prev === 'full' && m !== 'full') { savedBounds = cfg.savedBounds || win.getBounds(); cfg.savedBounds = savedBounds; saveCfg(cfg); }
  const sc = Math.min(1.5, Math.max(0.6, cfg.scale || 1));
  const wa = workArea();
  if (m === 'hidden') { win.hide(); }
  else if (m === 'mini') {
    const mw = Math.round(120 * sc), mh = Math.round(152 * sc);
    win.setBounds({ x: wa.x + wa.width - mw - 10, y: wa.y + wa.height - mh - 10, width: mw, height: mh });
    win.showInactive();
  } else {
    // full：1002直播定稿——有用户拖过的 bounds 就原样恢复(记忆位置,不再弹回右下角);
    // 没有才按 704:896 竖版默认贴右下角
    if (cfg.bounds && cfg.bounds.width >= 200) {
      win.setBounds(cfg.bounds); win.showInactive();
    } else {
      let h = Math.min(wa.height - 16, Math.round(wa.height * 0.76 * sc));
      let w = Math.round(h * 704 / 896);
      if (w > wa.width * 0.5) { w = Math.round(wa.width * 0.5); h = Math.round(w * 896 / 704); }
      const back = { x: wa.x + wa.width - w - 10, y: wa.y + wa.height - h - 10, width: w, height: h };
      win.setBounds(back); win.showInactive();
    }
  }
  win.webContents.send('teacher:mode', m);
    // 1019fix: 设置页挂着「挂眼」=> 老师一出屏就自动开抓拍窗并把采样跑起来(设置即生效, 不再靠手点👁)。
    // 但"自动"要有分寸: (a) 每次启动最多自动弹一次; (b) 你自己关掉它就不再打扰;
    //   (c) 在设置里重新打开「挂眼」= 重新授权自动弹; (d) 托盘可随时手动开关。
    if (m === 'full') {
      try {
        const en = remindCfg().eye_enabled === true;
        if (cfg._eyeSeen === false && en) cfg.eyeClosedByUser = false;   // 重新打开=重新授权
        cfg._eyeSeen = en; saveCfg(cfg);
        if (en && !eyeAutoOpened && !cfg.eyeClosedByUser && (!camWin || camWin.isDestroyed())) {
          eyeAutoOpened = true;
          setTimeout(() => { try { openCamWindow(true); } catch (e) {} }, 800);
        }
      } catch (e) {}
    }
  }
ipcMain.on('teacher:dnd', () => {
  if (mode === 'full') applyMode('mini');
  else if (mode === 'mini') applyMode('full');
  else applyMode('mini');  // hidden→双击不到这里；兜底mini
});
ipcMain.on('teacher:summon', () => applyMode('full'));

// ---------- 定时提醒引擎(1003, 搬自设置页旧"测对话"按钮位) ----------
// 主进程权威轮询(20s): overlay 窗可隐藏、渲染进程定时器不可靠, 这里才稳。
// 时长真相直接读 sessions.json 的 started(权威时钟), Electron 中途重启也不丢已上时长。
let _remindCfgCache = { m: 0, v: null };
function remindCfg() {
  const p = path.join(__dirname, 'backend', 'config', 'app.json');
  try {
    const mt = fs.statSync(p).mtimeMs;
    if (mt !== _remindCfgCache.m) {
      const c = JSON.parse(fs.readFileSync(p, 'utf8'));
      _remindCfgCache = { m: mt, v: c };
    }
  } catch (e) { mlog('remind cfg read fail: ' + e.message); }
  return _remindCfgCache.v || {};
}
function remindActiveSession() {
  // 复刻 evaluation._gactive: 未 ended 且 started 最大者 → 返回 {sid, started} 或 null
  try {
    const d = JSON.parse(fs.readFileSync(path.join(__dirname, 'backend', 'config', 'sessions.json'), 'utf8'));
    let k = null, best = -1;
    for (const [sid, v] of Object.entries(d)) {
      if (!v.ended && (v.started || 0) > best) { k = sid; best = v.started || 0; }
    }
    return k ? { sid: k, started: best } : null;
  } catch { return null; }
}
let _remindState = { endFiredSid: '', startFiredKey: '' };
function remindTick() {
  try {
    const c = remindCfg();
    if (!c || c.remind_enabled !== true) return;                 // 默认关
    // 规则1: 上课超过 N 分钟 → 该下课休息了(每会话只响一次, 用 sid 记档)
    const lim = Number(c.remind_class_limit_min) || 0;
    if (lim >= 5) {
      const s = remindActiveSession();
      if (s && s.sid !== _remindState.endFiredSid && Date.now() - s.started * 1000 >= lim * 60000) {
        _remindState.endFiredSid = s.sid;
        fireRemind('class_end');
      }
    }
    // 规则2: 到点 HH:MM → 该上课了(每天每时刻只响一次)
    const at = String(c.remind_class_start_at || '').trim();
    if (/^\d{1,2}:\d{2}$/.test(at)) {
      const d = new Date();
      const key = d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate() + ' ' + at;
      const [H, M] = at.split(':').map(Number);
      if (_remindState.startFiredKey !== key && d.getHours() === H && d.getMinutes() === M) {
        _remindState.startFiredKey = key;
        fireRemind('class_start');
      }
    }
  } catch (e) { mlog('remind tick err: ' + (e && e.message)); }
}
function fireRemind(kind) {
  const texts = {
    class_end: ['Class is over — time for a little break.', '这堂课结束了，请稍事休息。'],
    class_start: ['Time for class! I have been waiting for you.', '该上课啦，我等你很久了。']
  };
  const [en, zh] = texts[kind] || texts.class_end;
  const title = kind === 'class_start' ? '上课提醒' : '下课提醒';
  mlog('REMIND fire ' + kind);
  const deliver = (url) => {
    try {
      if (kind === 'class_start' && mode === 'hidden') applyMode('full');   // 到点上课先把老师叫出来
      if (win && !win.isDestroyed()) {
        try { win.showInactive(); } catch {}
        // 数字人开口: text 上气泡, url=她的真声TTS(preload say() 拼 API 前缀)
        win.webContents.send('teacher:say', en + ' ' + zh, url || null);
      }
      try { if (Notification.isSupported()) new Notification({ title, body: en + '\n' + zh, silent: false }).show(); } catch {}
      if (tray) { try { tray.displayBalloon({ title, content: en + ' ' + zh }); } catch {} }
    } catch (e) { mlog('remind deliver err: ' + (e && e.message)); }
  };
  // TTS 拿她真声; 失败退回纯文字泡, 提醒绝不哑
  fetch('http://127.0.0.1:8765/tts?text=' + encodeURIComponent(en + ' ' + zh))
    .then((r) => r.ok ? r.json() : null).then((j) => deliver(j && j.url))
    .catch(() => deliver(null));
}
setInterval(remindTick, 20000);
ipcMain.on('teacher:open-calib', () => {
  // 校准五官：确保老师窗口可见 -> 重载 -> teacher.js 检测到 .calib 自动进拖框模式
  if (!win || win.isDestroyed()) return;
  if (mode === 'hidden') applyMode('full');
  win.webContents.reload();
});
ipcMain.handle('teacher:mode', () => mode);
// 1003(卡通形象): 设置页保存后请求重载老师窗口(形象热切换的最硬保险; overlay自身也有/config轮询)
ipcMain.on('teacher:reload-win', () => { if (win && !win.isDestroyed()) win.webContents.reload(); });
// 1002 学生复习导出: 存盘对话框, 默认落"文档"目录
ipcMain.handle('teacher:save-text', async (ev, { content, filename }) => {
  const r = await dialog.showSaveDialog(win || settings, {
    title: '导出会话笔记',
    defaultPath: path.join(app.getPath('documents'), filename || 'aiteacher-review-notes.md'),
    filters: [{ name: 'Markdown 笔记', extensions: ['md'] }, { name: '所有文件', extensions: ['*'] }],
  });
  if (r.canceled || !r.filePath) return { saved: false };
  try { fs.writeFileSync(r.filePath, content, 'utf-8'); return { saved: true, path: r.filePath }; }
  catch (e) { return { saved: false, error: String(e).slice(0, 120) }; }
});
// 半身像以 dataURL 交给渲染层：data: 图不污染 canvas，肤色采样必成
ipcMain.handle('teacher:bust-data', () => {
  try { return "data:image/png;base64," + fs.readFileSync(path.join(__dirname, 'assets', 'teacher_bust.png')).toString('base64'); }
  catch { return null; }
});
ipcMain.on('teacher:drag', (ev, { dx, dy }) => {
  if (win && !win.isDestroyed()) { const [x, y] = win.getPosition(); win.setPosition(Math.round(x + dx), Math.round(y + dy)); }
});
// 1002: 拖动只移动不变大小; 抬起后校正跨屏DPI引起的大小漂移, 再落盘记住位置
ipcMain.on('teacher:drag-end', (ev, { w, h } = {}) => {
  if (!win || win.isDestroyed()) return;
  const b = win.getBounds();
  if (w && h && (b.width !== w || b.height !== h)) {
    b.width = w; b.height = h; win.setBounds(b);   // 身体拖动=尺寸必归原
  }
  cfg.bounds = win.getBounds(); saveCfg(cfg);
});
// 1002直播定稿: 边缘按住拖=调整边框改大小(物理像素经screen精确换算DIP)
ipcMain.on('teacher:edge-resize', (ev, { edge, dx, dy }) => {
  if (!win || win.isDestroyed()) return;
  const p0 = screen.screenToDipPoint({ x: 0, y: 0 });
  const p1 = screen.screenToDipPoint({ x: dx, y: dy });
  const ddx = Math.round(p1.x - p0.x), ddy = Math.round(p1.y - p0.y);
  const [x, y] = win.getPosition(); const [w, h] = win.getSize();
  const MIN = 240;
  let nx = x, ny = y, nw = w, nh = h;
  if (edge.includes('e')) nw = Math.max(MIN, w + ddx);
  if (edge.includes('s')) nh = Math.max(MIN, h + ddy);
  if (edge.includes('w')) { nw = Math.max(MIN, w - ddx); nx = x + (w - nw); }
  if (edge.includes('n')) { nh = Math.max(MIN, h - ddy); ny = y + (h - nh); }
  win.setBounds({ x: nx, y: ny, width: nw, height: nh });
});
ipcMain.on('teacher:quit', () => { quitting = true; app.quit(); });
ipcMain.on('open-settings', () => openSettings());
ipcMain.handle('teacher:cfg', () => cfg);
ipcMain.on('teacher:set-model', (e, m) => { cfg.model = m; saveCfg(cfg); });
ipcMain.on('teacher:set-scale', (e, s) => { cfg.scale = s; saveCfg(cfg); });
// 气泡文字宽度（Shift+滚轮调节，持久化 cfg.bubbleW，20~100%）
ipcMain.on('teacher:set-bubblew', (e, w) => {
  cfg.bubbleW = Math.min(100, Math.max(20, Math.round(w)));
  saveCfg(cfg);
  mlog('bubbleW=' + cfg.bubbleW);
});
// 滚轮/双击缩放: 直接改窗口 bounds —— 整个框等比放大缩小, 画面跟着放大, 绝不裁切内容
// 1002教训: 别再叠加 CSS transform(canvas 外层 scale), 那只会"放大局部"
function zoomWindow(factor) {
  if (!win || win.isDestroyed()) return;
  const b = win.getBounds();
  if (!cfg._zoomBase && mode === 'full') {
    cfg._zoomBase = { x: b.x, y: b.y, width: b.width, height: b.height }; saveCfg(cfg);
  }
  const wa = screen.getDisplayMatching(b).workArea;
  let w = Math.round(b.width * factor), h = Math.round(b.height * factor);
  const maxW = Math.round(wa.width * 0.92), maxH = Math.round(wa.height * 0.96);
  if (w < 160) { h = Math.round(h * 160 / w); w = 160; }
  if (h < 200) { w = Math.round(w * 200 / h); h = 200; }
  if (w > maxW) { h = Math.round(h * maxW / w); w = maxW; }
  if (h > maxH) { w = Math.round(w * maxH / h); h = maxH; }
  const cx = b.x + b.width / 2, cy = b.y + b.height / 2;
  let x = Math.round(cx - w / 2), y = Math.round(cy - h / 2);
  x = Math.max(wa.x, Math.min(x, wa.x + wa.width - w));
  y = Math.max(wa.y, Math.min(y, wa.y + wa.height - h));
  win.setBounds({ x, y, width: w, height: h });
  cfg.bounds = { x, y, width: w, height: h };
  cfg.scale = Math.round(Math.min(3, Math.max(0.4, (cfg.scale || 1) * factor)) * 100) / 100;
  saveCfg(cfg);
  const base = (cfg._zoomBase && cfg._zoomBase.width) || b.width;
  try { win.webContents.send('teacher:zoom-pct', Math.round(w / base * 100)); } catch (e) {}
}
let zoomPend = 1, zoomTimer = null;   // 滚轮连发只算一个乘积, 40ms 尾沿应用一次
ipcMain.on('teacher:zoom', (e, factor) => {
  if (!(factor > 0.5 && factor < 2)) return;
  zoomPend *= factor;
  if (zoomTimer) return;
  zoomTimer = setTimeout(() => { zoomTimer = null; const f = zoomPend; zoomPend = 1; zoomWindow(f); }, 40);
});
ipcMain.on('teacher:zoom-reset', () => {
  if (!win || win.isDestroyed()) return;
  const b = cfg._zoomBase;
  cfg.scale = 1; saveCfg(cfg);
  if (b) { win.setBounds(b); cfg.bounds = b; saveCfg(cfg); } else applyMode(mode, false);
  try { win.webContents.send('teacher:zoom-pct', 100); } catch (e) {}
});

// 1019 复制到剪贴板: 走主进程 clipboard, 不依赖窗口焦点(execCommand 在未聚焦时会静默失败)
ipcMain.handle('teacher:copy', (e, text) => {
  try { clipboard.writeText(String(text == null ? '' : text)); return true; }
  catch (err) { console.error('clipboard fail', err); return false; }
});

ipcMain.handle('teacher:backend-up', () => !!backend);

// ---------- 设置窗口 ----------
function openSettings() {
  if (settings && !settings.isDestroyed()) {
    if (!settings.isVisible()) settings.show();  // 隐藏态 focus 无效，必须 show 唤回
    settings.focus();
    return;
  }
  settings = new BrowserWindow({
    width: 520, height: 640, title: '桌面老师 · 设置',
    alwaysOnTop: true,  // 悬浮窗置顶，设置窗不能被盖住
    parent: win && !win.isDestroyed() ? win : undefined,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
  });
  settings.setMenu(null);
  settings.loadFile(path.join(__dirname, 'renderer', 'settings.html'));
}

// ---------- 直播效果窗口（与直播间同一条 WebRTC 链路：真人口型+克隆声+实时字幕） ----------
// ---------- 1012v2 独立抓拍窗（📷 拍照问老师）----------
// 需求: 旧📷弹窗被锁死在老师小窗中央、挪不出去还挡字幕。改为独立 frameless 置顶窗:
//       标题栏可系统级自由拖动 + 拖边放大 + 多摄像头切换 + 快门释放设备, 照片经主进程中转回老师窗发送。
let camWin = null;
let eyeAutoOpened = false;      // 本次启动是否已自动弹过相机窗(有分寸: 每次启动最多一次)
function openCamWindow(silent) {
  if (camWin && !camWin.isDestroyed()) { camWin.show(); camWin.focus(); return; }
  const wa = workArea();
  const cw = Math.min(440, Math.round(wa.width * 0.34));
  const ch = Math.round(cw * 0.72) + 128;
  camWin = new BrowserWindow({
    width: cw, height: ch,
    x: wa.x + Math.round(wa.width * 0.16), y: wa.y + Math.round(wa.height * 0.14),
    title: '📷 拍照问老师', backgroundColor: '#06080d', show: false,
    frame: false, transparent: false, resizable: true, alwaysOnTop: true, skipTaskbar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload-cam.js'),
      contextIsolation: true, nodeIntegration: false, backgroundThrottling: false,
      autoplayPolicy: 'no-user-gesture-required',
    },
  });
  camWin.setMenu(null);
  camWin.setAlwaysOnTop(true, 'screen-saver');
  camWin.once('ready-to-show', () => { if (camWin && !camWin.isDestroyed()) { if (silent) camWin.showInactive(); else { camWin.show(); camWin.focus(); } } });
  camWin.on('closed', () => {
    camWin = null;
    if (!quitting) { try { cfg.eyeClosedByUser = true; saveCfg(cfg); } catch (e) {} }   // 你主动关的 -> 别再自动弹
  });
  // 摄像头权限: 抓拍窗独立放行 media（与主窗同策略）
  camWin.webContents.session.setPermissionRequestHandler((wc, perm, cb) => cb(perm === 'media' || perm === 'audioCapture'));
  camWin.webContents.session.setPermissionCheckHandler((wc, perm) => perm === 'media' || perm === 'audioCapture');
  camWin.loadFile(path.join(__dirname, 'renderer', 'camwin.html'));
  mlog('cam window open');
}
function closeCamWindow() { if (camWin && !camWin.isDestroyed()) camWin.close(); }
ipcMain.on('cam:open', () => openCamWindow());
ipcMain.on('cam:close', () => closeCamWindow());
ipcMain.on('cam:result', (ev, d) => {
  // 抓拍窗回传 {q,image} → 转给老师主窗走 /human → 关闭抓拍窗（照片不落盘, 只在内存传递）
  if (win && !win.isDestroyed()) win.webContents.send('cam:result', d);
  closeCamWindow();
});

function openLiveWindow() {
  if (liveWin && !liveWin.isDestroyed()) { liveWin.focus(); return; }
  const wa = workArea();
  const lwW = Math.min(Math.round(wa.width * 0.30), Math.round((wa.height - 240) * 704 / 896));
  const lwH = Math.round(lwW * 896 / 704) + 170;
  liveWin = new BrowserWindow({
    width: lwW, height: lwH,
    x: wa.x + wa.width - lwW - 24, y: wa.y + 16,
    title: '桌面老师 · ' + teacherName(), backgroundColor: '#06080d', show: false, autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false,
      autoplayPolicy: 'no-user-gesture-required' },
  });
  liveWin.setMenu(null);
  liveWin.once('ready-to-show', () => liveWin.show());
  liveWin.on('closed', () => { liveWin = null; });
  liveWin.webContents.on('did-fail-load', (e, code) => {
    mlog('LIVE FAILLOAD ' + code + '（8011 未就绪？看门狗会自动拉起）');
    if (code === -102 && !quitting) setTimeout(() => liveWin && liveWin.loadURL('http://127.0.0.1:8011/desktop.html'), 5000);
  });
  liveWin.loadURL('http://127.0.0.1:8011/desktop.html');
  mlog('live window open');
}

// ---------- 托盘 ----------
function buildTrayMenu() {
  return Menu.buildFromTemplate([
    { label: '── 服务状态 ──────────', enabled: false },
    { label: `${dot('backend')} 对话后端 (8765)`, enabled: false },
    { label: `${dot('tts')} 克隆声音服务 (18091)`, enabled: false },
    { label: `${dot('engine')} 数字人引擎 (8011)`, enabled: false },
    { label: `${dot('watchdog')} 看门狗 (18999)`, enabled: false },
    { type: 'separator' },
    { label: '▶ 启动/修复 全部服务', click: () => { startBackend(); startWatchdog().then(probeSvcs); } },
    { label: '⏸ 停止 引擎+声音服务', click: async () => { stopStack(); await sleep(1500); probeSvcs(); } },
    { label: '🔁 重启看门狗', click: async () => { stopStack(); await sleep(1500); startWatchdog().then(probeSvcs); } },
    { type: 'separator' },
    { label: '🎬 打开真人对话窗口', click: () => openLiveWindow() },
    { label: '① 老师出现在桌面（真人口型·推荐）', click: () => openLiveWindow() },
    { label: '② 缩小到角落小头像', click: () => applyMode('mini', false) },
    { label: '③ 老师出现在桌面（视频版）', click: () => applyMode('full', false) },
    { label: '④ 隐身收进托盘', click: () => applyMode('hidden', false) },
    { type: 'separator' },
    { label: `${camWin && !camWin.isDestroyed() ? '📷 关闭相机窗（挂眼）' : '📷 打开相机窗（挂眼）'}`, click: () => {
      if (camWin && !camWin.isDestroyed()) closeCamWindow();
      else { try { cfg.eyeClosedByUser = false; saveCfg(cfg); } catch (e) {} eyeAutoOpened = true; openCamWindow(); }
    } },
    { label: '校准五官位置…', click: async () => {
        try { await fetch('http://127.0.0.1:8765/calibflag', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on: true }) }); } catch {}
        applyMode('full'); win.webContents.reload();
      } },
    { label: '设置…', click: () => openSettings() },
    { label: '打开聊天网页', click: () => shell.openExternal('http://127.0.0.1:8765/docs') },
    { type: 'separator' },
    { label: '开机自启', type: 'checkbox', checked: app.isLaunchAtLoginSupport ? cfg.autostart : false,
      click: (item) => { cfg.autostart = item.checked; saveCfg(cfg); try { app.setLoginItemSettings({ openAtLogin: item.checked }); } catch {} } },
    // 1018(Owen: “打开时一直提示连接失败”): 真凶就是这行 stopStack() 带起的
    { label: '退出界面（引擎常驻 · 下次秒开）', click: () => { quitting = true; stopBackend(); app.quit(); } },
    { label: '退出并停止全部服务（立刻释放约 13G 显存）', click: () => { quitting = true; stopStack(); stopBackend(); app.quit(); } },
  ]);
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
function refreshTrayMenu() { if (tray) { try { tray.setContextMenu(buildTrayMenu()); } catch {} } }
function createTray() {
  const ico = path.join(__dirname, 'assets', 'tray.png');
  tray = new Tray(fs.existsSync(ico) ? nativeImage.createFromPath(ico) : nativeImage.createEmpty());
  refreshTrayMenu();
  tray.on('double-click', () => applyMode(mode === 'full' ? 'hidden' : 'full'));
  probeSvcs();
  setInterval(probeSvcs, 5000);
}

// ---------- 单实例（固定 userData，锁可靠；二次启动=唤醒已有实例） ----------
app.setPath('userData', path.join(__dirname, 'config', 'userdata'));
if (!app.requestSingleInstanceLock()) {
  mlog("已有实例在跑，本次启动=唤醒");
  app.quit();
  process.exit(0);
}
app.on('second-instance', (e, argv) => {
  // 再次双击启动器 = 唤起真人窗口（老静态图仅作降级选项，不再是默认）
  openLiveWindow();
});

const UI_HB_FILE = path.join(LT_BASE, '.ui_alive_' + process.pid);
function uiHeartbeat() { try { fs.writeFileSync(UI_HB_FILE, String(Date.now())); } catch {} }
// ---- 1019setup 首次配置向导: 新机装上就引导配云端 Token(只在没配时弹) ----
let setupWin = null;
function openSetupWizard() {
  if (setupWin && !setupWin.isDestroyed()) { setupWin.show(); setupWin.focus(); return; }
  setupWin = new BrowserWindow({
    width: 600, height: 660, resizable: false, minimizable: false, maximizable: false,
    title: 'SpeakTosi 首次配置 · 云端 Token', backgroundColor: '#12151c', autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, nodeIntegration: false },
  });
  try { setupWin.setMenu(null); } catch {}
  setupWin.loadFile(path.join(__dirname, 'renderer', 'setup_wizard.html'));
  setupWin.on('close', () => { if (!cfg.setupDismissed) { cfg.setupDismissed = 1; saveCfg(cfg); } });
  setupWin.on('closed', () => { setupWin = null; });
  mlog('setupWizard opened');
}
async function maybeSetupWizard() {
  for (let i = 0; i < 24; i++) {                      // 最多等 ~36s: 后端冷启可能慢
    try {
      const s = await (await fetch('http://127.0.0.1:8765/setup/status')).json();
      if (s && s.configured) { mlog('setup: token 已配置, 不弹向导'); return; }
      if (s && s.configured === false) break;
    } catch (e) {}
    await new Promise(r => setTimeout(r, 1500));
  }
  if (cfg.setupDismissed) { mlog('setup: 用户此前选过「稍后」, 本次不弹'); return; }
  openSetupWizard();
}

// 1020: 外链(设置页/文档里的官网链接)一律交系统浏览器, 不在应用内开空白窗口
app.on('web-contents-created', (_e, wc) => {
  try {
    wc.setWindowOpenHandler(({ url }) => {
      if (/^https?:\/\//i.test(url)) { shell.openExternal(url); return { action: 'deny' }; }
      return { action: 'allow' };
    });
  } catch {}
});

app.whenReady().then(async () => {
  mlog("app ready");
  uiHeartbeat(); setInterval(uiHeartbeat, 30000);
  try { app.setLoginItemSettings({ openAtLogin: !!cfg.autostart }); } catch {}
  startBackend();
  // 1002桌面bat整合: 看门狗不再需要桌面bat用 cmd /c 起(黑框根因)，本程序直接用 pythonw 无窗拉起
  startWatchdog().then(r => mlog('watchdog status: ' + r));
  createWindow();
  createTray();
  maybeSetupWizard();   // 1019setup: 未配云端 Token 时弹首次配置向导
  applyMode(mode, false); // 默认 hidden：启动绝不出屏挡路
  if (process.argv.some(a => /--live/.test(a))) openLiveWindow();
});
app.on('window-all-closed', () => { /* 托盘常驻，不退出 */ });
app.on('before-quit', () => {
  quitting = true; stopBackend();
    try { fs.unlinkSync(UI_HB_FILE); } catch {}   // 1018 界面已关 -> 看门狗开始计空闲
  });
