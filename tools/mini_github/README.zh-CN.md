# SpeakTosi Mini · 两位 AI 英语老师

> **一个跑在你自己电脑上的 AI 英语私教**：真人质感的口型数字人 + 一对一实时对话 + 会话复盘。
> 本仓库是 **Mini 简化版** —— 只装两位老师：**Coach Arthur**(睿智博学导师 📚) 与 **Coach Mia**(灵动艺术博主 🎨)。

<p align="center">
  <img src="avatar/Coach-Arthur.png" width="45%" alt="Coach Arthur" />
  <img src="avatar/Coach-Mia.png" width="45%" alt="Coach Mia" />
</p>

<p align="center">🌐 官网 <b><a href="https://www.speaktosi.com">www.speaktosi.com</a></b> · 定制专属 AI 老师形象 / 克隆声音 / 陪练课程</p>

**中文说明** · [English →](README.md)

---

## 这个版本有什么

| | |
|---|---|
| 🎓 **两位老师** | Arthur(英式文学导师，深挖长句与思辨) / Mia(生活风博主，练日常表达与情绪细节) |
| 🗣 **真人系口型** | MuseTalk 实时口型驱动，说英语时嘴型跟着走(不是波浪动画) |
| 🔊 **克隆音色** | CosyVoice2 本地克隆，每位老师一套固定音色(不串音) |
| 💬 **实时对话** | 语音唤醒 → 识别 → 大模型回答 → 说话，全程本地编排，只有"想内容"这一步走云端 |
| 📝 **会话复盘** | 一次对话结束给一份 SESSION REVIEW：做对什么 / 一句话怎么说得更好 / 下一步练什么 |
| 🧠 **长期记忆** | 记住学生的水平、兴趣、上次聊到哪(全部存在本机) |
| 🖥 **桌面形态** | Electron 桌面老师窗口 + 托盘，随时开聊；支持"出现在桌面上"的悬浮窗口 |
| 🎚 **回答长度可调** | 设置页五档：不限 / 长回答(200词) / 标准(120词·默认) / 简答(80词) / 极简(50词)；改完 30 秒内热生效，不用重启 |

> ⚠️ 本版**只含这两位老师**。原始开发版共有 13 位老师(含卡通系)，本仓库把其余 11 位的
> 形象包、待机序列、人设、音色与整族卡通素材裁掉了：包更小、冷启动更快、物料一致。
> 需要完整 13 师阵容：用 `tools/package_share.py --lowmem` 重新打包(见 [docs/zh-CN/PACKAGING.md](docs/zh-CN/PACKAGING.md))。

---

## 快速开始

### 路线 A：零配置(推荐给"只想用")

零配置自包含版 `SpeakTosi_Mini_*.zip`（约 25 GB，自带全部模型 + Python 运行环境 + Electron 依赖 + 克隆语音环境）**体积超出 GitHub 单文件 2 GB 上限，因此不在 Releases 提供** —— 需要开箱即用版的同事/机构，请通过官网 <https://www.speaktosi.com>（域名与备案办理中）联系我们获取；拿到包后照下面走：

```text
1) 解压到任意【英文、无空格】路径，例如 D:\SpeakTosi
2) 双击 start_speaktosi.vbs        ← 首次会静默自愈一次包内绝对路径
3) 首启弹【首次配置】向导：选平台 → 粘贴自己的云端 Key → 「保存并测试」
4) 托盘图标变绿即可开聊（冷启动 1~3 分钟，之后热启动约 10 秒）
```

排障时先双击 `SETUP.bat` 看自检输出。详细步骤见 [docs/zh-CN/INSTALL.md](docs/zh-CN/INSTALL.md)。

### 路线 B：从源码装(给要改代码的人)

```bash
git clone <this-repo> && cd SpeakTosi-Mini

# 1) 前端 Electron
cd app && npm install && cd ..

# 2) 数字人引擎(8011)
python -m venv engine/venv
engine/venv/Scripts/pip install -r engine/requirements.txt     # Windows

# 3) 后端(8765)
python -m venv app/backend/.venv
app/backend/.venv/Scripts/pip install -r app/backend/requirements-backend.txt

# 4) 克隆语音(18091/18092)：需要 Voice-Pro 运行环境，见 docs/zh-CN/ASSETS.md
# 5) 取模型与形象素材：见 docs/zh-CN/ASSETS.md（二进制不进 git）
```

---

## 硬件与端口

| 项目 | 要求 |
|---|---|
| 系统 | Windows 10 / 11 x64 |
| GPU | NVIDIA，显存 **≥ 12 GB**(推荐 16 GB)。MuseTalk 单引擎 ≈ 9 GB，克隆语音 ≈ 2 GB/实例 |
| 内存 | **≥ 16 GB**(推荐 32 GB) |
| 磁盘 | 自包含包解压后 ≈ 25 GB，另留 20 GB 给会话/缓存/日志 |

