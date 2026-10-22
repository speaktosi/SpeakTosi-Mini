# Coach Profiles (Coach Arthur / Coach Mia)

**English** · [中文说明 →](zh-CN/TEACHERS.md)

The Mini edition only includes these two. Both are **live-action** styles: MuseTalk real-time lip-sync + CosyVoice2 voice cloning + adult-oriented coaching personas.

---

## Coach Arthur 📚

| Item | Value |
|---|---|
| Display Name | **Arthur** |
| Title | Wise and Erudite Mentor · *The Wise Scholar* |
| Positioning | Digging into long sentences, critical thinking, turning "can say" into "say accurately" |
| Greeting | *"Hello, I'm Arthur. Bring me a big question, and let's explore it together."* |
| Avatar ID | `coach_arthur` |
| Voice Preset | `arthur` (Reference audio `audiocpp/webui/voice/arthur_ref.wav`) |
| Synth Params | `seed=2, temperature=0.5, f0=132.8Hz, tol=0.18` |
| Persona File | `avatar/persona/CoachArthur.md` |
| Avatar Pack / Idle Sequence | `engine/data/avatars/coach_arthur/`, `engine/data/customvideo/coach_arthur_idle/` |
| Engine Primary | ✅ Yes (8011 startup primary = Arthur, so he is rendered in the "coach appears on desktop" window) |

## Coach Mia 🎨

| Item | Value |
|---|---|
| Display Name | **Mia** |
| Title | Vibrant Art Vlogger · *The Vibrant Vlogger* |
| Positioning | Daily expression, emotion and detail description, turning "nothing to say" into "having things to chat about" |
| Greeting | *"Hey, I'm Mia! Tell me something about your day—let's make it sparkle."* |
| Avatar ID | `coach_mia` |
| Voice Preset | `mia` (Reference audio `audiocpp/webui/voice/mia_ref.wav`) |
| Synth Params | `seed=9, f0=200.0Hz, tol=0.18` |
| Persona File | `avatar/persona/CoachMia.md` |
| Avatar Pack | `engine/data/avatars/coach_mia/` |
| Idle Sequence | No independent sequence → Uses frames included in the avatar pack (Engine falls back when `custom_config` is missing) |

> Speaking rate: Both use the `_default` target speaking rate from `engine/data/tts_calibration.json` (138 wpm, dynamically normalized based on measured duration).

---

## Persona File Format (`avatar/persona/*.md`)

```markdown
[name] Arthur
[title] 睿智博学导师 · The Wise Scholar
[emoji] 📚
[voice_preset] arthur
[greeting] Hello, I'm Arthur. Bring me a big question, and let's explore it together.

(以下是正文 —— 会原样进入 System prompt，Markdown 语法保留)
YOU ARE ARTHUR …
```

- `[Fields]` headers: the system parses at most these keys: `name / title / emoji / voice_preset / greeting`.
- Body = the entire original text after removing the header, concatenated with `Shared-core-adult.md` (general specifications for adult coaches):

```text
system = Shared-core-adult.md 正文 + "\n\n" + 本老师正文 + (若有 greeting 则追加"固定开场白"锚)
```

- The `system` content is identical to `system_master` (the latter is a copy reserved for "master control" purposes).
- The generated result is saved to `app/backend/config/persona.json`, rewritten when switching coaches (switching coach = soul swapping).

---

## Adding a Third Coach (Complete Checklist)

Taking adding `coach_xxx` as an example, all six parts must be complete. Missing one will result in "appearing in the interface but broken when clicked":

1. **Avatar Pack**: `engine/data/avatars/coach_xxx/` (latents/coords/mask/full_imgs/avator_info.json, etc., output from MuseTalk training).
2. **Idle Sequence** (Optional): `engine/data/customvideo/coach_xxx_idle/*.png`. If provided, also write
   `engine/data/custom_config_coach_xxx.json`: `[{"audiotype":1,"imgpath":"data/customvideo/coach_xxx_idle"}]`.
3. **Registry**: Add an entry to `AVATARS` in `app/backend/server.py` (`avatar_id / name / voice_preset / persona_md / emoji / greeting / engine`).
   Simultaneously add an item to `_TEACHER_MD` (`"coach_xxx": "CoachXxx.md"`).
4. **Persona**: `avatar/persona/CoachXxx.md` (format as above).
5. **Voice**: Place the reference audio at `audiocpp/webui/voice/xxx_ref.wav` → Add the `xxx` entry in `engine/data/voice_registry.json` (reference audio path + reference text)
   → Add `xxx` to `voice_presets` in `tools/scripts/tts_demo_voice.json` (determines the interface voice dropdown)
   → Add `xxx` to `voice_params` in `engine/data/tts_calibration.json` (seed/temperature/f0/tol);
   After the first synthesis, a voice anchor will be generated at `engine/data/voice_anchor_*`.
6. **Avatar Assets**: `avatar/Coach-Xxx.png` (Portrait, used for the settings page cover), `avatar/Coach-Xxx.wav` (Greeting reference audio, optional).

After making changes, restart the watchdog (or click "Start/Repair" in the system tray). If you want to include the new coach in the distribution package,
add it to `MINI_TEACHERS` in `tools/mini_edition.py` (or just use `--lowmem` to package the original lineup).
