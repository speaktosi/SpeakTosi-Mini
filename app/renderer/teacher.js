// SpeakTosi 渲染层：半身像 + 头随鼠标 + 呼吸/眨眼 + 口型FFT + 气泡对话 + 文字/语音输入 + 校准模式
(function () {
  const $ = (id) => document.getElementById(id);
  const img = $("bust"), teacherWrap = $("teacherWrap"), bubble = $("bubble"), micBtn = $("mic");
  const mouth = $("mouth"), lidL = $("lidL"), lidR = $("lidR"), stage = $("stage");
  const BACKEND = "http://127.0.0.1:8765";
  const API = (window.teacher && window.teacher.apiBase) || BACKEND;

  let NAT = { w: 531, h: 693 }, SCALE = 1, LM = {};
  let USERSCALE = 1;   // 用户缩放系数（滚轮/Ctrl拖拽调节，主进程持久化 cfg.scale）
  let BUBBLEW = 86;    // 气泡最大宽度%（Shift+滚轮调节，主进程持久化 cfg.bubbleW）
  function applyBubbleW() {
    bubble.style.maxWidth = BUBBLEW + "%";
    // 宽度超过人物区时气泡可伸出窗口左缘，保持 translateX(-50%) 居中于人物
    bubble.style.left = "50%";
  }
  // 基于 teacher_bust.png（重裁：人物完整bbox+4px余量，原bbox 749,27起）；网格实测坐标已平移+3/+1
  let LMD = {
    head:   [200, 92, 365, 300],
    eyeL:   [203, 154, 233, 165],
    eyeR:   [274, 149, 312, 161],
    mouth:  [227, 243, 287, 262],
  };
  const CAL_KEYS = ["head", "eyeL", "eyeR", "mouth"];

  // ---------- 尺寸/锚定 ----------
  let DND = false; // mini 小头像态
  let HIDDEN = false;
  function layout() {
    const W = window.innerWidth, H = window.innerHeight;
    if (DND) {
      // mini小头像：face_crop=含头发的完整人脸框(alpha投影实测)，头顶不再被切
      const b = LMD.face_crop || LMD.head;
      const pad = 1.06;   // 6%呼吸留白
      SCALE = Math.min(W / ((b[2] - b[0]) * pad), H / ((b[3] - b[1]) * pad));
      img.style.width = Math.round(NAT.w * SCALE) + "px";
      img.style.height = Math.round(NAT.h * SCALE) + "px";
      const hcx = (b[0] + b[2]) / 2 * SCALE, hcy = (b[1] + b[3]) / 2 * SCALE;
      teacherWrap.style.left = Math.round(W / 2 - hcx) + "px";
      teacherWrap.style.top = Math.round(H / 2 - hcy) + "px";
      teacherWrap.style.right = "auto"; teacherWrap.style.bottom = "auto";
    } else {
      SCALE = Math.min(W / NAT.w, H / NAT.h);
      img.style.width = Math.round(NAT.w * SCALE) + "px";
      img.style.height = Math.round(NAT.h * SCALE) + "px";
      teacherWrap.style.left = "auto"; teacherWrap.style.top = "auto";
      teacherWrap.style.right = "0px"; teacherWrap.style.bottom = "0px";
    }
    for (const k of CAL_KEYS) {
      const el = k === "mouth" ? mouth : k === "eyeL" ? lidL : k === "eyeR" ? lidR : null;
      if (!el) continue;
      const b = LMD[k];
      if (k === "mouth") {
        el._cx = ((b[0] + b[2]) / 2) * SCALE; el._cy = ((b[1] + b[3]) / 2) * SCALE;
        el._bw = (b[2] - b[0]) * SCALE; el._bh = (b[3] - b[1]) * SCALE;
      } else {
        el.style.left = (b[0] * SCALE) + "px"; el.style.top = (b[1] * SCALE) + "px";
        el.style.width = ((b[2] - b[0]) * SCALE) + "px"; el.style.height = "0px";
        el._top = b[1] * SCALE; el._hFull = (b[3] - b[1]) * SCALE;  // 眨眼=从眼框顶向下扫合
      }
    }
    sendHitbox();
  }
  // 命中区=头部 ∪ 麦克风按钮 ∪ 底部输入条（此前只有头部→麦克风落在穿透区永远点不到）
  function hitRects() {
    // 校准模式：全窗口可点（否则透明穿透吞掉 Save/拖框的真实鼠标点击）
    const c = document.getElementById('calib');
    if (c && getComputedStyle(c).display !== 'none') {
      return [{ x: 0, y: 0, w: window.innerWidth, h: window.innerHeight }];
    }
    const b = LMD.head || [600, 0, NAT.w, 560];
    const head = { x: (b[0] - 40) * SCALE, y: Math.max(0, (b[1] - 60) * SCALE),
                   w: (b[2] - b[0] + 120) * SCALE, h: (b[3] - b[1] + 240) * SCALE };
    const W = window.innerWidth, H = window.innerHeight;
    const rects = [head];
    if (micBtn.style.display !== "none") rects.push({ x: W - 62, y: H - 62, w: 62, h: 62 });
    if (bar.style.display !== "none") rects.push({ x: 0, y: H - 60, w: W, h: 60 });
    if (bubble.style.display !== "none") {
      const bb = bubble.getBoundingClientRect();
      rects.push({ x: bb.left - 6, y: bb.top - 6, w: bb.width + 12, h: bb.height + 12 });
    }
    return rects;
  }
  function sendHitbox() {
    if (!window.teacher) return;
    teacher.hitbox(DND ? [{ x: 0, y: 0, w: window.innerWidth, h: window.innerHeight }] : hitRects());
  }
  setInterval(sendHitbox, 120);

  // ---------- 加载 landmark / 校准标记 ----------
  async function loadConfig() {
    try {
      const lm = await (await fetch(API + "/landmarks")).json();
      if (lm && lm.natural) { NAT = lm.natural; for (const k of CAL_KEYS) if (lm[k]) LMD[k] = lm[k]; }
      if (lm && lm.face_crop) LMD.face_crop = lm.face_crop;   // 含头发的完整人脸框(mini头像用)
    } catch {}
    let calib = false;
    try { calib = (await (await fetch(API + "/calibflag")).json()).on === true; } catch {}
    layout(); sampleSkin();
    if (calib) startCalib();
  }

  // ---------- 空闲动画：呼吸 + 眨眼 + 待机微摆 ----------
  let t0 = performance.now(), blink = { start: 0, dur: 0 };
  // 真肤采样：眨眼贴片颜色 = 眼皮/额头实测中位色（杜绝写死土黄"创可贴"）
  let SKIN = null;
  async function sampleSkin() {
    try {
      // file:// 图会污染 canvas → 从主进程拿 base64 dataURL 离屏重绘再采样
      let src = img;
      if (window.teacher && teacher.bustData) {
        const url = await teacher.bustData();
        if (url) { src = new Image(); src.src = url; await src.decode(); }
      }
      const cv = document.createElement("canvas"); cv.width = NAT.w; cv.height = NAT.h;
      cv.getContext("2d").drawImage(src, 0, 0, NAT.w, NAT.h);
      // 两颊采样（眼睛下方侧颊，避开头bang/眉毛/阴影）：只保留肤色特征像素
      const eL = LMD.eyeL, eR = LMD.eyeR;
      const zones = [
        [Math.max(0, eL[0] - 26), eL[3] + 12, Math.max(2, eL[0] - 2), eL[3] + 46],   // 左颊(亮侧)
        [Math.min(NAT.w - 2, eR[2] + 2), eR[3] + 12, Math.min(NAT.w, eR[2] + 26), eR[3] + 46], // 右颊
      ];
      let px = [];
      const g2 = cv.getContext("2d");
      for (const z of zones) {
        const d = g2.getImageData(z[0], z[1], z[2] - z[0], z[3] - z[1]).data;
        for (let i = 0; i < d.length; i += 4) {
          if (d[i+3] < 200) continue;
          const R = d[i], G = d[i+1], B = d[i+2];
          if (R > 110 && G > 70 && B > 45 && R > B + 25 && R < 250) px.push([R, G, B]); // 肤色特征
        }
      }
      if (!px.length) return;
      // 中位亮度像素整组RGB(三通道相关) = 两颊总肤色，兜底
      px.sort((a,b)=>(a[0]+a[1]+a[2])-(b[0]+b[1]+b[2]));
      SKIN = px[Math.floor(px.length/2)];
      // 双侧脸光照不均→每只眼各采自己眼皮上方条带，绝不共用一色
      const gctx = cv.getContext("2d");
      function strip(x0, y0, x1, y1) {
        const q = [];
        const wd = Math.max(2, Math.round(x1 - x0)), ht = Math.max(2, Math.round(y1 - y0));
        const dd = gctx.getImageData(Math.max(0, Math.round(x0)), Math.max(0, Math.round(y0)), wd, ht).data;
        for (let i = 0; i < dd.length; i += 4) {
          if (dd[i+3] < 200) continue;
          const r = dd[i], gg3 = dd[i+1], b2 = dd[i+2];
          if (r < 90 || r > 250 || !(r >= gg3 && gg3 >= b2)) continue;
          q.push([r, gg3, b2]);
        }
        if (!q.length) return null;
        q.sort((a,b)=>(a[0]+a[1]+a[2])-(b[0]+b[1]+b[2]));
        return q[Math.floor(q.length/2)];
      }
      // 实体眼睑：肤色主体+下缘睑缘暗线(睫毛感)；顶部微透明过渡到额头
      const mk = (c) => {
        const dark = [Math.max(0,c[0]-70), Math.max(0,c[1]-70), Math.max(0,c[2]-60)];
        return `linear-gradient(180deg, rgba(${c[0]},${c[1]},${c[2]},.55) 0%, rgb(${c[0]},${c[1]},${c[2]}) 30%, rgb(${c[0]},${c[1]},${c[2]}) 72%, rgb(${dark[0]},${dark[1]},${dark[2]}) 100%)`;
      };
      // 眼皮采样：取眼睑内侧半幅(避太阳穴发际)、仅上睑缘上3~9px(避刘海)；失败则退两颊色
      const halfL = (eL[2]-eL[0])*0.2, halfR = (eR[2]-eR[0])*0.2;
      let sL = strip(eL[0]+halfL, eL[1]-9, eL[2]-halfL, eL[1]-3) || strip(eL[0]+halfL, eL[1]-6, eL[2]-halfL, eL[1]-2);
      let sR = strip(eR[0]+halfR, eR[1]-9, eR[2]-halfR, eR[1]-3) || strip(eR[0]+halfR, eR[1]-6, eR[2]-halfR, eR[1]-2);
      // 肤色校验：必须接近两颊色温且够亮，否则视为采到头发→退两颊
      const cheekOk = (c) => c && c[0] > SKIN[0]-40 && c[1] > 100 && c[0]+c[1]+c[2] > SKIN[0]+SKIN[1]+SKIN[2]-90;
      if (!cheekOk(sL)) sL = strip(eL[0]+halfL, eL[1]-4, eL[2]-halfL, eL[1]+2) || SKIN;
      if (!cheekOk(sR)) sR = strip(eR[0]+halfR, eR[1]-4, eR[2]-halfR, eR[1]+2) || SKIN;
      if (!cheekOk(sL)) sL = SKIN;
      if (!cheekOk(sR)) sR = SKIN;
      lidL.style.background = mk(sL); lidR.style.background = mk(sR);
      window.__SKIN = "cheek " + SKIN.join(",") + " | lidL " + sL.join(",") + " | lidR " + sR.join(",");
    } catch (e) { console.log("skin sample fail", e); }
  }
  function scheduleBlink() {
    setTimeout(() => { blink.start = performance.now(); blink.dur = 240 + Math.random() * 80; scheduleBlink(); },
               2600 + Math.random() * 4200);
  }
  window.__blink = (ms) => { blink.start = performance.now(); blink.dur = ms || 300; };
  function idle(now) {
    if (dragging) return;
    const t = (now - t0) / 1000;
    const breath = 1 + 0.008 * Math.sin(t * 0.9);
    img.style.setProperty("--breath", breath);
    img.style.transform = `scale(${breath})`;
    // 眨眼：实体眼睑从眼框顶向下扫合再收回（opacity恒1，绝无半透明泛光）；150ms快眨，4~11s随机
    if (blink.start) {
      const p = (now - blink.start) / blink.dur;
      if (p >= 0 && p <= 1) {
        // 下合快(0~45%)、上开稍慢(45~100%)，带缓动
        const q = p < 0.45 ? (p / 0.45) : 1 - (p - 0.45) / 0.55;
        const env = q * q * (3 - 2 * q);   // smoothstep
        for (const el of [lidL, lidR]) {
          if (!el._hFull) continue;
          el.style.display = "block";
          el.style.height = (el._hFull * env).toFixed(1) + "px";
        }
      } else {
        blink.start = 0;
        for (const el of [lidL, lidR]) { el.style.display = "none"; el.style.height = "0px"; }
      }
    }
    requestAnimationFrame(idle);
  }
  function scheduleBlink() {
    setTimeout(() => { blink.start = performance.now(); blink.dur = 130 + Math.random() * 50; scheduleBlink(); },
               4000 + Math.random() * 7000);
  }
  scheduleBlink(); requestAnimationFrame(idle);

  // ---------- 头随鼠标（窗口内实时坐标，穿透模式也能拿到） ----------
  let hx = 0, hy = 0, mx = 0, my = 0;
  window.addEventListener("mousemove", (e) => { mx = e.clientX; my = e.clientY; });
  setInterval(() => {
    const W = window.innerWidth, H = window.innerHeight;
    const nx = Math.max(-1, Math.min(1, (mx - W * 0.45) / (W * 0.55)));
    const ny = Math.max(-1, Math.min(1, (my - H * 0.3) / (H * 0.7)));
    hx += (nx * 6 - hx) * 0.08; hy += (ny * 3.5 - hy) * 0.08;
    teacherWrap.style.setProperty("--hy", hy.toFixed(2) + "deg");
    teacherWrap.style.setProperty("--hx", (-hx * 0.5).toFixed(2) + "deg");
  }, 50);

  // ---------- 口型：WebAudio FFT ----------
  let actx = null, analyser = null, dataArr = null, open = 0;
  function audioPipeline(el) {
    if (!actx) { actx = new (window.AudioContext || window.webkitAudioContext)(); analyser = actx.createAnalyser(); analyser.fftSize = 512; dataArr = new Uint8Array(analyser.frequencyBinCount); }
    if (!el._src) { try { el._src = actx.createMediaElementSource(el); el._src.connect(analyser); analyser.connect(actx.destination); } catch {} }
    if (actx.state === "suspended") actx.resume();
  }
  function talkLoop() {
    if (speaking && analyser) {
      analyser.getByteFrequencyData(dataArr);
      let s = 0; for (let i = 2; i < 40; i++) s += dataArr[i];
      const v = s / 38 / 255;
      open = open * 0.55 + v * 0.45;
      const mh = Math.max(3, Math.min(46, open * 44)) * SCALE;
      const mw = mouth._bw * (1 + open * 0.35);
      mouth.style.display = "block";
      mouth.style.left = (mouth._cx - mw / 2) + "px";
      mouth.style.top = (mouth._cy - mh / 2) + "px";
      mouth.style.width = mw + "px"; mouth.style.height = mh + "px";
    } else if (open > 0.02) { open = 0; mouth.style.display = "none"; }
    else mouth.style.display = "none";
    requestAnimationFrame(talkLoop);
  }
  requestAnimationFrame(talkLoop);

  // ---------- 播放队列 ----------
  let speaking = false, queue = [];
  function playNext() {
    if (queue.length === 0) { speaking = false; return; }
    const { text, url } = queue.shift();
    if (text !== null && text !== undefined) showBubble(text);
    speaking = true;
    if (!url) { setTimeout(playNext, text ? Math.min(9000, 1200 + text.length * 160) : 200); return; }
    const audio = new Audio(url);
    audioPipeline(audio);
    audio.onended = () => { open = 0; setTimeout(playNext, 240); };
    audio.onerror = () => setTimeout(playNext, 240);
    audio.play().catch(() => setTimeout(playNext, 500));
  }
  function say(text, url) { queue.push({ text, url: url ? API + url : null }); if (!speaking) playNext(); }
  function showBubble(text) {
    bubble.textContent = text; bubble.style.display = "block";
    clearTimeout(showBubble._t);
    showBubble._t = setTimeout(() => { if (!speaking) bubble.style.display = "none"; }, 12000);
  }

  // ---------- 聊天（流式优先：首句~2s出声；SSE失败自动回退旧接口） ----------
  async function sendChat(text) {
    showBubble("…");
    let acc = "";
    try {
      const r = await fetch(API + "/chat_stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      if (!r.ok || !r.body) throw new Error("HTTP " + r.status);
      const reader = r.body.getReader(), dec = new TextDecoder();
      let sse = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        sse += dec.decode(value, { stream: true });
        let idx;
        while ((idx = sse.indexOf("\n\n")) >= 0) {
          const chunk = sse.slice(0, idx); sse = sse.slice(idx + 2);
          const line = chunk.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          let ev; try { ev = JSON.parse(line.slice(6)); } catch { continue; }
          if (ev.type === "sent") {
            acc += ev.text;
            showBubble(acc);
            clearTimeout(showBubble._t);   // 流式中不自动隐藏
            queue.push({ text: null, url: ev.tts ? API + ev.tts : null });
            if (!speaking) playNext();
          } else if (ev.type === "err") {
            showBubble("我这边出了点状况（" + ev.error + "）");
          }
        }
      }
      if (!acc) showBubble("脑子没回话…检查一下后端哦");
    } catch (e) {
      // 回退：旧非流式接口
      try {
        const r2 = await fetch(API + "/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
        const j = await r2.json();
        if (j.ok) say(j.reply, j.tts);
        else showBubble("我这边连不上脑子（" + (j.error || "LLM未就绪") + "）");
      } catch (e2) { showBubble("后端没起，我晕过去了…（双击桌面「start_speaktosi.bat」）"); }
    }
  }

  // ---------- 输入条 ----------
  const bar = document.createElement("div");
  bar.style.cssText = "position:absolute;left:0;right:0;bottom:0;display:none;background:rgba(12,14,20,.92);padding:8px 10px;border-top:1px solid rgba(212,175,55,.4);";
  bar.innerHTML = '<input id="tinp" placeholder="和 SpeakTosi 说点什么… (Enter 发送, Esc 收起)" style="width:100%;box-sizing:border-box;padding:8px 10px;border-radius:8px;border:1px solid #555;background:#1b1e26;color:#eee;font:14px \'Microsoft YaHei\',sans-serif;outline:none;">';
  stage.appendChild(bar);
  const tinp = bar.querySelector("#tinp");
  tinp.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if (e.key === "Enter" && tinp.value.trim()) { const t = tinp.value.trim(); tinp.value = ""; sendChat(t); bar.style.display = "none"; }
    if (e.key === "Escape") bar.style.display = "none";
  });
  tinp.addEventListener("mousedown", (e) => e.stopPropagation());
  function toggleBar() {
    if (bar.style.display === "none") { bar.style.display = "block"; tinp.focus(); }
    else bar.style.display = "none";
  }
  // 隐藏后恢复提示（右下角小字，4秒自动消失）
  let tipEl = null;
  function showTip(msg) {
    if (!tipEl) { tipEl = document.createElement("div");
      tipEl.style.cssText = "position:absolute;right:10px;bottom:14px;z-index:9;padding:5px 10px;background:rgba(16,18,24,.8);color:#d4af37;border-radius:8px;font:12px 'Microsoft YaHei',sans-serif;pointer-events:none;transition:opacity .4s";
      stage.appendChild(tipEl); }
    tipEl.textContent = msg || "再双击这里可恢复 SpeakTosi";
    tipEl.style.opacity = "1";
    setTimeout(() => { if (tipEl) tipEl.style.opacity = "0"; }, 3500);
  }

  // ---------- 点击 / 拖拽移动 / Ctrl拖拽缩放 ----------
  let dragging = false, downXY = null, moved = 0, clickN = 0, clickT = null, zoomDrag = false, zoomAcc = 0;
  teacherWrap.addEventListener("mousedown", (e) => {
    downXY = { x: e.screenX, y: e.screenY }; moved = 0; dragging = true;
    zoomDrag = !e.ctrlKey;  // 普通拖拽=调大小（人物锚右下角：往左上拖=变大，往右下推=变小）；Ctrl+拖拽=移动
    zoomAcc = 0;
    const mm = (ev) => {
      const dx = ev.screenX - downXY.x, dy = ev.screenY - downXY.y;
      moved = Math.max(moved, Math.abs(dx) + Math.abs(dy));
      downXY = { x: ev.screenX, y: ev.screenY };
      if (moved <= 6) return;
      if (zoomDrag) {
        // 对角位移 dx+dy：负=往左上拖=放大，正=往右下推=缩小；每累计30px一档
        zoomAcc += dx + dy;
        if (Math.abs(zoomAcc) >= 30) {
          const d = zoomAcc < 0 ? 0.05 : -0.05;
          zoomAcc = 0;
          if (window.teacher) teacher.zoom(d);
        }
      } else if (window.teacher) teacher.drag(dx, dy);
    };
    const mu = () => {
      dragging = false; zoomDrag = false; window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu);
      if (moved > 6) return;
      if (DND) { if (window.teacher) teacher.summon(); return; }  // mini: 单击=召唤(Owen习惯不变)
      clickN = (clickN || 0) + 1;
      clearTimeout(clickT);
      clickT = setTimeout(() => {
        if (clickN >= 2) {  // 双击=隐藏人物图像(Owen认可交互，再双击恢复)
          const hid = teacherWrap.style.opacity === "0";
          teacherWrap.style.opacity = hid ? "1" : "0";   // opacity 不吞点击→原地双击可恢复
          if (!hid) { bar.style.display = "none"; bubble.style.display = "none"; }
          else showTip();
        }
        else if (!DND) toggleBar();                                   // full单击=输入条
        clickN = 0;
      }, 260);
    };
    window.addEventListener("mousemove", mm); window.addEventListener("mouseup", mu);
  });
  // 滚轮缩放（在人物身上滚动=调大小；Shift+滚轮=调气泡文字宽度）
  window.addEventListener("wheel", (e) => {
    if (e.shiftKey) {
      BUBBLEW = Math.min(100, Math.max(20, BUBBLEW + (e.deltaY < 0 ? 4 : -4)));
      applyBubbleW();
      if (window.teacher && teacher.setBubbleW) teacher.setBubbleW(BUBBLEW);
      showTip("气泡宽度 " + BUBBLEW + "%");
      return;
    }
    if (!window.teacher) return;
    teacher.zoom(e.deltaY < 0 ? 0.05 : -0.05);
  }, { passive: true });
  // 窗口被主进程重摆(缩放)后→重排五官贴片/命中区
  window.addEventListener("resize", () => layout());

  // ---------- 语音输入（后端 STT 可用才显示麦克风） ----------
  let rec = null;
  (async () => {
    try {
      const h = await (await fetch(API + "/health")).json();
      if (h.stt) micBtn.style.display = "block";
    } catch {}
  })();
  let mediaEl = null, proc = null, chunks = [], recCtx = null, recording = false, recSession = 0;
  micBtn.addEventListener("mousedown", async (e) => {
    e.stopPropagation();
    if (recording) return;
    recording = true; micBtn.classList.add("rec");
    const mySession = ++recSession;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1 } });
      if (mySession !== recSession) { stream.getTracks().forEach(t => t.stop()); return; }  // 已松开→丢弃,防悬挂
      recCtx = new (window.AudioContext || window.webkitAudioContext)(); await recCtx.resume();
      const src = recCtx.createMediaStreamSource(stream);
      proc = recCtx.createScriptProcessor(4096, 1, 1);
      chunks = [];
      proc.onaudioprocess = (ev) => { chunks.push(new Float32Array(ev.inputBuffer.getChannelData(0))); };
      // 零增益节点隔离：ScriptProcessor必须连到destination才触发回调,但gain=0杜绝麦克风外放回音
      const mute = recCtx.createGain(); mute.gain.value = 0;
      src.connect(proc); proc.connect(mute); mute.connect(recCtx.destination);
      rec = stream;
    } catch (err) { recording = false; micBtn.classList.remove("rec"); showBubble("麦克风不可用: " + err.message); }
  });
  micBtn.addEventListener("mouseup", async (e) => {
    e.stopPropagation();
    if (!recording) return;
    recording = false; recSession++;   // 会话失效→在途getUserMedia自动丢弃
    micBtn.classList.remove("rec");
    try {
      if (!proc || !chunks.length) { showBubble("按久一点再松开哦（至少半秒）"); cleanupRec(); return; }
      proc.disconnect();
      const rate = recCtx.sampleRate;
      let total = 0; for (const c of chunks) total += c.length;
      const pcm = new Float32Array(total); let off = 0;
      for (const c of chunks) { pcm.set(c, off); off += c.length; }
      const wav = encodeWav(pcm, rate);
      const secs = total / rate;
      cleanupRec();
      if (secs < 0.4) { showBubble("按久一点再松开哦（至少半秒）"); return; }
      showBubble("🎙 听写中…");
      const fd = new FormData(); fd.append("file", new Blob([wav], { type: "audio/wav" }), "say.wav");
      const j = await (await fetch(API + "/stt", { method: "POST", body: fd })).json();
      if (j.ok && j.text) sendChat(j.text);
      else showBubble("没听清呢…再说一次？");
    } catch (err) { cleanupRec(); showBubble("语音识别失败: " + err.message); }
  });
  function cleanupRec() {
    try { if (rec) rec.getTracks().forEach(t => t.stop()); } catch {}
    try { if (recCtx && recCtx.state !== "closed") recCtx.close(); } catch {}
    rec = null; recCtx = null; proc = null; chunks = [];
  }
  // 自动化测试钩子：程序化驱动按住说话全流程（真实麦克风）
  window.__micTest = async (ms) => {
    const md = (el, type) => el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
    md(micBtn, "mousedown");
    await new Promise(r => setTimeout(r, ms || 2000));
    md(micBtn, "mouseup");
    await new Promise(r => setTimeout(r, 8000));   // 等STT+sendChat完成
    return JSON.stringify({ bubble: bubble.textContent, bubbleShown: bubble.style.display,
                            recLeft: !!(window.rec || rec), micRec: micBtn.classList.contains("rec") });
  };
  // 文字输入测试钩子
  window.__typeTest = async (txt) => {
    toggleBar();
    tinp.value = txt;
    tinp.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await new Promise(r => setTimeout(r, 12000));
    return JSON.stringify({ bubble: bubble.textContent, bubbleShown: bubble.style.display });
  };
  function encodeWav(pcm, rate) {
    const d = new DataView(new ArrayBuffer(44 + pcm.length * 2));
    const ws = (o, s) => { for (let i = 0; i < s.length; i++) d.setUint8(o + i, s.charCodeAt(i)); };
    ws(0, "RIFF"); d.setUint32(4, 36 + pcm.length * 2, true); ws(8, "WAVE"); ws(12, "fmt ");
    d.setUint32(16, 16, true); d.setUint16(20, 1, true); d.setUint16(22, 1, true);
    d.setUint32(24, rate, true); d.setUint32(28, rate * 2, true); d.setUint16(32, 2, true); d.setUint16(34, 16, true);
    ws(36, "data"); d.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) d.setInt16(44 + i * 2, Math.max(-1, Math.min(1, pcm[i])) * 32767, true);
    return d.buffer;
  }

  // ---------- 校准模式：拖框+缩放，保存后自动退出 ----------
  function startCalib() {
    const cal = $("calib"); cal.style.display = "block";
    cal.innerHTML = '<div style="position:absolute;top:4px;left:8px;color:#ff5c8a;font:12px monospace;z-index:9">校准模式：拖动=移动框，右下角圆点=缩放 | Save 保存并退出 | Esc 放弃</div>';
    const save = document.createElement("button");
    save.textContent = "Save"; save.style.cssText = "pointer-events:auto;pointer-events:auto;position:absolute;top:24px;left:8px;z-index:9;padding:4px 14px;background:#d4af37;border:0;border-radius:6px;font-weight:bold;cursor:pointer";
    save.onclick = async () => {
      save.textContent = "保存中…";
      try {
        const body = JSON.stringify({ natural: NAT, ...LMD });
        let r = await fetch(API + "/landmarks", { method: "POST", headers: { "Content-Type": "application/json" }, body });
        if (!r.ok) throw new Error("landmarks HTTP " + r.status);
        r = await fetch(API + "/calibflag", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: false }) });
        if (!r.ok) throw new Error("calibflag HTTP " + r.status);
        save.textContent = "✓ 已保存"; location.reload();
      } catch (e) {
        save.textContent = "保存失败(重试)";
        alert("保存失败：" + (e && e.message || e) + " | 后端(8765)没起？恢复后重试；按 Esc 放弃校准。");
      }
    };
    save.style.pointerEvents = "auto"; cal.appendChild(save);
    for (const k of CAL_KEYS) {
      const b = LMD[k];
      const box = document.createElement("div"); box.className = "calbox"; box.style.pointerEvents = "auto"; save && 0;
      box.style.cssText += `left:${b[0] * SCALE}px;top:${b[1] * SCALE}px;width:${(b[2] - b[0]) * SCALE}px;height:${(b[3] - b[1]) * SCALE}px;`;
      box.innerHTML = `<label>${k}</label>`;
      const hnd = document.createElement("div");
      hnd.style.cssText = "position:absolute;right:-6px;bottom:-6px;width:13px;height:13px;border-radius:50%;background:#ff5c8a;cursor:nwse-resize";
      box.appendChild(hnd);
      let mode = null, start = null;
      box.addEventListener("mousedown", (e) => { mode = e.target === hnd ? "resize" : "move"; start = { x: e.clientX, y: e.clientY, b: [...b] }; e.preventDefault(); });
      window.addEventListener("mousemove", (e) => {
        if (!mode) return;
        const dx = e.clientX - start.x, dy = e.clientY - start.y;
        let nb = [...start.b];
        if (mode === "move") { nb[0] += dx / SCALE; nb[1] += dy / SCALE; nb[2] += dx / SCALE; nb[3] += dy / SCALE; }
        else { nb[2] += dx / SCALE; nb[3] += dy / SCALE; }
        LMD[k] = nb;
        box.style.left = nb[0] * SCALE + "px"; box.style.top = nb[1] * SCALE + "px";
        box.style.width = (nb[2] - nb[0]) * SCALE + "px"; box.style.height = (nb[3] - nb[1]) * SCALE + "px";
      });
      window.addEventListener("mouseup", () => { mode = null; });
      cal.appendChild(box);
    }
    window.addEventListener("keydown", (e) => { if (e.key === "Escape") { fetch(API + "/calibflag", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on: false }) }).then(() => location.reload()); } });
  }

  // ---------- 托盘唤起 / 唤醒问候 ----------
  if (window.teacher) {
    teacher.onWake(() => { toggleBar(); });
    teacher.onSay((t, u) => say(t, u));
    const apply = (m) => { DND = m === 'mini'; HIDDEN = m === 'hidden'; window.__DND = DND; teacherWrap.style.visibility = "visible"; if (m !== 'full') { bar.style.display = "none"; bubble.style.display = "none"; } layout(); };
    teacher.getMode().then(apply);
    teacher.onMode(apply);
  }

  // ---------- 启动 ----------
  img.onload = () => { if (img.naturalWidth) { NAT = { w: img.naturalWidth, h: img.naturalHeight }; } layout(); sampleSkin(); };
  (async () => {
    // 用户缩放系数 + 气泡宽度从主进程配置读回（重启后保持）
    if (window.teacher && teacher.cfg) {
      try {
        const c = await teacher.cfg();
        USERSCALE = Math.min(1.5, Math.max(0.6, c.scale || 1));
        if (c.bubbleW) { BUBBLEW = Math.min(100, Math.max(20, c.bubbleW)); }
      } catch {}
    }
    applyBubbleW();
    loadConfig();
  })();
})();
