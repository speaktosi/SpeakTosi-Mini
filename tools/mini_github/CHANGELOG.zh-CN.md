# 更新日志

本文件记录 **SpeakTosi Mini(两位老师版)** 的发布历史。完整 13 师版本的历史见开发仓库。

## 文档/SEO — 2026-10-22（仅仓库文案，不改功能）

### 文档
- **README 默认语言改为英文**（面向国际用户）：`README.md` 为英文、`README.zh-CN.md` 为中文；
  删除重复内容的 `README.en.md`（重复正文对搜索排名有害）。
- 英文 README 重写为**搜索友好版**：标题含 *Free Open-Source AI English Tutor with a Real-Time
  Talking-Head Teacher*，正文自然覆盖 AI English tutor / AI English coach / MuseTalk talking head /
  spoken English practice / self-hosted 等检索词，并新增 FAQ 段（免费？离线？支持哪些大模型？要什么显卡？）。
- 发布件文档层 `tools/mini_github/` 同步改名，`tools/package_share.py` 的 mini-code 白名单
  由 `README.en.md` 改为 `README.zh-CN.md`（否则下次打包会用旧中文版覆盖仓库 README）。
- GitHub 仓库 About 描述与 topics 按英文关键词重设（description 英文 + 20 个 topic）。

## 1.0.3 — 2026-10-22

### 修复
- **对话中途"突然断链"根治（看门狗把"正忙"误判为"已死"）**：
  Windows 上监听队列满时，新连接返回 `WSAECONNREFUSED / WinError 10061「目标计算机积极拒绝」`，
  与"服务真的死了"**同一个错误码**（Linux 是静默丢包，所以只在 Windows 上发作）。
  旧健康探针据此判死，把正在干活的引擎/TTS `kill` 重启 —— 实测 19:10~19:19 十三分钟内连杀 4 次，
  而被杀前引擎日志仍在 `actual avg final fps:25.x` 正常渲染、TTS 仍在正常合成（有反证）。
  现在改为「**忙不判死**」：探针被拒/返回 503 时，先做端口归属仲裁
  （`netstat` 确认端口仍由本栈进程 `LISTENING` → 判"忙"，不计数、不中断；连续 8 轮仍如此才兜底回收真僵死）。
  同时把 TTS 服务监听队列 5 → **128**、引擎 aiohttp `backlog` → **512**，让"忙"这件事本身少发生。
- **断线重连的迟到回调崩溃**：`app/renderer/overlay.js` 的 `connectionstatechange` 监听读的是可变的 `pc` 变量，
  `teardown()` 把 `pc` 置 `null` 后，旧连接的迟到事件会抛
  `Cannot read properties of null (reading 'connectionState')`（实测每轮误杀都伴随该报错刷屏）。
  现于监听内先取**本连接实例快照**，并忽略已被废弃连接的迟到事件（顺带消除"旧连接误报断线 → 触发多余重连"）。

### 优化
- **冷启动错峰**：副 TTS worker(18092) 由"与首句路径并行冷读权重"改为 **boot 组2**（等 `lx_tts` 健康后再起）。
  - 实测依据：冷启动双 TTS 同时冷读权重各约 **76s**，而热启动（权重已进页缓存）只要约 **20s**。
  - 首句只需要 `lx_tts`（+引擎）；副 worker 排在组2后读的是**同一份已在页缓存里的权重**，
    自身也更快，且不再与首句路径抢磁盘/CPU。
  - 安全性：引擎端点表若已列出 18092 而副尚未就绪，`_tts_pick` 会走备用端点并对失败端点 60s 自动摘除，不会静音。
  - 回滚：`aiteacher_watchdog.py` 里把 `lx_tts2` 的 `boot_group` 改回 `1`。

## 1.0.1 — 2026-10-20

### 新增
- **回答长度五档可调**（设置页 → 「回答长度」）：不限 / 长回答(200 词) / 标准(120 词 · 出厂默认) / 简答(80 词) / 极简(50 词，保开口速度)。
  文字链(`app/backend/server.py`)与语音链(`engine/llm.py`)共用同一份 `_len_rule()`，口径逐字一致（打包自检会比对，防两链漂移）。
  - 修复旧口径缺陷：原先把"≤80 词"与"拆成 1-2 句短回合、说完就停"绑死，实测只产出约 25 词，
    压过了 `avatar/persona/CoachArthur.md` 的 4-8 句人设契约（学生听到的"回答太短")。
  - 现按档位分级：≤60 词 = 极简 1-2 句 / 61-100 词 = 简答 2-4 句 / >100 词 = 标准 3-6 句。实测同题同模型：
    50→19 词、80→33 词、120→53 词、200→79 词、不限→72 词（改前 80 档仅 25 词）。
- 官网入口：设置页底部与 README 提供 **www.speaktosi.com**；`app/main.js` 放行外链（交系统浏览器打开，不再弹空白窗口）。
- 定制服务说明（专属老师形象 / 克隆声音 / 课程脚本）写入 README（中英）与包内安装说明。

