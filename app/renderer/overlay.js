/* 桌面老师 · 视频版 overlay v2（三点升级：赛博朋克麦克风@右上 / 底部3行影视字幕 / 会话记录面板+词汇高亮）
   视频源：LiveTalking WebRTC（avatar=aiteacher_demo，源视频全程闭嘴 → 静音天然聆听态）
   交互：鼠标穿透命中区、拖拽移动、滚轮缩放、双击隐藏、按住说话(STT)、单击出输入条 */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  // 1006双引擎: 1020mini: 只剩 musetalk 真人系→8011(卡通引擎已裁); 按 /avatars 注册表 engine 字段路由
  const LT_DEFAULT = 'http://127.0.0.1:8011';
  let LT = LT_DEFAULT;
  const API = (window.teacher && teacher.apiBase) || 'http://127.0.0.1:8765';
  // 1003(卡通形象): 原硬编码 AVATAR='aiteacher_demo' → 改从设置页(/config)读取,
  // 切换形象=换视频素材包 + 换音色preset(refaudio), 引擎 offer 原生支持双参数。
  let AVATAR = 'aiteacher_demo';
  let AVATAR_VOICE = 'default';

  // 1002fix: 老师名字动态化——此前界面硬编码"SpeakTosi", 设置页改名单像是"没保存"
  let teacherName = 'SpeakTosi';
  // 1004(四老师): 见面语——连麦成功后由老师用本人音色(引擎echo链路)原句朗读一次。
  let GREET = '';
  async function pullPersona() {
    try {
      const p = await (await fetch(API + '/personality')).json();
      if (p && p.name && p.name.trim()) {
        teacherName = p.name.trim();
        $('txt').placeholder = '问 ' + teacherName + ' 老师…（Enter 发送）';
        document.title = '桌面老师 · ' + teacherName;
        const he = $('histEmpty');
        if (he) he.innerHTML = '还没有对话记录<br>跟 ' + teacherName + ' 老师说点什么吧';
      }
      GREET = (p && p.greeting || '').trim();
    } catch (e) { /* backend没起就用默认名 */ }
  }
  pullPersona();

  const wrap = $('wrap'), vid = $('vid'), aud = $('aud'), subs = $('subs'),
        micBtn = $('mic'), bar = $('bar'), tinp = $('txt'), tip = $('tip'),
        topBtns = $('topBtns'), histBtn = $('histBtn'), histPanel = $('histPanel'),
        histList = $('histList'), intrBtn = $('intrBtn'), setBtn = $('setBtn');
  // 1019 隐藏/唤回: 脸(opacity) + 命中区 一起切(隐藏态整框仍可点=双击唤回)
  function setFaceHidden(hid) {
    wrap.style.opacity = hid ? '0' : '1';
    if (hid) {
      bar.style.display = 'none';
      subClearFlow(); subClearMine();
      clearTimeout(showTip._t); tip.style.opacity = '0';
    }
    sendHitbox();
  }
  // 1006v3 语音确认发送: confirm 模式=松手后出确认条(✓发送/✗取消, 文本可改); auto=旧行为松手即发
  const confirmBar = $('confirmBar'), cfmTxt = $('cfmTxt'), cfmSend = $('cfmSend'), cfmCancel = $('cfmCancel');
  let VOICE_CONFIRM = false;          // 从 /config voice_confirm_mode==='confirm' 同步(2.5s轮询热生效)
  let cfmPayload = null;              // 待确认的 {text, wav} —— wav 用于发送后补传评测留存

  // ---------- 1004 B路线: WebGL chroma-key 真透明渲染 ----------
  // 引擎输出纯绿#00FF00背景(重打包full_imgs + 绿幕idle), shader按到key色的距离软阈值出alpha,
  // despill压掉边缘绿晕; WebGL不可用时自动回退旧行为(video直显)。
  // 1006v2: key色逐老师可配(默认绿)。备用路: 某老师素材若用品红#FF00FF底, 在此登记 [1,0,1]。
  // (Marcus 绿幕参考图曾连撞审核403, 最终用白底参考+提示词绿幕过审, 全员统一纯绿)
  // 1010i: coach_kate 的H3源绿幕偏青(RGB 0.318,1.0,0.338≈#51FF56), 与纯绿key距离0.519>0.55
  //   导致背景alpha=0.64=半透明绿雾盖桌面(Owen实机看到"绿边")。按1006v2正路登记实测key色:
  //   背景alpha归0、脸部0.979不动(逐帧实测n=100角点std<0.015, 源色均匀)。帧像素零改动。
  const CK_KEY_OF = { coach_kate: [0.318, 1.0, 0.338] };
  let ckKey = [0.0, 1.0, 0.0];
  const ckUpdateKey = () => { ckKey = CK_KEY_OF[AVATAR] || [0.0, 1.0, 0.0]; };
  // 1010j: key2 = 与 ckKey 配对的另一个绿相。待机idle与说话full_imgs背景色可能不同,
  // 双key取最近距离才能两条路径全覆盖。纯绿是全员说话背景, 故 key2 恒为纯绿兜底;
  // 若老师无登记(idle也是纯绿), 两key相同 min() 退化为原行为, 零风险。
  const ckKey2 = () => [0.0, 1.0, 0.0];
  const ck = $('ck');
  let gl = null, glProg = null, glTex = null, glRAF = 0, glKeyLoc = null, glKey2Loc = null;
  (function initGL() {
    try {
      gl = ck.getContext('webgl', { alpha: true, premultipliedAlpha: false, antialias: false, preserveDrawingBuffer: true });
      if (!gl) throw new Error('no webgl');
      const VS = 'attribute vec2 p; varying vec2 uv; void main(){ uv = vec2(p.x*0.5+0.5, 0.5-p.y*0.5); gl_Position = vec4(p,0.,1.); }';
      const FS = [
        'precision mediump float;',
        'varying vec2 uv;',
        'uniform sampler2D src;',
        'uniform vec3 key;',
        'uniform vec3 key2;',
        'void main() {',
        '  vec4 c = texture2D(src, uv);',
        // 1010j: 双key取最近距离 —— 待机idle帧与说话full_imgs背景绿相可能不同
        // (Kate: idle偏青(0.31,1,0.33), 说话纯绿(0,1,0)), 单key只能配一半
        '  float d = min(distance(c.rgb, key), distance(c.rgb, key2));',
        '  float a = clamp((d - 0.30) / 0.25, 0.0, 1.0);',
        // despill: 半透明边缘把 key 主导通道压向其余通道, 消色晕(绿/品红两路)
        '  vec3 col = c.rgb;',
        '  if (key.g > 0.5) {',
        '    col.g = min(c.g, mix(c.g, (c.r + c.b) * 0.5 + 0.08, 1.0 - a));',
        '  } else {',
        '    col.r = min(c.r, mix(c.r, c.g + 0.08, 1.0 - a));',
        '    col.b = min(c.b, mix(c.b, c.g + 0.08, 1.0 - a));',
        '  }',
        '  gl_FragColor = vec4(col, a);',
        '}'
      ].join('\n');
      const mk = (t, s) => { const sh = gl.createShader(t); gl.shaderSource(sh, s); gl.compileShader(sh);
        if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh)); return sh; };
      glProg = gl.createProgram();
      gl.attachShader(glProg, mk(gl.VERTEX_SHADER, VS));
      gl.attachShader(glProg, mk(gl.FRAGMENT_SHADER, FS));
      gl.linkProgram(glProg);
      if (!gl.getProgramParameter(glProg, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(glProg));
      gl.useProgram(glProg);
      const buf = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, buf);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
      const loc = gl.getAttribLocation(glProg, 'p');
      gl.enableVertexAttribArray(loc);
      gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
      glTex = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, glTex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
      glKeyLoc = gl.getUniformLocation(glProg, 'key');
      glKey2Loc = gl.getUniformLocation(glProg, 'key2');
      const draw = () => {
        glRAF = requestAnimationFrame(draw);
        if (vid.readyState < 2 || !vid.videoWidth) return;
        if (ck.width !== vid.videoWidth || ck.height !== vid.videoHeight) {
          ck.width = vid.videoWidth; ck.height = vid.videoHeight;
          gl.viewport(0, 0, ck.width, ck.height);
        }
        ckUpdateKey();   // 1006v2: 每帧同步当前老师的 key 色(切换老师即时生效)
        gl.uniform3f(glKeyLoc, ckKey[0], ckKey[1], ckKey[2]);
        const k2 = ckKey2();   // 1010j: 第二 key(纯绿兜底), 与登记 key 取最近距离
        gl.uniform3f(glKey2Loc, k2[0], k2[1], k2[2]);
        gl.bindTexture(gl.TEXTURE_2D, glTex);
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, vid);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      };
      draw();
      console.log('[ck] WebGL chroma-key ready');
    } catch (e) {
      gl = null;
      ck.style.display = 'none';
      vid.style.cssText = 'display:block;width:100%;height:100%;object-fit:cover;pointer-events:none;background:#000;';
      console.warn('[ck] WebGL 失败, 回退直显:', e.message);
    }
  })();

  // ---------- 1019 整体缩放: 由主进程改窗口 bounds(main.js zoomWindow), 这里只负责百分比提示 ----------
  // 铁律: 不再对 canvas 外层做 CSS transform —— 那只会"放大局部"; 整体缩放=窗口变大, 画面等比跟随。
  (function zoomHint(){
    let el = null;
    if (!(window.teacher && teacher.onZoomPct)) return;
    teacher.onZoomPct((p) => {
      try {
        if (!el) {
          el = document.createElement('div');
          el.id = 'zoomToast';
          el.style.cssText = 'position:absolute;right:14px;top:14px;z-index:99;padding:4px 10px;border-radius:8px;'
            + 'background:rgba(0,0,0,.6);color:#7ce7f5;font:600 12px/1.4 "Segoe UI","Microsoft YaHei",sans-serif;'
            + 'pointer-events:none;transition:opacity .25s;opacity:0;';
          document.body.appendChild(el);
        }
        el.textContent = '🔍 老师 ' + p + '%';
        el.style.opacity = 1;
        clearTimeout(el._t);
        el._t = setTimeout(() => { el.style.opacity = 0; }, 900);
      } catch (e) {}
    });
  })();

    // ---------- 状态提示 ----------
    function setStatus(on, t, cls) { $('status').className = cls || (on ? 'on' : ''); $('stxt').textContent = t; }
  function showTip(msg, pin) {
    // 1019: 取消「双击这里可恢复…」兜底文案(多此一举、影响美观), 只有真提示才显示
    if (!msg) return;
    tip.textContent = msg;
    tip.style.opacity = '1';
    clearTimeout(showTip._t);
    // 1002修复: 隐藏后提示必须常驻(pin)，3.5秒自动淡没=唤不回的真凶之一
    if (!pin) showTip._t = setTimeout(() => { tip.style.opacity = '0'; }, 3500);
  }

  // ---------- 鼠标穿透命中区 ----------
  function elRect(el, pad) {
    pad = pad || 0;
    const b = el.getBoundingClientRect();
    return { x: b.left - pad, y: b.top - pad, w: b.width + pad * 2, h: b.height + pad * 2 };
  }
  function hitRects() {
    const W = window.innerWidth, H = window.innerHeight;
    const rects = [];
    const pw = Math.round(W * 0.74), px = Math.round((W - pw) / 2);
    // 1019: 隐藏态下整框仍是命中区 -> 双击框内任意位置即可唤回老师(旧版只留 tip 小方块, 双击唤不回)
    rects.push({ x: px, y: 0, w: pw, h: Math.round(H * 0.88) });
    if (topBtns.style.display !== 'none') rects.push(elRect(topBtns, 6));
    if (bar.style.display === 'flex') rects.push({ x: 0, y: H - 54, w: W, h: 54 });
    if (confirmBar.style.display === 'flex') rects.push(elRect(confirmBar, 4));   // 1006v3: 确认条必须可点(穿透窗命中区)
    if (histPanel.classList.contains('open')) rects.push(elRect(histPanel, 0));
    if (subFlow.style.display !== 'none' || subMine.style.display !== 'none') rects.push(elRect(subs, 6));   // 1007r3: 只在字幕实际可见时占命中区
    // 1002修复: 脸已隐藏(opacity 0)时, "双击恢复"提示区必须是可点的命中区
    if (wrap.style.opacity === '0') rects.push(elRect(tip, 14));
    // 1002直播定稿: 四边7px边缘条保持可交互 -> 边缘按住拖=改大小(身体拖动只移动)
    rects.push({ x: 0, y: 0, w: W, h: 7 });
    rects.push({ x: 0, y: H - 7, w: W, h: 7 });
    rects.push({ x: 0, y: 0, w: 7, h: H });
    rects.push({ x: W - 7, y: 0, w: 7, h: H });
    return rects;
  }
  let DND = false;
  function sendHitbox() {
    if (!window.teacher) return;
    teacher.hitbox(DND ? [{ x: 0, y: 0, w: window.innerWidth, h: window.innerHeight }] : hitRects());
  }
  setInterval(sendHitbox, 120);
  window.addEventListener('resize', sendHitbox);

  // ---------- 1007r3(Owen)字幕重构: 老师内容=单背景块流式排布(自动折行、只显示最新2-3行、尾部跟随)，
  //            不再一句一行堆叠；学生=独立气泡，即说即清；老师开口清学生气泡 ----------
  let subFlowT = null, subMineT = null;
  const subFlow = document.getElementById('subFlow');
  const subMine = document.getElementById('subMine');
  function subClearFlow() { subFlow.textContent = ''; subFlow.style.display = 'none'; clearTimeout(subFlowT); subFlowT = null; sendHitbox(); }
  function subClearMine() { subMine.textContent = ''; subMine.style.display = 'none'; clearTimeout(subMineT); subMineT = null; sendHitbox(); }
  function pushTeacherSub(turnText) {
    subClearMine();                                   // 老师开口=只显示老师内容
    subFlow.style.display = 'block';
    subFlow.textContent = turnText;
    subFlow.scrollTop = subFlow.scrollHeight;         // 尾部跟随: 超3行只显示最新内容
    clearTimeout(subFlowT);
    subFlowT = setTimeout(subClearFlow, 6000);        // 说完6s清屏不占地(完整内容在会话记录)
    sendHitbox();
  }
  function pushMineSub(text) {
    subMine.textContent = text; subMine.style.display = 'block';
    subMine.style.animation = 'none'; void subMine.offsetWidth; subMine.style.animation = '';
    clearTimeout(subMineT);
    subMineT = setTimeout(subClearMine, 8000);
    sendHitbox();
  }
  function pushSub(text, mine, opts) {                // 兼容旧调用: 系统/错误提示走学生气泡样式
    if (mine) pushMineSub(text); else pushTeacherSub(text);
  }

  // ---------- 会话记录（含 LLM 词汇高亮） ----------
  function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function highlight(text, vocab) {
    const h = esc(text);
    if (!vocab || !vocab.length) return h;
    const pats = Array.from(new Set(vocab.map((v) => esc(v).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))))
                      .filter(Boolean).sort((a, b) => b.length - a.length);
    if (!pats.length) return h;
    return h.replace(new RegExp('(' + pats.join('|') + ')', 'gi'), '<mark>$1</mark>');
  }
  // 1002fix: 钉底状态——用户在底部附近=钉底(允许自动刷新跟底)；上翻阅读=解除钉底(冻结刷新)
  let histPinned = true;
  histList.addEventListener('scroll', () => {
    histPinned = histList.scrollHeight - histList.scrollTop - histList.clientHeight < 60;
  }, { passive: true });
  // ---------- 1019 学生复习翻译: 会话记录内一键英↔中互译 ----------
  // 方向自动判别(含中文→译英, 否则→译中); 译文缓存内存+localStorage(重开面板/重启仍在);
  // 后端 /translate 走云端百炼(与对话同款 qwen3.8-flash, 关思考); 失败只提示, 不影响通话。
  const TR = new Map();        // trk -> {txt, tgt}
  const TR_SRC = new Map();    // trk -> 原文全文(渲染时回填)
  const TR_HIDE = new Set();   // trk -> 用户折叠了译文
  const trKey = (it) => String(it.ts || '') + '|' + String(it.text || '').slice(0, 120);
  const hasCJK = (s) => /[\u4e00-\u9fa5]/.test(s || '');
  try {
    const saved = JSON.parse(localStorage.getItem('tr_cache_v1') || 'null');
    if (saved && saved.arr) for (const r of saved.arr) TR.set(r[0], { txt: r[1], tgt: r[2] });
  } catch (e) { /* localStorage 不可用则只用内存缓存 */ }
  let trSaveT = null;
  function trSave() {
    if (trSaveT) return;
    trSaveT = setTimeout(() => {
      trSaveT = null;
      try {
        const arr = Array.from(TR.entries()).slice(-600).map(([k, v]) => [k, v.txt, v.tgt]);
        localStorage.setItem('tr_cache_v1', JSON.stringify({ arr: arr }));
      } catch (e) {}
    }, 800);
  }
  async function trFetch(text, tgt) {
    const r = await fetch(API + '/translate', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: text, target: tgt }) });
    const d = await r.json();
    if (!d || !d.ok) throw new Error((d && d.error) || ('http ' + r.status));
    return String(d.text || '').trim();
  }
  async function trOne(item, btn) {
    const trk = item.dataset.trk;
    const box = item.querySelector('.htr');
    if (box && TR.has(trk)) {                                     // 已有译文 → 折叠/展开
      if (TR_HIDE.has(trk)) { TR_HIDE.delete(trk); box.style.display = ''; btn.textContent = '🌐 隐'; }
      else { TR_HIDE.add(trk); box.style.display = 'none'; btn.textContent = '🌐 译'; }
      trSave();
      return;
    }
    const src = (TR_SRC.get(trk) || '').trim();
    if (!src) return;
    const tgt = hasCJK(src) ? 'en' : 'zh';
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = '🌐 …';
    try {
      const out = await trFetch(src, tgt);
      if (!out) throw new Error('空译文');
      TR.set(trk, { txt: out, tgt: tgt });
      TR_HIDE.delete(trk);
      let el = item.querySelector('.htr');
      if (!el) { el = document.createElement('div'); el.className = 'htr'; item.appendChild(el); }
      el.textContent = out; el.style.display = '';
      btn.textContent = '🌐 隐';
      trSave();
    } catch (err) { btn.textContent = old; histHint('翻译失败: ' + (err.message || err)); }
    btn.disabled = false;
  }
  histList.addEventListener('click', (e) => {                     // 事件委托: DOM每6s重建也不丢
    const btn = e.target && e.target.closest ? e.target.closest('.trbtn') : null;
    if (!btn) return;
    e.stopPropagation();
    const item = btn.closest('.hitem');
    if (item) trOne(item, btn);
  });
  async function trAll() {                                        // 只译最近20条未译的, 串行控流
    const items = Array.from(histList.querySelectorAll('.hitem'));
    let n = 0, ok = 0;
    for (const it of items.slice(-20)) {
      if (TR.has(it.dataset.trk)) continue;
      const btn = it.querySelector('.trbtn');
      if (!btn) continue;
      n++;
      histHint('翻译中… ' + ok + '/' + n);
      await trOne(it, btn);
      if (TR.has(it.dataset.trk)) ok++;
    }
    histHint(n ? ('已翻译 ' + ok + ' 条') : '最近20条都已有译文');
  }
  const histTrAllBtn = $('histTrAll');
  if (histTrAllBtn) histTrAllBtn.onclick = (e) => { e.stopPropagation(); trAll(); };

  // ---------- 1019 声音回放: 老师句=后端原声缓存, 学生句=本地/评测留存的原声 ----------
  // 老师: GET /tts?text=... 与通话同一条缓存键(md5) -> 命中即"你刚听到的原声", 不会重合成变调;
  //       缓存未命中才现合成(约1~3s, 按钮显示"…合成")。
  // 学生: 按住说话/语音轮发送时把同一段 WAV 留档(内存 Blob 立即可听 + localStorage 记后端文件名,
  //       重开面板甚至重启后仍可回放)。纯打字轮没有音频 -> 按钮显示 🔇 并给出提示。
  const STU_AUD = new Map();       // 学生原声: text(trim) -> {blob?, fn?}
  const STU_KEYS = [];
  const STU_MAX = 120;
  let playingKey = null, curAudio = null, curBtn = null;
  try {
    const saved = JSON.parse(localStorage.getItem('stu_aud_v1') || 'null') || {};
    for (const k in saved) { if (saved[k]) STU_AUD.set(k, { fn: saved[k] }); }
  } catch (e) {}
  function stuSaveFn() {
    try {
      const o = {}; let n = 0;
      for (const kv of STU_AUD) { const k = kv[0], v = kv[1]; if (v && v.fn) { o[k] = v.fn; if (++n >= STU_MAX) break; } }
      localStorage.setItem('stu_aud_v1', JSON.stringify(o));
    } catch (e) {}
  }
  function stuRemember(text, wav) {
    const k = String(text || '').trim();
    if (!k || !wav) return;
    try {
      const rec = STU_AUD.get(k) || {};
      rec.blob = new Blob([wav], { type: 'audio/wav' });
      STU_AUD.set(k, rec);
      STU_KEYS.push(k);
      while (STU_KEYS.length > STU_MAX) { const old = STU_KEYS.shift(); const r = STU_AUD.get(old); if (r) delete r.blob; }
    } catch (e) {}
  }
  function stuSetFn(text, fn) {
    const k = String(text || '').trim();
    if (!k || !fn) return;
    const rec = STU_AUD.get(k) || {};
    rec.fn = fn; STU_AUD.set(k, rec); stuSaveFn();
  }
  function auLabel(it) {
    const k = trKey(it);
    if (playingKey === k) return '⏸ 停';
    if (it.role === 'user') return STU_AUD.has(String(it.text || '').trim()) ? '🔊 原声' : '🔇 原声';
    return '🔊 回放';
  }
  function stopPlayback() {
    try { if (curAudio) { curAudio.pause(); curAudio.src = ''; } } catch (e) {}
    curAudio = null; curBtn = null; playingKey = null;
  }
  async function playUrl(url, btn, key) {
    stopPlayback();
    const a = new Audio(url);
    curAudio = a; curBtn = btn; playingKey = key;
    btn.textContent = '⏸ 停';
    a.onended = a.onerror = () => { if (curAudio === a) { stopPlayback(); btn.textContent = auLabelText(btn, key); } };
    try { await a.play(); } catch (e) { stopPlayback(); throw e; }
  }
  function auLabelText(btn, key) {
    const item = btn.closest ? btn.closest('.hitem') : null;
    const isUser = !!(item && item.classList.contains('user'));
    if (!isUser) return '🔊 回放';
    const txt = String(TR_SRC.get(key) || '').trim();
    return STU_AUD.has(txt) ? '🔊 原声' : '🔇 原声';
  }
  async function auPlayOne(item, btn) {
    const key = item.dataset.trk;
    if (playingKey === key) { stopPlayback(); btn.textContent = auLabelText(btn, key); return; }
    const isUser = item.classList.contains('user');
    if (isUser) {
      const txt = String(TR_SRC.get(key) || '').trim();
      const rec = STU_AUD.get(txt);
      if (rec && rec.blob) { try { return await playUrl(URL.createObjectURL(rec.blob), btn, key); } catch (e) {} }
      if (rec && rec.fn) {
        try { return await playUrl(API + '/eval/audio/' + encodeURIComponent(rec.fn), btn, key); }
        catch (e) { histHint('这条录音已被清理（评测缓存上限）'); return; }
      }
      histHint('这句是你打字的，没有录音 —— 只有"按住说话"那几轮能回放原声');
      return;
    }
    const src = String(TR_SRC.get(key) || '').slice(0, 800).trim();
    if (!src) return;
    const old = btn.textContent;
    btn.textContent = '…合成';
    try {
      const r = await fetch(API + '/tts?text=' + encodeURIComponent(src));
      const d = await r.json();
      if (!d || !d.ok || !d.url) throw new Error('tts failed');
      await playUrl(API + d.url, btn, key);
    } catch (e) { btn.textContent = old; histHint('回放失败（语音合成服务忙，稍后再试）'); }
  }
  histList.addEventListener('click', (e) => {
    const b = e.target && e.target.closest ? e.target.closest('.audiobtn') : null;
    if (!b) return;
    e.stopPropagation();
    const item = b.closest('.hitem');
    if (item) auPlayOne(item, b);
  });

  // 1019 复制按钮: 每条一键复制原文; 走主进程 clipboard(窗口未聚焦时 execCommand 会静默失败)
  histList.addEventListener('click', async (e) => {
    const btn = e.target && e.target.closest ? e.target.closest('.copybtn') : null;
    if (!btn) return;
    e.stopPropagation();
    const item = btn.closest('.hitem');
    const trk = item && item.dataset ? item.dataset.trk : '';
    const txt = (trk && TR_SRC.get(trk)) || (item ? item.innerText : '');
    let ok = false;
    try { ok = await teacher.copyText(txt); } catch (err) { ok = false; }
    btn.textContent = ok ? '✅ 已复制' : '⚠ 失败';
    setTimeout(() => { btn.textContent = '📋 复制'; }, 1200);
    histHint(ok ? ('已复制 ' + txt.length + ' 字符到剪贴板') : '复制失败，请手动选择文本');
  }, true);
  async function loadHistory() {
    // 1002fix: 用户上翻阅读中(scroll未钉底)时跳过刷新——避免6s定时重建DOM把阅读位置拽回底部
    if (histPanel.classList.contains('open') && !histPinned && histList.children.length) return;
    try {
      const d = await (await fetch(API + '/history?limit=300')).json();
      const items = (d && d.items) || [];
      if (!items.length) {
        histList.innerHTML = '<div id="histEmpty">还没有对话记录<br>跟 ' + teacherName + ' 老师说点什么吧</div>';
        return;
      }
      histList.innerHTML = '';
      for (const it of items) {
        const div = document.createElement('div');
        const isUser = it.role === 'user';
        div.className = 'hitem ' + (isUser ? 'user' : 'assistant');
        const who = isUser ? '🧑 我 · STUDENT' : ('👩‍🏫 ' + teacherName + ' 老师 · TEACHER');
        const vocabLine = (!isUser && it.vocab && it.vocab.length)
                  ? '<div class="vocab">📌 ' + it.vocab.map(esc).join(' · ') + '</div>' : '';
                // 1019 翻译按钮: 每条一键英↔中互译(方向自动判别), 译文持久化 localStorage
                const trk = trKey(it);
                TR_SRC.set(trk, it.text || '');
                const trg = TR.get(trk);
                const trHTML = trg ? ('<div class="htr"' + (TR_HIDE.has(trk) ? ' style="display:none"' : '') + '>' + esc(trg.txt) + '</div>') : '';
                div.dataset.trk = trk;
                div.innerHTML = '<div class="who">' + who + '<span class="ts">' + esc(it.ts || '') + '</span></div>'
                              + highlight(it.text, it.vocab) + vocabLine
                              + '<button class="copybtn" title="复制这条文本">📋 复制</button>'
                              + '<button class="trbtn" title="翻译这条（英↔中自动判别）">' + (trg ? '🌐 ' + (TR_HIDE.has(trk) ? '译' : '隐') : '🌐 译') + '</button>'
                                                            + '<button class="audiobtn" data-au="1" title="'
                                                            + (isUser ? '回放我说的这句原声（语音轮才留了音）' : '回放老师这句原声（后端缓存命中=听到的原声）') + '">'
                                                            + auLabel(it) + '</button>'
                                                            + trHTML;
                histList.appendChild(div);
      }
      histList.scrollTop = histList.scrollHeight;
      // 1002fix: 布局稳定后再钉一次底, 防innerHTML重建后scrollHeight延迟扩展导致的贴底竞态
      requestAnimationFrame(() => { if (histPinned) histList.scrollTop = histList.scrollHeight; });
    } catch (e) {
      histList.innerHTML = '<div id="histEmpty">记录加载失败（后端 8765 在线？）</div>';
    }
  }
  let histRefresh = null;
  function openHist() {
    histPanel.classList.add('open');
    evalBadgeCheck();
    histPinned = true;   // 1002fix: 重开面板=回到阅读起点，重置钉底(否则上次上翻的false会拦掉首刷)
    loadHistory();
    if (!histRefresh) histRefresh = setInterval(loadHistory, 6000);   // 词汇是后台LLM异步提取，开着就自动刷
    sendHitbox();
  }
  function closeHist() {
    histPanel.classList.remove('open');
    clearInterval(histRefresh); histRefresh = null;
    if (typeof evalTimer !== 'undefined' && evalTimer) { clearInterval(evalTimer); evalTimer = null; }
    sendHitbox();
  }
  histBtn.onclick = (e) => { e.stopPropagation(); histPanel.classList.contains('open') ? closeHist() : openHist(); };
  $('histClose').onclick = (e) => { e.stopPropagation(); closeHist(); };
  $('histClear').onclick = async (e) => {
    e.stopPropagation();
    if (!confirm('清空全部会话记录？')) return;
    try { await fetch(API + '/history', { method: 'DELETE' }); } catch (err) {}
    loadHistory();
  };
  $('histExport').onclick = async (e) => {
    e.stopPropagation();
    // 1002 学生复习导出: 优先 Electron 存盘对话框(preload暴露saveText), 否则浏览器下载兜底
    try {
      const resp = await fetch(API + '/history/export?fmt=md');
      if (!resp.ok) throw new Error('http ' + resp.status);
      const md = await resp.text();
      const fname = (teacherName || 'SpeakTosi').replace(/[\\/:*?"<>|]/g, '') + '-复习笔记.md';
      if (window.teacher && window.teacher.saveText) {
        const r = await window.teacher.saveText(md, fname);
        histHint('已导出: ' + (r && r.saved ? r.path : '未保存'));
      } else {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }));
        a.download = fname;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
        histHint('笔记已下载');
      }
    } catch (err) { histHint('导出失败: ' + err.message); }
  };
  function histHint(msg) {
    const el = document.getElementById('histHint');
    if (el) { el.textContent = msg; setTimeout(() => { el.textContent = ''; }, 4000); }
  }
  histPanel.addEventListener('mousedown', (e) => e.stopPropagation());   // 面板内操作不触发窗口拖拽

  // ---------- 会话评测 UI / evaluation panel ----------
  const tabLog = $('tabLog'), tabEval = $('tabEval'), evalWrap = $('evalWrap');
  const evalList = $('evalList'), evalDetail = $('evalDetail');
  let evalItems = [], curEvalSid = null, evalTimer = null;
  function mdLite(s) {
    // 迷你markdown渲染(报告结构固定: 标题/加粗/引用/列表/分隔线/行内码)
    const inl = (t) => esc(t)
      .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
    const out = []; let inUl = false;
    for (const raw of (s || '').split(/\r?\n/)) {
      const line = raw.trim();
      const li = /^[-*]\s+/.test(line);
      if (inUl && !li) { out.push('</ul>'); inUl = false; }
      if (/^#{1,3}\s/.test(line)) out.push('<h3>' + inl(line.replace(/^#+\s*/, '')) + '</h3>');
      else if (li) { if (!inUl) { out.push('<ul>'); inUl = true; } out.push('<li>' + inl(line.replace(/^[-*]\s+/, '')) + '</li>'); }
      else if (/^>/.test(line)) out.push('<blockquote>' + inl(line.replace(/^>\s*/, '')) + '</blockquote>');
      else if (/^(---|\*\*\*)$/.test(line)) out.push('<hr>');
      else if (line) out.push('<p>' + inl(line) + '</p>');
    }
    if (inUl) out.push('</ul>');
    return out.join('');
  }
  function switchTab(which) {
    const isEval = which === 'eval';
    tabLog.classList.toggle('on', !isEval);
    tabEval.classList.toggle('on', isEval);
    histList.style.display = isEval ? 'none' : '';
    evalWrap.style.display = isEval ? 'flex' : 'none';
    $('histClear').style.display = isEval ? 'none' : '';
    $('histExport').style.display = isEval ? 'none' : '';
    if (isEval) { loadEvals(); if (!evalTimer) evalTimer = setInterval(loadEvals, 15000); }
    else if (evalTimer) { clearInterval(evalTimer); evalTimer = null; }
    sendHitbox();
  }
  tabLog.onclick = (e) => { e.stopPropagation(); switchTab('log'); };
  tabEval.onclick = (e) => { e.stopPropagation(); switchTab('eval'); };
  async function loadEvals() {
    try {
      const d = await (await fetch(API + '/eval/sessions')).json();
      evalItems = (d && d.items) || [];
      renderEvalList();
    } catch (e) { evalList.innerHTML = '<div id="histEmpty">评测服务连不上（后端 8765？）</div>'; }
  }
  const ST = { open: ['进行中 live', 'open'], running: ['评测中… evaluating', 'running'],
               done: ['✓ 已出报告 done', 'done'], error: ['✗ 评测失败 error', 'error'],
               skipped_short: ['不足5分钟 skip', 'skip'], skipped_thin: ['发言太少 skip', 'skip'] };
  function renderEvalList() {
    if (!evalItems.length) { evalList.innerHTML = '<div id="histEmpty">还没有会话记录<br>和老师聊满 5 分钟（≥3 句发言），关闭老师窗口后自动生成评测报告</div>'; return; }
    evalList.innerHTML = '';
    for (const x of evalItems) {
      const div = document.createElement('div');
      const st = ST[x.eval_status] || [x.eval_status || '', 'skip'];
      div.className = 'eitem' + (x.session_id === curEvalSid ? ' open' : '');
      div.innerHTML = '<span class="badge ' + st[1] + '">' + st[0] + '</span>'
        + '<div class="et">📅 ' + esc(x.started_str || '') + (x.read ? '' : ' <b style="color:#ff3860">● NEW</b>') + '</div>'
        + '<div class="es">时长 ' + (x.duration_min || 0) + ' 分钟 · 学生发言 ' + (x.user_turns || 0) + ' 句'
        + (x.has_report ? ' · 点击查看评测报告' : (x.eval_error ? ' · ' + esc(x.eval_error) : '')) + '</div>';
      div.onclick = (e) => { e.stopPropagation(); openEval(x); };
      evalList.appendChild(div);
    }
  }
  async function openEval(x) {
    curEvalSid = x.session_id;
    evalList.style.display = 'none';
    evalDetail.style.display = 'block';
    evalDetail.innerHTML = '<div id="histEmpty">加载中…</div>';
    // 详情条: 返回 / 导出MD / 删除 / 重评
    let bar = document.getElementById('edBar');
    if (!bar) { bar = document.createElement('div'); bar.id = 'edBar'; evalWrap.appendChild(bar); }
    bar.innerHTML = '<button id="edBack">← 列表</button><span class="sp"></span>'
      + '<button id="edRerun">↻ 重新评测</button>'
      + '<button id="edExport">⬇ 导出MD</button>'
      + '<button id="edDel" class="danger">🗑 删除</button>';
    bar.style.display = 'flex';
    document.getElementById('edBack').onclick = (e) => { e.stopPropagation(); backToEvalList(); };
    document.getElementById('edRerun').onclick = async (e) => {
      e.stopPropagation();
      try { const j = await (await fetch(API + '/eval/sessions/' + curEvalSid + '/rerun', { method: 'POST' })).json();
        evalHint(j.evaluating ? '重新评测中，稍候自动刷新' : ('不触发: ' + (j.reason || ''))); loadEvals(); }
      catch (err) { evalHint('重评失败'); }
    };
    document.getElementById('edExport').onclick = async (e) => {
      e.stopPropagation();
      try {
        const resp = await fetch(API + '/eval/sessions/' + curEvalSid + '/md');
        if (!resp.ok) throw new Error('http ' + resp.status);
        const md = await resp.text();
        const fname = '会话评测-' + curEvalSid + '.md';
        if (window.teacher && teacher.saveText) {
          const r = await teacher.saveText(md, fname);
          evalHint(r && r.saved ? '已导出: ' + r.path : '未保存');
        } else {
          const a = document.createElement('a');
          a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }));
          a.download = fname; a.click();
          setTimeout(() => URL.revokeObjectURL(a.href), 5000);
          evalHint('已下载');
        }
      } catch (err) { evalHint('导出失败: ' + err.message); }
    };
    document.getElementById('edDel').onclick = async (e) => {
      e.stopPropagation();
      if (!confirm('删除这条会话记录及其评测报告？不可恢复。')) return;
      try { await fetch(API + '/eval/sessions/' + curEvalSid, { method: 'DELETE' }); } catch (err) {}
      backToEvalList(); evalHint('已删除'); loadEvals(); evalBadgeCheck();
    };
    try {
      if (x.has_report) {
        const md = await (await fetch(API + '/eval/sessions/' + curEvalSid + '/md')).text();
        evalDetail.innerHTML = mdLite(md);
      } else if (x.eval_status === 'error') {
        evalDetail.innerHTML = '<p style="color:#ff9a9a">评测失败：' + esc(x.eval_error || '未知错误')
          + '<br>可点下方「重新评测」再试一次。</p>';
      } else {
        const why = x.eval_status === 'running' ? 'AI 正在批改本次会话，通常 1~2 分钟…'
          : x.eval_status === 'open' ? '会话进行中（满 5 分钟且≥3 句发言，关闭窗口后自动生成报告）'
          : '本次会话太短/发言太少，未达评测门槛（≥5 分钟 且 ≥3 句）。记录仍保留。';
        evalDetail.innerHTML = '<div id="histEmpty">' + why + '</div>';
      }
      fetch(API + '/eval/sessions/' + curEvalSid + '/read', { method: 'POST' }).catch(() => {});
      histBtn.classList.remove('hasEval');
    } catch (e) { evalDetail.innerHTML = '<p>加载失败: ' + esc(e.message) + '</p>'; }
  }
  function backToEvalList() {
    curEvalSid = null;
    evalList.style.display = ''; evalDetail.style.display = 'none';
    const bar = document.getElementById('edBar'); if (bar) bar.style.display = 'none';
    loadEvals();
  }
  function evalHint(msg) { histHint(msg); }

  function postHistory(role, text, via) {
    if (!text) return;
    fetch(API + '/history', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role, text, sid: evalSid || undefined, via: via || 'text' }) }).catch(() => {});
  }
  // ---------- 会话评测 / session lifecycle ----------
  let evalSid = null;
  function sessionStart() {
    fetch(API + '/session/start', { method: 'POST' })
      .then((r) => r.json())
      .then((j) => { if (j && j.ok) { evalSid = j.session_id; evalBadgeCheck(); } })
      .catch(() => {});
  }
  function sessionEnd() {
    if (!evalSid) return;
    const s = evalSid; evalSid = null;
    try { navigator.sendBeacon && navigator.sendBeacon(API + '/session/end',
      new Blob([JSON.stringify({ session_id: s })], { type: 'application/json' })); } catch (e) {}
  }
  // 评测红点: 有 done/error 报告而用户未读时点亮 histBtn
  function evalBadgeCheck() {
    fetch(API + '/eval/sessions').then((r) => r.json()).then((j) => {
      const items = (j && j.items) || [];
      const unseen = items.filter((x) => (x.eval_status === 'done' || x.eval_status === 'error') && !x.read);
      histBtn.classList.toggle('hasEval', unseen.length > 0);
    }).catch(() => {});
  }
  setInterval(evalBadgeCheck, 30000);

  // ---------- WebRTC ----------
  let pc = null, sid = null, es = null, retry = 0;
  let waitAt = 0;   // 1018: 首次连接尝试时刻 —— 引擎冷启动要加载模型(1~2min), 用它算"已等待 N 秒"
  let turnBuf = [], lastFrag = null;
  // 1004(四老师): 见面语。连麦connected后1.2s用老师本人音色(/human echo→引擎TTS)原句朗读。
  // 每个 sessionid 只播一次(断线重连同sid不重播; 切老师重连=新sid=新老师问好, 合理)。
  const greeted = new Set();
  async function greetOnce() {
    try {
      if (!sid || greeted.has(String(sid))) return;
      if (!GREET) await pullPersona();     // 首连竞态: persona还没拉到则先补拉
      if (!GREET) return;
      greeted.add(String(sid));            // 先占坑防并发重入
      await new Promise((r) => setTimeout(r, 1200));  // 等画面稳定再开口
      if (!sid || !pc || pc.connectionState !== 'connected') return;
      await fetch(LT + '/human', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionid: String(sid), type: 'echo', text: GREET }) });
    } catch (e) { /* 见面语失败不影响主链路 */ }
  }
  function flushTurn() {
    const text = turnBuf.join(' ').replace(/\s+/g, ' ').trim();
    turnBuf = []; lastFrag = null;
    if (text) postHistory('assistant', text);
  }
  function sse(id) {
    if (es) { try { es.close(); } catch (e) {} }
    es = new EventSource(LT + '/sse?sessionid=' + encodeURIComponent(id));
    es.onmessage = (ev) => {
      let d; try { d = JSON.parse(ev.data); } catch (e) { return; }
      if (d.text) {
        const t = d.text.trim();
        if (t && t !== lastFrag) {          // 引擎可能重发同句，去重
          lastFrag = t; turnBuf.push(t);
          pushTeacherSub(turnBuf.join(' '));   // 1007r3: 整轮内容流式排进单块(非逐句一行)
        }
      }
      if (d.status === 'end') flushTurn();
    };
    es.onerror = () => {};
  }
  async function connect() {
    if (pc) return;
    // 1018: 首轮才亮"连接中…"; 后续每 5s 重试保持"老师准备中…N 秒"文案, 避免 连接中/准备中 反复闪烁
    if (!waitAt) { waitAt = Date.now(); setStatus(false, '连接中…'); }
    try {
      pc = new RTCPeerConnection({ sdpSemantics: 'unified-plan' });
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.addTransceiver('audio', { direction: 'recvonly' });
      pc.addEventListener('track', (e) => {
        const s = e.streams[0];
        if (e.track.kind === 'video') vid.srcObject = s; else aud.srcObject = s;
      });
      const _pc = pc;   // 1022ui: 本连接的实例快照 —— 防"迟到回调读已被置 null 的 pc"崩溃
      pc.addEventListener('connectionstatechange', () => {
        if (pc !== _pc) return;   // 本连接已 teardown/被新连接取代: 忽略其迟到事件(否则会误报断线并重连)
        if (_pc.connectionState === 'connected') {
          setStatus(true, '桌面老师在线');
          greetOnce();
        }
        else if (_pc.connectionState === 'failed' || _pc.connectionState === 'disconnected') {
          setStatus(false, '断线，5秒后重连…'); teardown(false); setTimeout(connect, 5000);
        }
      });
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise((r) => {
        if (pc.iceGatheringState === 'complete') return r();
        const c = () => { if (pc.iceGatheringState === 'complete') { pc.removeEventListener('icegatheringstatechange', c); r(); } };
        pc.addEventListener('icegatheringstatechange', c);
        setTimeout(r, 2500);
      });
      const res = await fetch(LT + '/offer', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        // 1003(卡通形象): avatar=视频素材包, refaudio=音色preset名(引擎按preset覆盖REF_FILE/REF_TEXT)
        body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type,
          avatar: AVATAR, refaudio: AVATAR_VOICE }),
      });
      const ans = await res.json();
      if (ans.code && ans.code !== 0) throw new Error(ans.msg || 'offer 失败');
      if (!ans.sdp) throw new Error('无 SDP');
      if (ans.sessionid) { sid = ans.sessionid; sse(sid); if (!evalSid) sessionStart(); }
      await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: ans.sdp }));
      retry = 0; waitAt = 0;
      vid.play().catch(() => {}); aud.play().catch(() => {});
    } catch (e) {
      pc = null;
      const msg = String((e && e.message) || e || '');
      const secs = Math.round((Date.now() - (waitAt || Date.now())) / 1000);
      if (/Maximum session|session limit/i.test(msg)) {
        // 引擎单会话槽被占(换形象/双开残留): 明确说"会话已满", 别报连接失败
        setStatus(false, '会话已满，5秒后重试…');
      } else if (/Failed to fetch|NetworkError|Load failed|ERR_|net::/i.test(msg)) {
        // 1018(Owen反馈"一直提示连接失败, 过好久才连上"): 真凶=引擎冷启动要加载模型(TTS 40~60s + 数字人 30s)。
        // 端口没监听时 fetch 直接失败 —— 那是"老师正在准备", 不是故障。给进度+琥珀灯, 附带人工兜底指引。
        if (secs < 150) setStatus(false, '老师准备中…已等待 ' + secs + ' 秒', 'wait');
        else setStatus(false, '老师仍未就绪(' + secs + 's)·右键托盘→启动/修复', 'wait');
      } else {
        setStatus(false, '连接失败：' + msg);
      }
      if (retry++ < 60) setTimeout(connect, 5000);
    }
  }
  function teardown(closePc) {
    if (es) { try { es.close(); } catch (e) {} es = null; }
    if (closePc && pc) { try { pc.close(); } catch (e) {} }
    pc = null; sid = null; vid.srcObject = null; aud.srcObject = null;
  }

  // ---------- 对话 ----------
  async function send() {
    const t = tinp.value.trim();
    if (!t) return;
    if (!sid) { showTip('还没连上，稍等…'); return; }
    tinp.value = '';
    flushTurn();                       // 上一轮若未收到 end，先落账
    pushSub(t, true, { replaceMine: true });   // 学生自己的话上字幕（青色），替换旧学生行
    postHistory('user', t, send._via || 'text'); send._via = null;
    try {
      await fetch(LT + '/human', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: t, type: 'chat', interrupt: true, sessionid: String(sid) }),
      });
    } catch (e) { pushSub('发送失败：' + e.message, false); }
  }
  $('send').onclick = send;
  tinp.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });

  // ---------- 1012 视觉老师：📷 拍照问老师 ----------
  // 流程: 开取景(拉视频流) → 📸快门(canvas缩到768px JPEG→立即停流释放摄像头,隐私灯灭)
  //       → 审阅(重拍/输入问题) → 发 /human {text, image} → 引擎带图问模型 → 老师看图用英语教学
  const camBtn = $('camBtn'), camModal = $('camModal'), camVid = $('camVid'), camSel = $('camSel'),
        camClose = $('camClose'), camSnapBtn = $('camSnap'), camRev = $('camRev'), camThumb = $('camThumb'),
        camTxt = $('camTxt'), camSendBtn = $('camSend'), camCancelBtn = $('camCancel'), camHint = $('camHint');
  let camStream = null, camShot = null;          // camShot: 快门后的 dataURL（停流后仍可预览/发送）
  function camStop() {
    if (camStream) { camStream.getTracks().forEach(t => t.stop()); camStream = null; }
    camVid.srcObject = null;
  }
  function camStatus(msg) { if (camHint) camHint.textContent = msg; }
  async function camOpen() {
    camModal.style.display = 'flex';
    camRev.style.display = 'none';
    camShot = null;
    camStatus('开启摄像头…');
    try {
      camStop();
      let devices = [];
      try { devices = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput'); } catch (e) {}
      camSel.innerHTML = '';
      devices.forEach((d, i) => {
        const o = document.createElement('option');
        o.value = d.deviceId; o.textContent = d.label || ('摄像头 ' + (i + 1));
        camSel.appendChild(o);
      });
      const constraints = { audio: false, video: devices.length ? { deviceId: { exact: camSel.value || devices[0].deviceId } } : true };
      camStream = await navigator.mediaDevices.getUserMedia(constraints);
      camVid.srcObject = camStream;
      await camVid.play().catch(() => {});
      camStatus('对准不会说的东西，按 📸 拍照');
    } catch (e) {
      camStatus('摄像头不可用：' + (e && e.name ? e.name : e) + '（检查占用/权限）');
      showTip('打不开摄像头：' + (e && e.name ? e.name : '未知错误'));
    }
  }
  camSel && camSel.addEventListener('change', () => { camOpen(); });
  function camShutter() {
    if (!camStream || !camVid.videoWidth) { camStatus('画面还没准备好，稍等…'); return; }
    // 等比缩到长边≤768px，JPEG 0.85 通常≈80-150KB，网关带图延迟可控
    const vw = camVid.videoWidth, vh = camVid.videoHeight;
    const sc = Math.min(1, 768 / Math.max(vw, vh));
    const cv = document.createElement('canvas');
    cv.width = Math.round(vw * sc); cv.height = Math.round(vh * sc);
    cv.getContext('2d').drawImage(camVid, 0, 0, cv.width, cv.height);
    camShot = cv.toDataURL('image/jpeg', 0.85);
    camStop();                                   // ★快门即释放设备（隐私灯灭）
    camThumb.src = camShot;
    camRev.style.display = 'flex';
    camVid.style.visibility = 'hidden';
    camStatus('看一下拍清楚了吗');
  }
  function camRetake() {
    camRev.style.display = 'none';
    camVid.style.visibility = 'visible';
    camShot = null;
    camOpen();
  }
  async function camSend() {
    if (!camShot) { camStatus('先按 📸 拍照'); return; }
    if (!sid) { showTip('还没连上老师，稍等…'); return; }
    const q = (camTxt.value || '').trim();
    const shown = q || '（学生拍了一张照片给老师看）';
    camSendBtn.disabled = true;
    try {
      flushTurn();
      pushSub(shown, true, { replaceMine: true });
      postHistory('user', '📷 ' + shown, 'photo');
      await fetch(LT + '/human', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: q, type: 'chat', interrupt: true, sessionid: String(sid), image: camShot }),
      });
      camTxt.value = '';
      camModal.style.display = 'none';
      camStop();
      showTip('照片已发给老师，等老师看画面…');
    } catch (e) {
      camStatus('发送失败：' + e.message);
    } finally { camSendBtn.disabled = false; }
  }
  camBtn && (camBtn.onclick = (e) => {
    e.stopPropagation();
    // 1012v2: 优先开独立抓拍窗(可自由移动/放大, 不挡老师); 老界面降级回内嵌弹窗
    if (window.teacher && teacher.camOpen) teacher.camOpen(); else camOpen();
  });
  camClose && (camClose.onclick = () => { camModal.style.display = 'none'; camStop(); camShot = null; });
  camSnapBtn && (camSnapBtn.onclick = camShutter);
  camSendBtn && (camSendBtn.onclick = camSend);
  camCancelBtn && (camCancelBtn.onclick = camRetake);
  camTxt && camTxt.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); camSend(); } });

  // 1012v2: 接收独立抓拍窗回传的照片 → 复用 /human 发送链路（与 camSend 同口径）
  if (window.teacher && teacher.onCam) {
    teacher.onCam(async (d) => {
      if (!d || !d.image) return;
      if (!sid) { showTip('还没连上老师，稍等…'); return; }
      const question = (d.q || '').trim();
      const shown = question || '（学生拍了一张照片给老师看）';
      try {
        flushTurn();
        pushSub(shown, true, { replaceMine: true });
        postHistory('user', '📷 ' + shown, 'photo');
        await fetch(LT + '/human', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: question, type: 'chat', interrupt: true, sessionid: String(sid), image: d.image }),
        });
        showTip('照片已发给老师，等老师看画面…');
      } catch (e) { showTip('发送失败：' + e.message); }
    });
  }

  // ---------- 打断（正确路由 /interrupt_talk；flush_talk 清 TTS 队列+停口型） ----------
  async function doInterrupt() {
    if (!sid) return;
    flushTurn();                        // 已说部分先落历史，不丢账
    try {
      await fetch(LT + '/interrupt_talk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionid: String(sid) }),
      });
    } catch (e) {}
    showTip('已打断');
  }
  intrBtn.onclick = (e) => { e.stopPropagation(); doInterrupt(); };
  setBtn.onclick = (e) => { e.stopPropagation(); if (window.teacher) teacher.openSettings(); };
  // ---------- 底部"关闭"=关闭会话：落账→收面板→结算(触发评测)→藏窗 ----------
  // 打断功能保留在右上角 intrBtn（⏹）；窗口隐藏后 visibilitychange 已自动停麦/停SSE，
  // 托盘"召唤老师"回来时 mode=full 分支会自动 sessionStart 开新场次，闭环无死角。
  $('closeSess').onclick = (e) => {
    if (e) e.stopPropagation();
    flushTurn();                        // 老师已说的先进历史，不丢账
    closeHist();
    sessionEnd();                       // beacon 结算；与之后 hide 触发的 hidden 双保险幂等
    showTip('会话已结束，评测生成中');
    if (window.teacher) teacher.hide();
  };

  // 说话状态轮询：老师在说话 -> 打断按钮红色脉冲"armed"
  setInterval(async () => {
    if (!sid) { intrBtn.classList.remove('armed'); return; }
    try {
      const r = await fetch(LT + '/is_speaking', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sessionid: String(sid) }),
      });
      const d = await r.json();
      intrBtn.classList.toggle('armed', !!(d && d.data));
    } catch (e) {}
  }, 1200);

  // ---------- 点击 / 拖拽 / 缩放 ----------
  let downXY = null, moved = 0, clickN = 0, clickT = null;
  const EDGE = 7;
  function edgeOf(e) {
    const b = wrap.getBoundingClientRect();
    const ex = e.clientX - b.left, ey = e.clientY - b.top;
    let s = '';
    if (ey <= EDGE) s += 'n'; if (ey >= b.height - EDGE) s += 's';
    if (ex <= EDGE) s += 'w'; if (ex >= b.width - EDGE) s += 'e';
    return s;
  }
  // 悬停边缘时给出标准调整光标提示(炫酷但克制)
  wrap.addEventListener('mousemove', (e) => {
    if (downXY) return;
    const s = edgeOf(e);
    wrap.style.cursor = !s ? 'pointer'
      : (s === 'n' || s === 's') ? 'ns-resize' : (s === 'e' || s === 'w') ? 'ew-resize'
      : (s === 'nw' || s === 'se') ? 'nwse-resize' : 'nesw-resize';
  }, { passive: true });
  // 1002直播定稿: 身体拖动=纯移动绝不变大小; 边缘按住拖=调整边框改大小; 滚轮=整体缩放
  wrap.addEventListener('mousedown', (e) => {
    const edge = edgeOf(e);
    downXY = { x: e.screenX, y: e.screenY }; moved = 0;
    const startW = window.outerWidth, startH = window.outerHeight;
    const mm = (ev) => {
      const dx = ev.screenX - downXY.x, dy = ev.screenY - downXY.y;
      moved = Math.max(moved, Math.abs(dx) + Math.abs(dy));
      downXY = { x: ev.screenX, y: ev.screenY };
      if (moved <= 5 || !window.teacher) return;
      if (edge) teacher.edgeResize(edge, dx, dy);
      else teacher.drag(dx, dy);
    };
    const mu = () => {
      if (moved > 5 && window.teacher && teacher.dragEnd && !edge) teacher.dragEnd(startW, startH);
      downXY = null;
      window.removeEventListener('mousemove', mm); window.removeEventListener('mouseup', mu);
      if (edge || moved > 5) { clickN = 0; return; }   // 边缘/拖动不触发单击/双击语义
      if (DND) { if (window.teacher) teacher.summon(); return; }
      clickN = (clickN || 0) + 1;
      clearTimeout(clickT);
      clickT = setTimeout(() => {
        if (clickN >= 2) {
          const hid = wrap.style.opacity === '0';
          setFaceHidden(!hid);
        } else if (wrap.style.opacity !== '0') toggleBar();
        clickN = 0;
        sendHitbox();
      }, 260);
    };
    window.addEventListener('mousemove', mm); window.addEventListener('mouseup', mu);
  });
  function toggleBar() {
    if (bar.style.display !== 'flex') { bar.style.display = 'flex'; tinp.focus(); }
    else bar.style.display = 'none';
    sendHitbox();
  }
  // 滚轮整体缩放: 老师窗(框+画面)等比放大缩小 —— 由主进程改窗口 bounds, 内容不裁切
  // 面板内滚轮仍归面板滚动; Ctrl+滚轮 = 一键复位 100%
  window.addEventListener('wheel', (e) => {
    const t = e.target;
    if (t && t.closest && t.closest('#histPanel,#evalWrap,#camModal')) return;
    if (!window.teacher || !teacher.zoom) return;
    e.preventDefault();
    if (e.ctrlKey) { if (teacher.zoomReset) teacher.zoomReset(); return; }
    teacher.zoom(e.deltaY < 0 ? 1.08 : 1 / 1.08);
  }, { passive: false });

  // ---------- mini/full 模式联动 ----------
  if (window.teacher && teacher.onMode) {
    teacher.onMode((m) => {
      DND = (m === 'mini');
      if (m === 'mini') {
        bar.style.display = 'none'; topBtns.style.display = 'none'; closeHist();
      } else {
        topBtns.style.display = 'flex';
        micBtn.style.display = micBtn._avail ? 'block' : 'none';
        // 1002直播保险: 托盘/唤回到 full 时必须把脸复位, 防止 opacity=0 残留=透明幽灵窗
        if (m === 'full') {
          setFaceHidden(false); tip.style.opacity = '0';
          clearTimeout(showTip._t);
          if (sid && !evalSid) sessionStart();   // 会话评测: 唤回=开新场次
        }
        if (m === 'hidden') sessionEnd();        // 双保险: 明确收到hidden也结算
      }
      sendHitbox();
    });
  }

  // 会话评测: 窗口隐藏(关✕/隐身/托盘)=关窗 => 结算; 重新可见 => 新会话
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') { flushTurn(); sessionEnd(); }
    else if (document.visibilityState === 'visible' && sid && !evalSid) { sessionStart(); }
  });

  // ---------- 麦克风 STT（赛博朋克按钮，按住说话） ----------
  let rec = null, proc = null, chunks = [], recCtx = null, recording = false, recSession = 0;
  function encodeWav(pcm, rate) {
    const buf = new ArrayBuffer(44 + pcm.length * 2), v = new DataView(buf);
    const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    ws(0, 'RIFF'); v.setUint32(4, 36 + pcm.length * 2, true); ws(8, 'WAVE'); ws(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    ws(36, 'data'); v.setUint32(40, pcm.length * 2, true);
    let o = 44;
    for (let i = 0; i < pcm.length; i++, o += 2) { const s = Math.max(-1, Math.min(1, pcm[i])); v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7FFF, true); }
    return buf;
  }
  (async () => {
    try {
      const h = await (await fetch(API + '/health')).json();
      if (h.stt) { micBtn._avail = true; if (!DND) micBtn.style.display = 'block'; sendHitbox(); }
    } catch (e) {}
  })();
  micBtn.addEventListener('mousedown', async (e) => {
    e.stopPropagation(); e.preventDefault();
    if (recording) return;
    recording = true; micBtn.classList.add('rec');
    const mySession = ++recSession;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
      if (mySession !== recSession) { stream.getTracks().forEach((t) => t.stop()); return; }
      recCtx = new (window.AudioContext || window.webkitAudioContext)(); await recCtx.resume();
      const src = recCtx.createMediaStreamSource(stream);
      proc = recCtx.createScriptProcessor(4096, 1, 1);
      chunks = [];
      proc.onaudioprocess = (ev) => { chunks.push(new Float32Array(ev.inputBuffer.getChannelData(0))); };
      const mute = recCtx.createGain(); mute.gain.value = 0;
      src.connect(proc); proc.connect(mute); mute.connect(recCtx.destination);
      rec = stream;
    } catch (err) { recording = false; micBtn.classList.remove('rec'); pushSub('麦克风不可用: ' + err.message, false); }
  });
  micBtn.addEventListener('mouseup', async (e) => {
    e.stopPropagation();
    if (!recording) return;
    recording = false; recSession++; micBtn.classList.remove('rec');
    try {
      if (!proc || !chunks.length) { showTip('按久一点再松开哦'); cleanupRec(); return; }
      proc.disconnect();
      const rate = recCtx.sampleRate;
      let total = 0; for (const c of chunks) total += c.length;
      const pcm = new Float32Array(total); let off = 0;
      for (const c of chunks) { pcm.set(c, off); off += c.length; }
      const wav = encodeWav(pcm, rate), secs = total / rate;
      cleanupRec();
      if (secs < 0.4) { showTip('按久一点再松开哦'); return; }
      pushSub('🎙 听写中…', true, { replaceMine: true });
      const fd = new FormData(); fd.append('file', new Blob([wav], { type: 'audio/wav' }), 'say.wav');
      const j = await (await fetch(API + '/stt', { method: 'POST', body: fd })).json();
      if (j.ok && j.text) {
        // 1006v3: confirm 模式=先出确认条(✓发送/✗取消), 不直接发; auto 模式=旧行为松手即发
        if (VOICE_CONFIRM) { openConfirm(j.text.trim(), wav); return; }
        doVoiceSend(j.text.trim(), wav);
      }
      else pushSub('没听清呢…再说一次？', false);
    } catch (err) { cleanupRec(); pushSub('语音识别失败: ' + err.message, false); }
  });
  // 1006v3: 语音轮真正发送(auto 模式直接调; confirm 模式点✓后调)。wav=原始录音, 发送后补传评测留存。
  function doVoiceSend(text, wav) {
      send._via = 'voice';                                   // 评测: 标注语音轮
      try {                                                  // 三份数据: 原始音频留存
        stuRemember(text, wav);                              // 1019: 留档本地原声(会话记录回放用)
        const fd2 = new FormData(); fd2.append('file', new Blob([wav], { type: 'audio/wav' }), 'say.wav');
        fetch(API + '/eval/audio', { method: 'POST', body: fd2 })
          .then((r) => r.json())
          .then((j) => { if (j && j.ok && j.audio) stuSetFn(text, j.audio); })
          .catch(() => {});
      } catch (e) {}
      tinp.value = text; send();
    }
  // ---------- 1006v3 语音确认条: ✓发送 / ✗取消 / 文本可编辑 / Enter=发送 Esc=取消 ----------
  function openConfirm(text, wav) {
    cfmPayload = { text, wav };
    cfmTxt.value = text;
    confirmBar.style.display = 'flex';
    bar.style.display = 'none';                 // 确认条与输入条同占底部, 互斥显示
    sendHitbox();
    try { cfmTxt.focus(); cfmTxt.setSelectionRange(text.length, text.length); } catch (e) {}
    clearTimeout(openConfirm._t);
    openConfirm._t = setTimeout(closeConfirm, 30000);   // 30s无操作自动撤销, 防确认条永久占屏
  }
  function closeConfirm() {
    clearTimeout(openConfirm._t);
    confirmBar.style.display = 'none';
    cfmPayload = null;
    sendHitbox();
  }
  function cfmDoSend() {
    const text = (cfmTxt.value || '').trim();
    const wav = cfmPayload && cfmPayload.wav;
    closeConfirm();
    if (!text) { pushSub('已取消（内容为空）', false); return; }
    if (wav) doVoiceSend(text, wav);
    else { tinp.value = text; send(); }
  }
  function cfmDoCancel() {
    closeConfirm();
    pushSub('已取消，没有发送', false);
    showTip('已取消这句话');
  }
  cfmSend.addEventListener('mousedown', (e) => e.stopPropagation());   // 确认条内操作不触发窗口拖拽/单击
  cfmCancel.addEventListener('mousedown', (e) => e.stopPropagation());
  confirmBar.addEventListener('mousedown', (e) => e.stopPropagation());
  cfmSend.onclick = (e) => { e.stopPropagation(); cfmDoSend(); };
  cfmCancel.onclick = (e) => { e.stopPropagation(); cfmDoCancel(); };
  cfmTxt.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); cfmDoSend(); }
    else if (e.key === 'Escape') { e.preventDefault(); cfmDoCancel(); }
  });
  // 1006v3 自动化测试钩子: 直接驱动确认条(不经真实录音), 返回当前状态供 CDP 断言
  window.__cfmTest = (action, text) => {
    if (action === 'open') openConfirm(text || 'hello teacher', null);
    else if (action === 'send') cfmDoSend();
    else if (action === 'cancel') cfmDoCancel();
    return JSON.stringify({ visible: confirmBar.style.display === 'flex', txt: cfmTxt.value,
                            voiceConfirm: VOICE_CONFIRM, mode: VOICE_CONFIRM ? 'confirm' : 'auto' });
  };
  function cleanupRec() {
    try { if (rec) rec.getTracks().forEach((t) => t.stop()); } catch (e) {}
    try { if (recCtx && recCtx.state !== 'closed') recCtx.close(); } catch (e) {}
    rec = null; recCtx = null; proc = null; chunks = [];
  }

  // ---------- 退出释放会话槽 ----------
  window.addEventListener('beforeunload', () => {
    flushTurn();
    sessionEnd();                       // 会话评测: 关窗即结算
    if (sid) {
      try {
        navigator.sendBeacon && navigator.sendBeacon(LT + '/interrupt_talk',
          new Blob([JSON.stringify({ interrupt: false, sessionid: String(sid), close: 1 })], { type: 'application/json' }));
      } catch (e) {}
    }
    teardown(true);
  });

  micBtn.style.display = 'none';   // STT 可用才亮
  // 1003(卡通形象): 启动先从 /config 拉形象+音色, 再 connect(offer 带新参数);
  // 后端没起/无字段 → 保持默认老师+default, 行为与旧版一致(向后兼容)。
  (async () => {
    try {
      const c = await (await fetch(API + '/config')).json();
      if (c.avatar_id) AVATAR = c.avatar_id;
      VOICE_CONFIRM = c.voice_confirm_mode === 'confirm';   // 1006v3: 语音确认发送开关
      // 音色preset跟随形象(注册表优先), 无注册表时退 config.avatar_voice/default
      try {
        const av = await (await fetch(API + '/avatars')).json();
        const me = (av.avatars || []).find(a => a.avatar_id === AVATAR);
        AVATAR_VOICE = (me && me.voice_preset) || c.avatar_voice || 'default';
        LT = (me && me.engine) || LT_DEFAULT;   // 1006: 按老师渲染引擎路由offer
      } catch (e) { AVATAR_VOICE = c.avatar_voice || 'default'; }
    } catch (e) { /* backend未就绪: 用默认 */ }
    connect();

    // 1003(卡通形象): 热切换——设置页保存后自动换形象重连(2.5s轮询/config)。
    // 先尽力关旧会话腾槽, 再teardown重拉config; 引擎槽未及释放时connect自带5s重试。
    let lastAvatar = AVATAR;
    setInterval(async () => {
      try {
        const c = await (await fetch(API + '/config')).json();
        // 1006v3: 语音确认发送开关热更新(设置页保存后≤2.5s生效, 无需重启); 必须在换形象的早退之前
        const vc = c.voice_confirm_mode === 'confirm';
        if (vc !== VOICE_CONFIRM) {
          VOICE_CONFIRM = vc;
          if (!vc) closeConfirm();          // 切回直发模式: 撤销未决确认条(其内容留在字幕提示外, 不打扰)
          else micBtn.title = '按住说话（松手后确认发送）';
          if (!vc) micBtn.title = '按住说话';
        }
        const av = c.avatar_id || 'aiteacher_demo';
        if (av === lastAvatar) return;
        lastAvatar = av;
        setStatus(false, '切换形象中…');
        if (sid) { try { await fetch(LT + '/interrupt_talk', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ interrupt: false, sessionid: String(sid), close: 1 }) }); } catch (e) {} }
        teardown(true);
        AVATAR = av;
        try {
          const avl = await (await fetch(API + '/avatars')).json();
          const me = (avl.avatars || []).find(a => a.avatar_id === av);
          AVATAR_VOICE = (me && me.voice_preset) || c.avatar_voice || 'default';
          LT = (me && me.engine) || LT_DEFAULT;   // 1006: 换老师同时换引擎
        } catch (e) { AVATAR_VOICE = c.avatar_voice || 'default'; }
        await pullPersona();   // 1004: 换魂必同步刷新名字+见面语(新老师上线先问好)
        connect();
      } catch (e) {}
    }, 2500);
  })();
})();
