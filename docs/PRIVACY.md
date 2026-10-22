# Privacy Statement (Where Your Data Goes)

**English** · [中文说明 →](zh-CN/PRIVACY.md)

In a nutshell: **Language content goes to the cloud LLM, everything else stays on your local machine**; this program does not actively upload any files.

## Stays on Local Machine (Offline)

| Data | Location |
|---|---|
| Conversation History / Memory | `app/backend/config/history.json`, `sessions.json`, `app/backend/config/userdata/` |
| Coach Persona (soul) | `app/backend/config/persona.json` (generated from `avatar/persona/*.md`) |
| Your Settings | `app/backend/config/app.json` (window position in `app/config/electron.json`) |
| Cloud Keys | `app/backend/.env` |
| Cloned Voice Reference Audio | `audiocpp/webui/voice/*_ref.wav` |
| Logs | `app/backend/logs/`, root directory `日志/` |

These **will not** be uploaded by the program; they will also be stripped/cleared during packaging (see the sanitization items in `docs/PACKAGING.md`).

## What Gets Sent to the Cloud

| Content | Trigger Condition | Destination |
|---|---|---|
| Conversation Text (Current student utterance + coach reply) | Every conversation turn | The cloud LLM service you entered in the settings (OpenAI-compatible protocol, defaults to Alibaba Cloud Bailian `token-plan` endpoint, model `qwen3.8-flash`) |
| Voice Synthesis Text | Every time the coach speaks | **Local** CosyVoice2 on port `18091` (offline) |
| Camera Feed (Eye Attachment) | Only when you enable "Eye Attachment" (挂眼) | Cloud vision model (used for visual teaching) |

That is to say: **Only the "thinking content" and "looking at pictures" steps go online**. Speech recognition, voice synthesis, and lip-sync generation are all completed locally.

## How You Can Control It

- Don't want to use the cloud: Point `llm_base` to your own compatible endpoint (a locally deployed OpenAI-compatible service works too).
- Don't want the camera: Turn off "Camera/Eye Attachment" in the settings page (`eye_enabled` / `vision_enabled` in `app.json`).
- Want to completely wipe local records: Simply delete the conversation/memory files in the table above (empty files will be recreated on the next startup).

## Use by Children/Minors

The two coaches in this Mini edition both have **adult-oriented coaching** personas (geared towards adult learners and young professionals). If used by minors,
please evaluate the compliance of cloud content and camera image uploads yourself (we recommend turning off the eye attachment).

## Telemetry

**None**. The program contains no statistical reporting, crash reporting, or usage analytics. The only network requests are to the cloud model interfaces you configure.
