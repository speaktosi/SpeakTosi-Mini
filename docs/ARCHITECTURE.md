# 架构说明(1020mini)

本文描述 Mini 版实际跑的拓扑、每个进程干什么、关键文件在哪，以及**为什么这样裁剪**。
文中数字来自 1020 打包期间的逐进程/逐源码取证。

---

## 1. 进程拓扑

```text
┌────────────────────────────┐
│ Electron 桌面 app           │  老师窗口(overlay) / 设置 / 首启向导
│ app/main.js + renderer/     │  托盘：状态灯、启动/修复、模式切换
└──────────────┬─────────────┘
               │ HTTP :8765  (唯一接口层)
┌──────────────▼─────────────┐        ┌──────────────────────────┐
│ 后端 FastAPI                │        │ 看门狗(常驻)              │
│ app/backend/server.py       │        │ engine/aiteacher_watchdog │
│ /avatars /voices /config    │        │ 单实例锁 :18999            │
│ /persona /chat /tts …       │  ←──→  │ 按档位拉起 + 守护 + 健康探测 │
└──────────────┬─────────────┘        └───────┬──────────────────┘
               │                              │ 拉起
               │                   ┌──────────┴───────────┐
               │                   │ ① 数字人引擎 :8011    │  MuseTalk 实时口型
               │                   │    (engine/app.py)    │  WebRTC 推流
               │                   ├──────────────────────┤
               │                   │ ② 克隆语音 :18091     │  CosyVoice2 主 worker
               │                   │  ③ 克隆语音 :18092    │  副 worker(full 档才起)
               │                   └──────────────────────┘
               │
        对话文本走云端 LLM(OpenAI 兼容协议，用自己的 Key)
```

- **界面只跟 8765 说话**；8765 再决定用哪台引擎、哪个音色。
- **看门狗是唯一"拉起服务"的人**：它探测显存/内存决定运行档位，然后守护进程；
  服务挂了会被重拉，所以"手动杀进程"通常会被它拉回来(排障见 TROUBLESHOOTING)。
- 引擎自己也是 HTTP 服务：`:8011/` 是首页(就绪指示)，`POST /offer` 是 WebRTC 建连端点。

## 2. 一次对话的数据流

```text
学生说话
  → 唤醒词/按键触发录音
  → 语音识别(faster-whisper，随包自带)
  → 老师人格(soul) + 记忆 + 本轮文本 → 云端大模型(qwen3.8-flash 等)  ← 唯一走网络的一步是"想内容"
  → 老师回复文本
  → 克隆语音合成(CosyVoice2 :18091，音色=该老师的 preset，带语速/基频标定)
  → 音频送入数字人引擎 :8011 → MuseTalk 生成口型帧
  → WebRTC 推到 Electron 窗口播放(口型 + 声音同步)
  → 对话落盘到本机(会话/记忆)，可选生成 SESSION REVIEW 复盘
```

**老师切换 = 换魂**：`/teacher/apply` 会按所选老师重建 `app/backend/config/persona.json`
(System prompt 由 `avatar/persona/<老师>.md` 正文 + `Shared-core-adult.md` 通用规范拼成)，
同时切音色 preset；引擎侧按 `engine` 字段路由到对应引擎。

## 3. 关键文件对照表

| 关注点 | 文件 | 说明 |
|---|---|---|
| 老师注册表 | `app/backend/server.py` 的 `AVATARS` | Mini 版只剩 `coach_arthur` / `coach_mia` 两条 |
| 老师人设(soul) | `avatar/persona/CoachArthur.md`、`CoachMia.md`、`Shared-core-adult.md` | `[字段]头 + 正文`，正文即 System prompt 主体 |
| 当前生效灵魂 | `app/backend/config/persona.json` | 由上面三个文件渲染生成(切换老师时重写) |
| 出厂配置 | `app/backend/config/app.json` | 默认老师、音色、云端 Key、唤醒词 |
| 引擎与守护 | `engine/aiteacher_watchdog.py` | 服务表 `SERVICES`、档位自动判定、深探 |
| 引擎入口 | `engine/app.py` | 加载形象包、读待机序列、`/offer` 建连 |
| 口型实现 | `engine/avatars/musetalk_avatar.py` | MuseTalk 真人系口型 |
| 音色注册 | `engine/data/voice_registry.json` | preset → 参考音 wav + 参考文本(Mini 版：arthur / mia) |
| 语速与音色标定 | `engine/data/tts_calibration.json` | 目标语速、seed/temperature/f0/容差(治克隆音色漂移) |
| TTS 模板 | `tools/scripts/tts_demo_voice.json` | 看门狗每次启动据此重渲 `engine/data/tts_runtime.json` |
| 形象包 | `engine/data/avatars/<id>/` | latents/coords/mask/full_imgs 等(约 80~115 MB/位) |
| 待机序列 | `engine/data/customvideo/<id>_idle/*.png` | 引擎启动即读；Mini 版保留 Arthur 的(约 85 MB) |
| 渲染源挂载 | `engine/data/custom_config_<id>.json`、`engine/data/custom_config.json` | 告诉引擎用哪套图序 |
| 参考音 | `audiocpp/webui/voice/<voice>_ref.wav` | 克隆音色的"声源"(Mini 版：arthur_ref / mia_ref) |
| 音色锚点 | `engine/data/voice_anchor_ref_<voice>_ref.wav_*.npz` | 句首对齐用的声学锚点 |
| 运行档位记录 | `engine/data/runtime_mode_active.json` | 看门狗写，后端 `/health` 读来回报界面 |