| 端口 | 用途 |
|---|---|
| **8011** | 数字人引擎(MuseTalk 口型) |
| 18091 / 18092 | 克隆语音(CosyVoice2，主/副 worker) |
| 18999 | 看门狗单实例锁 |
| 8765 | 后端 API(Electron 界面的唯一入口) |

> 本 Mini 版**没有 8010 端口** —— 原卡通系 viseme 引擎已裁掉，8011 兼任"桌面窗口"的渲染。

---

## 目录结构

```text
app/                Electron 桌面应用
  main.js           主进程：托盘、窗口、模式切换
  renderer/         界面层：overlay(老师的窗口)、settings(设置)、setup_wizard(首启向导)
  backend/          FastAPI 后端(8765)：/avatars /voices /config /persona /chat …
engine/             数字人引擎(LiveTalking 分支)
  app.py            引擎入口(WebRTC 推流)
  aiteacher_watchdog.py  看门狗：按机器档位拉起/守护全部服务
  avatars/          musetalk_avatar.py(真人系口型) / viseme_avatar.py(卡通系，本档不启用)
  llm.py            对话编排(接云端兼容 OpenAI 协议的模型)
  data/             形象注册表、音色注册表、语速标定、待机帧序列
  models/           口型/ASR 权重(不入 git)
audiocpp/webui/     音频工具(ffmpeg) + 参考音色 wav
avatar/             老师人设(avatar/persona/*.md)、立绘、参考音
runtime/            内置 Python 运行环境(自包含包才有)
tools/              打包器、首次安装自愈、维护脚本
docs/               文档(英文为默认；中文版在 docs/zh-CN/)
```

---

## 文档索引

| 文档 | 内容 |
|---|---|
| [docs/zh-CN/INSTALL.md](docs/zh-CN/INSTALL.md) | 三条安装路线 + 首启向导 + 常见安装问题 |
| [docs/zh-CN/ARCHITECTURE.md](docs/zh-CN/ARCHITECTURE.md) | 组件、数据流、端口、关键文件、为什么这样裁剪 |
| [docs/zh-CN/TEACHERS.md](docs/zh-CN/TEACHERS.md) | Arthur / Mia 的规格与人设文件；怎么加第三位老师 |
| [docs/zh-CN/ASSETS.md](docs/zh-CN/ASSETS.md) | 模型与形象素材清单、获取方式、LFS 说明 |
| [docs/zh-CN/TROUBLESHOOTING.md](docs/zh-CN/TROUBLESHOOTING.md) | 端口占用、显存不足、TTS 假死、搬家路径、venv 崩 |
| [docs/zh-CN/PACKAGING.md](docs/zh-CN/PACKAGING.md) | `--mini` / `--mini-code` / `--lowmem` 怎么打、自检项、怎么回滚 |
| [docs/zh-CN/PRIVACY.md](docs/zh-CN/PRIVACY.md) | 哪些数据留在本机、哪些会发到云端 |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 提 PR / 报 Bug 的方式 |

> 文档默认英文（`docs/`，面向国际用户）；中文版在 [`docs/zh-CN/`](docs/zh-CN/)。

---

## 我自己打包一份

```powershell
# 自包含 Mini 版(只两位老师，解压即用)
python tools\package_share.py --mini

# GitHub 仓库档(只有代码 + 文档，不含模型/运行时/形象包)
python tools\package_share.py --mini-code --tree dist\SpeakTosi-Mini-2Teachers

# 13 师完整版(原始阵容，含卡通系)
python tools\package_share.py --lowmem
```

打包器会在最后跑自检：只两位老师、无他师资产、音色两位、关键文件齐全、无密钥入库。
裁剪规则集中在 [`tools/mini_edition.py`](tools/mini_edition.py)，每条都有实测/源码依据。

---

## 关于 SpeakTosi 与定制服务

SpeakTosi 是**可定制的 AI 英语私教**：真人质感的口型数字人 + 一对一实时对话 + 会话复盘。

- 本仓库 = **开源 Mini 版**（仅 Arthur / Mia 两位老师），免费自行部署。
- 想要**自己形象的 AI 老师**、**专属克隆声音**、**贴合行业/考试的课程脚本**，或整机部署与二次开发支持：
  🌐 官网 **<https://www.speaktosi.com>**（域名与备案办理中，即将上线；也可直接在本仓库开 Issue 留言）。
- 定制形象与音色素材不在 Mini 版内 —— 定制服务与更多老师的版本仅在官网提供。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

致谢：MuseTalk、CosyVoice2、LiveTalking、whisper、Electron 与 FastAPI 社区。