### 修复
- **引擎配置路径包内自愈**：`engine/llm.py` 4 处硬编码 `C:\Speaktosi\...\config\app.json` 改为 `_cfg_json_path()`
  —— 包内相对路径优先 → 旧机器绝对路径回退 → 可用 `SPEAKTOSI_APP_JSON` 环境变量覆盖。
  换机/换盘解压后引擎读的一定是本包的配置（此前只靠首次启动改写源码兜底）。
- **音色注册表换机可移植**：`voice_registry.json` 的 `ref_wav` 在其他机器缺失时，读端回退到包内同名参考音；
  写端统一斜杠归一为 `C:/...`（此前硬校验存在性，换机会让 TTS 直接抛错）。

## 1.0.0 — 2026-10-20

首个 Mini 版发布：只装 **Coach Arthur** 与 **Coach Mia** 两位真人系老师。

### 新增
- `tools/package_share.py --mini`：自包含 Mini 档(≈25 GB)，解压即用；产物内含 `BUILD-INFO.txt`(真实文件数/体积/规则指纹)。
- `tools/package_share.py --mini-code --tree`：GitHub 仓库档(只代码 + 文档)。
- `tools/mini_edition.py`：Mini 档裁剪规则(每条带取证依据)，开发机源码零改动，可随时 `--lowmem` 回到原阵容。
- 完整文档集：README(中/英)、INSTALL、ARCHITECTURE、TEACHERS、ASSETS、TROUBLESHOOTING、PACKAGING、PRIVACY，
  以及 `.github/`(issue / PR 模板 + CI)。
- 打包器 Mini 专项自检：他师资产残留、`AVATARS` 收窄、音色数量、persona 生成器与 `server.py` 逐字段比对。

### 变更(相对 13 师开发版)
- **引擎拓扑**：裁掉卡通系 viseme 引擎(8010)。Mini 版只有 MuseTalk(8011) 一个引擎，
  并兼任 `engine/web/desktop.html`("老师出现在桌面")窗口的宿主；桌面窗口/状态灯/维护脚本的端口引用同步改为 8011。
  - 依据：viseme 的嘴型是共享卡通图层(`avatar/viseme/*.png`)贴到待机帧上，真人底图贴卡通嘴会五官错位；
    且此前实测过 musetalk 素材喂 viseme 引擎会卡死。
- **引擎启动主位**：`aiteacher_demo`(演示老师) → `coach_arthur`，音色 `default` → `arthur`。
  这样"不带 avatar 参数"的建连(桌面窗口)渲染的是本档老师，而不是已裁掉的演示老师。
- **TTS 深探音色**：`lingxue` → `arthur`。看门狗用"真合成一句话"判 TTS 存活；
  音色被裁掉却仍按旧音色探活，会导致 18091 永远被判"卡死"→ 反复被杀(死循环)。
- **运行档位**：`ultra`(定义为"只跑卡通引擎")在本档无意义 → 请求 `ultra` 时按 `lite` 跑。
- **出厂默认**：默认老师 = `coach_arthur`、音色 = `arthur`、唤醒词去掉个人化词条、云端 Key 置空。
- **素材**：其余 11 位老师的形象包、待机序列、人设、参考音、音色锚点与 13 师名册文档不入包。

### 移除(相对开发仓库)
- 旧本地 Qwen3-TTS 服务残留(`audiocpp/{models,gpu,venv,cpu}`，≈7.7 GB，1015 已退役不被加载)。
- 开发期交接报告(`docs/SpeakTosi_1019_*.md`)与 13 师名册(`docs/AI_COACHES_PROFILES.md`)。
- 个人状态：会话/记忆、五官标定、窗口位置、`*.env`。

### 已知限制
- 仅 Windows x64 + NVIDIA 显卡；显存 < 11 GB 时无可退档位(本档没有卡通引擎)。
- 自包含包 ≈25 GB，网盘分发请注意上传时间。
- 无自动更新机制。

---

## 上游沿革(摘要)

| 日期 | 里程碑 |
|---|---|
| 10-06 | 双引擎架构：卡通系(viseme) / 真人系(MuseTalk)分流 |
| 10-13 | 音色恒定化：CosyVoice2 克隆 + 逐位合成参数(seed/temperature/f0/容差)治音色漂移 |
| 10-15 | 多老师音色统一到 18091 单口(删本地 Qwen3-TTS，省 5~6 GB 显存) |
| 10-16 | 双 TTS worker 并行预取，治"说两句就卡" |
| 10-17/18 | 冷启动顺序修正 + 并行启动组；12 GB 显存自适应；启动加速 |
| 10-19 | 主程序自包含化 + 打包分享(share / full / lowmem 三档) |
| 10-20 | **Mini 两位老师版**（本版） |