## 4. 运行档位(看门狗自动判定)

| 档位 | 起什么 | 实测显存(原始 13 师版) | 适用 |
|---|---|---|---|
| `full` | 数字人引擎 + **双** TTS worker | ≈ 13 GB | 内存 ≥ 29 GB 且显存 ≥ 16 GB |
| `lite` | 数字人引擎 + **单** TTS worker | ≈ 10.8 GB | 内存 16~28 GB 或显存 11~15 GB |
| ~~`ultra`~~ | ~~只跑卡通引擎~~ | — | **Mini 版不适用**(卡通引擎已裁) |

Mini 版的实测口径：MuseTalk 约 9 GB + 单 TTS 约 2 GB ≈ **11 GB 显存**。
Mini 版把 `ultra` 请求**钳到 `lite`**(原定义"只跑卡通系引擎"在本档没有对象)。
自动判定写在 `engine/aiteacher_watchdog.py`，结果落到 `runtime_mode_active.json`，设置页可看可改。

## 5. Mini 版裁了什么、为什么

| 裁掉的东西 | 体量 | 依据 |
|---|---|---|
| 其余 11 位老师的形象包 | ~1.1 GB | 只有 Arthur/Mia 在 `AVATARS` 里；形象包不注册就不该在包内 |
| 其余老师的待机序列 / 人设 / 参考音 / 音色锚点 / 音色 preset | ~0.5 GB | 同上；`tts_runtime.json` 由剪过的模板重渲 → 界面音色恰好两位 |
| **卡通系 viseme 引擎(8010)整块** | 引擎进程 + 素材 ~0.4 GB | 本档只有真人系老师。viseme 的嘴型是**共享卡通图层**(`avatar/viseme/*.png`)贴到待机帧上，真人底图贴卡通嘴 = 五官错位；且 1017 实录过 musetalk 素材喂 viseme 引擎会卡死 |
| 旧本地 Qwen3-TTS 服务残留(`audiocpp/{models,gpu,venv,cpu}`) | ~7.7 GB | 1015 已退役，运行时根本不加载(低配版沿用同一裁剪) |
| 个人状态：history/sessions、五官标定、窗口位置、`.env` | — | 出厂纯净化：装机后从零开始，云 Key 由首启向导填自己的 |

**为什么 8011 兼做"桌面窗口"**：`engine/web/desktop.html`(托盘"老师出现在桌面")建连时发的是
**不带 avatar 参数**的 `/offer`，渲染的是引擎**启动主位**。Mini 版把主位设成 `coach_arthur`,
于是那个窗口出现的就是本档老师本人，而不是一位不存在的卡通老师。

**深探陷阱(必改项)**：看门狗用"真合成一句话"来判 TTS 是否活着(只探端口会被"端口在但不干活"骗过)。
这句探活的音色若仍写 `lingxue`，而音色已被裁掉 → 18091 永远判"卡死" → 反复被杀，形成死循环。
Mini 版把探活音色改成 `arthur`。

## 6. 纯净与安全(所有打包档强制)

- 会话/记忆：`history.json` / `sessions.json` 清空；`persona.json` 复位为出厂老师。
- 个人标定：`landmarks.json`、`.calib`、`app/config/electron.json` 一律不入包。
- 运行时状态：`tts_endpoints.json`、`runtime_mode_active.json`、`tts_runtime.json` 不入包，启动时重建。
- 密钥：`*.env` 全排除 + 打包前全树嗅探(命中即拒入包)；出厂 `app.json` 的 `llm_api_key` 置空。
- 搬家自愈：包内绝对路径由 `tools/scripts/first_run_setup.py` → `portable_paths.py` 在首启时改写为当前根目录。