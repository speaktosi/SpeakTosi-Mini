# SpeakTosi Mini — Two AI English Teachers

> A desktop **AI English tutor** with a real-time talking-head avatar, one-on-one spoken practice,
> and an end-of-session review. This is the **Mini edition**: only two teachers —
> **Coach Arthur** (the wise scholar 📚) and **Coach Mia** (the vibrant vlogger 🎨).

🌐 Official site: **<https://www.speaktosi.com>** — custom AI-teacher avatars, cloned voices and tailored courses.

[中文说明 →](README.md)

---

## What you get

- 🎓 **Two coaches** — Arthur (literature-minded, pushes long-form thinking) and Mia (daily-life fluency, emotions and detail).
- 🗣 **Real lip-sync** — MuseTalk drives the mouth from the actual audio (not a wave animation).
- 🔊 **Cloned voices** — CosyVoice2 runs locally; each coach has a fixed, consistent voice.
- 💬 **Live conversation** — wake word → speech recognition → LLM reply → speech, orchestrated locally. Only "thinking up the answer" leaves your machine.
- 📝 **Session review** — after a conversation: what went well, one sentence said better, what to practice next.
- 🧠 **Long-term memory** — remembers level, interests and last topic. Stored locally.
- 🖥 **Desktop companion** — Electron window + tray; can sit on your desktop while you chat.
- 🎚 **Adjustable reply length** — five presets in Settings (unlimited / long 200w / standard 120w default / short 80w / minimal 50w); hot-reloads within 30 s, no restart.

> ⚠️ This edition ships **only these two coaches**. The original development build had 13
> (including a cartoon line-up); everything else was pruned. Need the full line-up?
> Repackage with `tools/package_share.py --lowmem` (see [docs/PACKAGING.md](docs/PACKAGING.md)).

## Quick start

**Option A — turnkey (recommended).** The self-contained `SpeakTosi_Mini_*.zip` (~25 GB, all models and
Python environments included) **exceeds GitHub's 2 GB per-file limit, so it is not hosted in Releases** —
to get the turnkey build, contact us via <https://www.speaktosi.com> (domain / ICP filing in progress).
Once you have the zip:

```text
1) Unzip into an English path without spaces, e.g. D:\SpeakTosi
2) Double-click start_speaktosi.vbs      (first run self-heals absolute paths)
3) The first-run wizard asks for your cloud API key → "Save & test"
4) Wait for the tray icon to turn green (cold start 1–3 min), then start talking
```

**Option B — from source.**

```bash
cd app && npm install && cd ..
python -m venv engine/venv && engine/venv/Scripts/pip install -r engine/requirements.txt
python -m venv app/backend/.venv && app/backend/.venv/Scripts/pip install -r app/backend/requirements-backend.txt
# models + avatar assets + CosyVoice runtime: see docs/ASSETS.md
```

## Requirements

| | |
|---|---|
| OS | Windows 10/11 x64 |
| GPU | NVIDIA, **≥12 GB VRAM** (16 GB recommended) |
| RAM | **≥16 GB** (32 GB recommended) |
| Disk | ~25 GB unpacked + ~20 GB for sessions/logs |

## Ports

| Port | Service |
|---|---|
| **8011** | Talking-head engine (MuseTalk) |
| 18091 / 18092 | Cloned voice (CosyVoice2 workers) |
| 18999 | Watchdog single-instance lock |
| 8765 | Backend API (the only endpoint the UI talks to) |

There is **no 8010** in this edition: the cartoon viseme engine was removed and the MuseTalk engine
also hosts the desktop-page render.

## Docs

[INSTALL](docs/INSTALL.md) · [ARCHITECTURE](docs/ARCHITECTURE.md) · [TEACHERS](docs/TEACHERS.md) ·
[ASSETS](docs/ASSETS.md) · [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) ·
[PACKAGING](docs/PACKAGING.md) · [PRIVACY](docs/PRIVACY.md) · [CHANGELOG](CHANGELOG.md) ·
[CONTRIBUTING](CONTRIBUTING.md)

## About SpeakTosi & custom work

SpeakTosi is a **customisable AI English tutor**: a photoreal talking-head avatar, live one-on-one
conversation and an end-of-session review.

- This repo is the **open-source Mini edition** (Arthur / Mia only) — free to self-host.
- Want **your own avatar as the teacher**, a **cloned voice**, **industry-specific course scripts**,
  or full deployment / integration support? See 🌐 **<https://www.speaktosi.com>**
  (domain registration in progress — coming soon; you can also open an issue here).
- Custom avatars and voice assets are not part of the Mini edition.

## License

MIT — see [LICENSE](LICENSE).
Thanks to MuseTalk, CosyVoice2, LiveTalking, whisper, Electron and FastAPI.