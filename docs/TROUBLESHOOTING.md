# 排障手册

先看这里：**托盘图标是什么颜色**。
绿 = 全栈就绪；灰/红 = 看门狗没把服务拉全(常见原因是端口被占或显存不够)。

---

## 1. 端口被占(最常见的"起不来")

占用端口：`8011`(引擎) `18091` `18092`(语音) `18999`(看门狗锁) `8765`(后端)。

```bat
netstat -ano | findstr ":8011 :8765 :18091 :18999"
```

```powershell
# 按 PID 查是谁
Get-Process -Id <PID> | Select-Object ProcessName, Path
```

处理：关掉占用者(旧的本程序实例、另一个 AI 工作台、浏览器调试端口等)，或
在**另一个目录**启动第二套会互相抢端口 —— 同一台机器只跑一套。

> 已知现场：开了另一个会绑 8011/18091 的本地 AI 服务(或上一次没退干净的实例)时，
> 新启动的服务会一直被看门狗"拉起→失败→再拉起"。先 `taskkill /PID <PID> /T /F`。

## 2. 显存不够 / 反复重启

```bat
nvidia-smi
```

- Mini 版**最低 12 GB 显存**: MuseTalk ≈ 9 GB + 单 TTS ≈ 2 GB。
- 启动前请关掉占用显存的东西：另一个生图/本地大模型工作台、游戏、浏览器硬件加速的重页面。
- 档位在 `engine/data/runtime_mode_active.json` 里能看到实测值；设置页可手选 `full` / `lite`。
- 12 GB 卡建议留在 `lite`(单 TTS worker)。`full` 需要 ≥16 GB。
- 若显示"`ultra` 也不够"：Mini 版没有卡通引擎可退，`ultra` 会自动按 `lite` 跑，
  所以低于 11 GB 显存时**没有可退档位** —— 只能减占显存的东西。

## 3. 语音服务被判"卡死"反复被杀(端口在，但没声音)

看门狗判 TTS 健康的方式是**真合成一句话**(只探端口会被"端口开着但模型没加载完"骗过)。
判据用的音色必须在 `engine/data/voice_registry.json` 里存在。

- Mini 版已把探活音色固定为 `arthur`(音色裁剪时必须同步改，否则死循环)。如果你自己裁过音色，
  记得同步 `engine/aiteacher_watchdog.py` 里 `deep_tts_ok()` 的 `'voice'` 字段。
- 手动验证语音真的能合成：

```bat
curl -X POST http://127.0.0.1:18091/v1/audio/speech ^
     -H "Content-Type: application/json" ^
     -d "{\"input\":\"Hello, this is a voice test.\",\"voice\":\"arthur\",\"response_format\":\"wav\"}" ^
     --output test.wav
```

能拿到 `test.wav`(>1 KB) = 语音服务真的活着。

## 4. 老师窗口里"人不对"

- 老师窗口走 `engine/web/desktop.html`，它建连时发的是**不带 avatar 参数**的请求，
  渲染的是引擎**启动主位**(Mini 版 = `coach_arthur`)。
- 想在窗口里换人：设置页切换老师(会把 persona 与音色一起换掉)；主位只在看门狗服务表里定义。
- 如果窗口一片绿/一片黑：那是**卡通系**渲染层的旧产物 → Mini 版没有卡通引擎，
  请确认你没有手工把 `--model viseme` 的服务加回来(它需要卡通素材，本档没带)。

## 5. 搬家后崩(换目录/换盘符/拷给别人)

```bat
SETUP.bat          :: = runtime\py312\python.exe tools\scripts\first_run_setup.py
```

它会修 `pyvenv.cfg` 的 `home`(指向包内 Python)并改写全树绝对路径。
**必须用包内的 Python 跑**：`runtime\py312\python.exe`，别用系统 Python。

手工痕迹：`engine/data/avatars/*/avator_info.json` 里会有全路径，自愈脚本会一起改。

## 6. venv 起不来 / `Fatal error in launcher` / 解释器指到开发机

这是"venv 搬家"经典坑：launcher 里写死了创建时的 Python 路径。自愈脚本会重写 `pyvenv.cfg`；
若仍失败(换机后 base 指向不存在的路径)，用包内 Python 重建：

```bat
runtime\py312\python.exe -m venv engine\venv
engine\venv\Scripts\pip.exe install -r engine\requirements.txt
runtime\py311\python.exe -m venv app\backend\.venv
app\backend\.venv\Scripts\pip.exe install -r app\backend\requirements-backend.txt
```

## 7. 对话没反应 / 一直"思考中"

- 云端 Key 没配或失效：托盘 → 设置 → 「云端 Token」→ 保存并测试。
- 界面能开但老师不说话：看 `app/backend/logs/` 与根目录 `日志/`。
- `401 Unauthorized` = Key 无效或该平台不认这个模型名。

## 8. 维护者工具(会打断服务，日常别用)

| 工具 | 干什么 | 注意 |
|---|---|---|
| `tools/verify_vram.py` | 停看门狗 → 逐个杀服务测显存 delta → 重启 | 会**杀掉**正在跑的服务 |
| `tools/verify_e2e_ui.py` | 端到端（含 CDP 读界面 DOM） | 同上，且会开 Electron |
| `tools/scripts/live_opener.py` | 等端口就绪后打开页面 | 只读等待 |

跑完记得让看门狗把服务拉回来(托盘"启动/修复"，或直接重启程序)。

## 9. 还是不行：按这个顺序收集信息

```bat
SETUP.bat                                   :: 关键文件 / 路径自愈自检
type engine\data\runtime_mode_active.json   :: 生效档位 + 实测显存/内存
netstat -ano | findstr ":8011 :8765"        :: 端口谁在听
nvidia-smi                                  :: 显存占用
```

再把 `app/backend/logs/` 里最新的日志一起发出来。