# 安装与首次启动

## 0. 先选路线

| 你的情况 | 走哪条 | 需要多久 |
|---|---|---|
| 只想用，不想碰代码 | **A. 自包含包**(Release 的 `SpeakTosi_Mini_*.zip`) | 解压 10 分钟 + 冷启动 1~3 分钟 |
| 要改代码/提 PR | **B. 源码 + 依赖自装** | 装依赖 30~60 分钟 |
| 包坏了/搬家了/换机了 | **C. 自愈修复** | 1 分钟 |

---

## A. 自包含包(推荐)

```text
1) 解压到【英文、无空格】路径，例如 D:\SpeakTosi
   ⚠️ 不要放 C:\Program Files、桌面(可能含中文/空格)、OneDrive 同步目录
2) 磁盘：解压后约 25 GB，另请留 20 GB 给会话/日志/缓存
3) 双击 start_speaktosi.vbs        ← 首次会静默做一次"路径自愈"
   排障时改用：先双击 SETUP.bat 看自检输出，再双击 start_speaktosi.vbs
4) 首次会弹【首次配置】向导：
     选平台 → 粘贴自己的云端 Key → 「保存并测试」→ 看到"云端已连通"即完成
5) 等托盘图标变绿(冷启动 1~3 分钟，要加载数字人 + 克隆语音模型) → 开聊
```

以后要改 Key 或换运行档位：**桌面老师窗口 → 右键托盘 → 设置**。

## B. 源码档

```bash
# 1) 前端
cd app && npm install && cd ..

# 2) 数字人引擎(8011)
python -m venv engine/venv
engine/venv/Scripts/pip install -r engine/requirements.txt

# 3) 后端(8765)
python -m venv app/backend/.venv
app/backend/.venv/Scripts/pip install -r app/backend/requirements-backend.txt

# 4) 模型与形象素材 → 见 docs/ASSETS.md(二进制不在仓库里)
# 5) 克隆语音环境(18091/18092) → 见 docs/ASSETS.md 第 4 节
```

装完双击 `SETUP.bat` 自检，再 `start_speaktosi.vbs`。

## C. 搬家 / 换机自愈

包内所有绝对路径在**首次启动时自动改写**为当前根目录(实现：`tools/scripts/first_run_setup.py` → `portable_paths.py`)。
所以整包换盘符、换目录、拷给别人都能直接跑。手动触发：

```bat
SETUP.bat      :: 里面就是 runtime\py312\python.exe tools\scripts\first_run_setup.py
```

`first_run_setup.py` 做四件事：
1. 修 `engine/venv`、`app/backend/.venv` 的 `pyvenv.cfg`(`home` 指向**包内** Python，换机必崩的坑就在这)；
2. 全树改写 `C:\Speaktosi` → 当前根(跳过 venv/node_modules/模型等目录)；
3. 补 `config/runtime_mode.json`(auto 档)；
4. 自检关键文件是否齐全。

---

## 首次配置向导(必做)

云端 Key 是唯一必须由你提供的东西(对话与翻译走云端大模型；其余全在本机)。

- 打开方式：首启自动弹；或托盘 → 设置 → 顶部「云端 Token」。
- 支持"选平台 → 贴 Key → 保存并测试"，显示"云端已连通"才算成功。
- Key 存在 `app/backend/.env`(本机文件，不入包、不进 git)。手工方式(排障用)：
  复制 `config/student_keys.example.env` → `app/backend/.env` 再填。

## 启动之后怎么确认它真的活着

| 看什么 | 正常表现 |
|---|---|
| 托盘图标 | 变绿 |
| `engine/data/runtime_mode_active.json` | 有内容(`mode` = `full` 或 `lite`) |
| 端口 | `8011`(引擎)、`18091`(语音)、`8765`(后端)、`18999`(看门狗锁)都在 LISTENING |
| 后端 | `http://127.0.0.1:8765/health` 返回 JSON |
| 老师列表 | `http://127.0.0.1:8765/avatars` 只有两位：`coach_arthur`、`coach_mia` |

> ⚠️ `tools/verify_vram.py` 与 `tools/verify_e2e_ui.py` 是**维护者工具**：它们会主动停看门狗、杀掉服务来测显存/端到端，
> 不适合日常体检(见 docs/TROUBLESHOOTING.md)。日常自检用 `SETUP.bat` 与上表的端口/接口检查即可。

## 卸载

删除整个目录即可。除了目录内的会话/日志，不写注册表、不装服务。