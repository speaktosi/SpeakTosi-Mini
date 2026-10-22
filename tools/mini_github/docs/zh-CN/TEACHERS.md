# 老师规格(Coach Arthur / Coach Mia)

Mini 版只装这两位。两人都属 **真人系**：MuseTalk 实时口型 + CosyVoice2 克隆音色 + 成人向教练人格。

---

## Coach Arthur 📚

| 项目 | 值 |
|---|---|
| 显示名 | **Arthur** |
| 头衔 | 睿智博学导师 · *The Wise Scholar* |
| 定位 | 深挖长句、思辨表达、把"说得出"练成"说得准" |
| 见面语 | *"Hello, I'm Arthur. Bring me a big question, and let's explore it together."* |
| 形象 id | `coach_arthur` |
| 音色 preset | `arthur`(参考音 `audiocpp/webui/voice/arthur_ref.wav`) |
| 合成参数 | `seed=2, temperature=0.5, f0=132.8Hz, tol=0.18` |
| 人设文件 | `avatar/persona/CoachArthur.md` |
| 形象包 / 待机序列 | `engine/data/avatars/coach_arthur/`、`engine/data/customvideo/coach_arthur_idle/` |
| 引擎主位 | ✅ 是(8011 启动主位 = Arthur，因此"老师出现在桌面"窗口渲染的是他) |

## Coach Mia 🎨

| 项目 | 值 |
|---|---|
| 显示名 | **Mia** |
| 头衔 | 灵动艺术博主 · *The Vibrant Vlogger* |
| 定位 | 日常表达、情绪与细节描述、把"没话说"变成"有话聊" |
| 见面语 | *"Hey, I'm Mia! Tell me something about your day—let's make it sparkle."* |
| 形象 id | `coach_mia` |
| 音色 preset | `mia`(参考音 `audiocpp/webui/voice/mia_ref.wav`) |
| 合成参数 | `seed=9, f0=200.0Hz, tol=0.18` |
| 人设文件 | `avatar/persona/CoachMia.md` |
| 形象包 | `engine/data/avatars/coach_mia/` |
| 待机序列 | 无独立序列 → 用形象包自带帧(引擎按 `custom_config` 缺失回退) |

> 语速：两位都走 `engine/data/tts_calibration.json` 的 `_default` 目标语速(138 wpm，会按实测时长动态归一化)。

---

## 人设文件格式(`avatar/persona/*.md`)

```markdown
[name] Arthur
[title] 睿智博学导师 · The Wise Scholar
[emoji] 📚
[voice_preset] arthur
[greeting] Hello, I'm Arthur. Bring me a big question, and let's explore it together.

(以下是正文 —— 会原样进入 System prompt，Markdown 语法保留)
YOU ARE ARTHUR …
```

- `[字段]` 头最多读这几个键：`name / title / emoji / voice_preset / greeting`。
- 正文 = 去掉头部之后的全部原文，与 `Shared-core-adult.md`(成人教练通用规范)拼接：

```text
system = Shared-core-adult.md 正文 + "\n\n" + 本老师正文 + (若有 greeting 则追加"固定开场白"锚)
```

- `system` 与 `system_master` 内容相同(后者给"主控"用途留的副本)。
- 生成结果落在 `app/backend/config/persona.json`，切换老师时重写(切老师 = 换魂)。

---

## 加第三位老师(完整清单)

以新增 `coach_xxx` 为例，六处都要齐，少一处就"界面里有、点开是坏的"：

1. **形象包**：`engine/data/avatars/coach_xxx/`(latents/coords/mask/full_imgs/avator_info.json 等，MuseTalk 训练产物)。
2. **待机序列**(可选)：`engine/data/customvideo/coach_xxx_idle/*.png`。若提供，再写
   `engine/data/custom_config_coach_xxx.json`：`[{"audiotype":1,"imgpath":"data/customvideo/coach_xxx_idle"}]`。
3. **注册表**：`app/backend/server.py` 的 `AVATARS` 加一条(`avatar_id / name / voice_preset / persona_md / emoji / greeting / engine`)。
   同时把 `_TEACHER_MD` 加一项(`"coach_xxx": "CoachXxx.md"`)。
4. **人设**：`avatar/persona/CoachXxx.md`(格式见上)。
5. **音色**：`audiocpp/webui/voice/xxx_ref.wav` 放参考音 → `engine/data/voice_registry.json` 加 `xxx` 条目(参考音路径 + 参考文本)
   → `tools/scripts/tts_demo_voice.json` 的 `voice_presets` 加 `xxx`(界面音色下拉由它决定)
   → `engine/data/tts_calibration.json` 的 `voice_params` 加 `xxx`(seed/temperature/f0/tol)；
   首次合成后会在 `engine/data/voice_anchor_*` 生成音色锚点。
6. **形象素材**：`avatar/Coach-Xxx.png`(立绘，设置页封面用它)、`avatar/Coach-Xxx.wav`(见面语参考音，可选)。

改完重启看门狗(或托盘"启动/修复")即可。若要把新老师也带进打包件，
在 `tools/mini_edition.py` 的 `MINI_TEACHERS` 里加上它(或直接用 `--lowmem` 打包原阵容)。