# 打包指南(怎么重新出一份)

打包器：`tools/package_share.py`，裁剪规则：`tools/mini_edition.py`。

```powershell
python tools\package_share.py --mini            # 自包含 Mini 版(两位老师, ~25 GB, 解压即用)
python tools\package_share.py --mini --dry      # 只统计体积, 不写 zip(改规则后先跑这个)
python tools\package_share.py --mini-code --tree dist\SpeakTosi-Mini-2Teachers   # GitHub 仓库档(只代码+文档)
python tools\package_share.py --lowmem          # 13 师完整版(原始阵容, 含卡通系)
python tools\package_share.py --full            # 13 师 + 克隆语音运行环境
python tools\package_share.py --share           # 13 师源码分享包
```

双击版(给人用的)：`tools\package_mini.bat`(Mini 自包含) / `tools\package_full.bat` / `tools\package_share.bat`。

## 档位对照

| 档位 | 老师 | 模型/环境 | 形象素材 | 卡通系引擎 | 体量 |
|---|---|---|---|---|---|
| `mini` | Arthur + Mia | ✅ 全带 | ✅ 两位 | ❌ 裁掉 | ≈ 25 GB(实际文件数与体积见产物内 `BUILD-INFO.txt`) |
| `mini-code` | Arthur + Mia | ❌ 不带 | ❌ 不带 | ❌ 裁掉 | ≈ 200 MB |
| `lowmem` | 13 位 | ✅ 全带 | ✅ 13 位 | ✅ 保留 | ≈ 28 GB |
| `full` | 13 位 | ✅ 全带 | ✅ 13 位 | ✅ 保留 | ≈ 35 GB |

## Mini 档裁了什么(规则表)

规则集中在 `tools/mini_edition.py`，每条都带依据注释。落到实现上是四类动作：

1. **文件级保留/剔除** —— `drop_reason(rel)`：
   `engine/data/avatars/<id>`、`engine/data/customvideo/<id>_idle`、`engine/data/custom_config_<id>.json`、
   `audiocpp/webui/voice/<voice>_ref.wav`、`engine/data/voice_anchor_ref_<voice>_ref.wav_*.npz`、
   `avatar/persona/<老师>.md`、`avatar/Coach-<老师>.*`、`app/backend/config/personas/*`(默认老师灵魂备份)。
2. **配置重算** —— `build_overrides()`：
   `app/backend/config/app.json`(默认老师/音色/唤醒词)、`persona.json`(出厂 soul，规则与
   `app/backend/server.py` 逐字一致)、`engine/data/voice_registry.json`、`engine/data/tts_calibration.json`、
   `tools/scripts/tts_demo_voice.json`(界面音色下拉的唯一来源)、`engine/data/custom_config.json`。
3. **源码文本补丁** —— `RAW_PATCHERS`：
   `app/backend/server.py`(AVATARS 用 ast 摘两位老师的原生条目 + `_TEACHER_MD` + 出厂默认值)、
   `engine/aiteacher_watchdog.py`(8011 主位 → Arthur、**深探音色 → arthur**、8010 卡通引擎块整块移除、
   `ultra` 钳到 `lite`、**副 TTS worker `lx_tts2` 放 boot 组2**(1022load 冷启错峰: 首句只需 `lx_tts`+musetalk;
   副等组1健康后再起 → 读的是已在页缓存的同一份权重, 自身更快且不抢首句的磁盘/CPU。回滚=`boot_group: 1`))、
   `app/main.js`(桌面窗口/状态灯 8010 → 8011)、`app/renderer/overlay.js`、
   `engine/web/desktop.html`、运维脚本。
4. **文档层覆盖** —— `tools/mini_github/**`：README、docs/、`.github/`(issue/PR 模板 + CI)、
   `.gitignore`、`.gitattributes`。zip 与目录树共用这一层，所以两个发布件的文档是同一套。

> `--mini` 与 `--mini-code` **都不修改开发机源码**：所有裁剪只发生在打包内存里。
> 要回到原阵容，`--lowmem` 打一份即可，零回滚成本。

## 打包器自检(每次自动跑)

1. 密钥嗅探：全树扫 `sk-…` / `API_KEY=…` 形态，命中即**拒绝入包**；`*.env` 一律排除。
2. 脱敏剔除：个人五官标定、窗口位置、会话/记忆、运行时状态文件。
3. 关键文件齐全：`MUST_PATHS`(两位老师的形象包、待机序列、音色、人设、配置…)。
4. **Mini 专项**：
   - `check_names()` —— 产物里还有没有别家老师的资产(残留即报)；
   - `audit_teacher_refs()` —— `AVATARS` 是否只剩两位、看门狗还有没有旧主位/旧探针引用、音色 preset 是否恰好两位；
   - `verify_persona_generator()` —— 用本仓库规则重算 Mia 的 soul，与仓库里**真代码生成**的 `persona.json` 逐字段比对，
     不一致直接报错(防止把"我以为是"的 soul 打进包)。

自检不通过就别发出去。

## 发完之后

- 自包含包：网盘/局域分发，写清"路径英文无空格 + 显存 ≥12 GB"。
- 仓库档：`git init` → 推到私有/公开仓库；`avatar/*.png` 会作为 README 配图。
- 体积大件的替代方案：把 `engine/models/**`、`engine/data/avatars/**`、`engine/data/customvideo/**`
  作为 Release 资产(或私有对象存储)，在 Release 说明里指个链接即可 —— 见 docs/ASSETS.md。