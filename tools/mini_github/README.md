# SpeakTosi Mini — Free Open-Source AI English Tutor with a Real-Time Talking-Head Teacher

> **SpeakTosi Mini is a free, open-source AI English tutor for Windows** — a real-time talking-head
> **AI English coach** that talks *with* you instead of quizzing you. Two teachers are included:
> **Coach Arthur** (the wise scholar 📚) and **Coach Mia** (the vibrant vlogger 🎨).
> The teacher's lips are driven by **MuseTalk** from the real speech audio, and the voices are
> cloned locally with **CosyVoice2** — so you practise spoken English with a face and a voice,
> one-on-one, at your own desk.

<p>
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg">
  <img alt="Platform: Windows 10/11 x64" src="https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-0078d4.svg">
  <img alt="GPU: NVIDIA 12GB+ VRAM" src="https://img.shields.io/badge/GPU-NVIDIA%20%E2%89%A512GB%20VRAM-76b900.svg">
  <img alt="Edition: Mini (2 teachers)" src="https://img.shields.io/badge/edition-Mini%20%C2%B7%202%20teachers-orange.svg">
  <img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-brightgreen.svg">
</p>

<p align="center">🌐 Official site: <b><a href="https://www.speaktosi.com">www.speaktosi.com</a></b> — custom AI-teacher avatars, cloned voices and tailored courses</p>

**English** · [中文说明 →](README.zh-CN.md)

---

## What is SpeakTosi? (in one paragraph)

Most "AI English tutor" tools give you a text box or a cartoon head. SpeakTosi gives you a
**photoreal digital teacher with real-time lip-sync** who holds a spoken conversation with you and
then reviews it. Mini is the open-source edition: **two AI English coaches, MuseTalk talking-head
rendering, cloned voices, speech recognition, session review and long-term memory** — all
orchestrated locally on your PC. Only one step ("thinking up the answer") leaves your machine, and
you can point that at a local model if you want.

**Typical searches this answers:** AI English tutor · **AI-powered English tutor** · free AI English
tutor · AI English coach · **real-time MuseTalk English teacher** · talking-head English teacher ·
MuseTalk talking head · spoken English practice · English conversation practice with a digital
human · ESL speaking partner · self-hosted language-learning teacher.

## A real-time MuseTalk English teacher that runs on your own PC

If you searched for a **real-time MuseTalk English teacher**, for an **AI-powered English tutor**
that actually *speaks*, or for a **free AI English coach** you can self-host, this is that project.
SpeakTosi Mini renders a photoreal teacher whose mouth is driven by **MuseTalk from the real speech
audio**, gives it a **locally cloned voice**, and lets you practise **spoken English** one-on-one —
then hands you a review of the session. No subscription, no account, no per-minute fee: MIT-licensed
code, your own LLM key, everything else on your machine.

## Why SpeakTosi Mini

| | |
|---|---|
| 🎓 **A teacher, not a quiz** | It asks, listens, replies and follows up — then hands you a SESSION REVIEW: what went well, one sentence phrased better, what to practise next. |
| 🗣 **Real-time talking head** | MuseTalk drives the mouth from the actual audio signal (not a looping wave animation), so the face matches the words while it speaks English. |
| 🔊 **Cloned, consistent voices** | CosyVoice2 runs locally; each coach keeps one fixed voice across the whole conversation — no mid-sentence voice drift. |
| 💬 **Live spoken loop** | Wake word, then speech recognition, then the LLM reply, then speech — orchestrated locally on your machine. |
| 🧠 **Long-term memory** | Remembers your level, interests and last topic (stored locally, not on a server). |
| 🔓 **Free and open source (MIT)** | Self-host it, read it, change it. No subscription, no account, no per-minute fee. |
| 🖥 **Desktop companion** | Electron window and tray icon that can float on your desktop while you chat. |
| 🎚 **Adjustable reply length** | Five presets in Settings (unlimited / long 200w / standard 120w default / short 80w / minimal 50w) — hot-reloads within 30 s. |

> ⚠️ This edition ships **only these two coaches**. The original development build had 13
> (including a cartoon line-up); everything else was pruned so the package stays small and cold
> starts stay fast. Need the full line-up? Repackage with `tools/package_share.py --lowmem`
> (see [docs/PACKAGING.md](docs/PACKAGING.md)).

## Quick start

**Option A — turnkey (recommended).** The self-contained `SpeakTosi_Mini_*.zip` (~25 GB, all models
and Python environments included) **exceeds GitHub's 2 GB per-file limit, so it is not hosted in
Releases** — to get the turnkey build, contact us via <https://www.speaktosi.com>. Once you have it:

```text
1) Unzip into an English path without spaces, e.g. D:\SpeakTosi
2) Double-click start_speaktosi.vbs      (first run self-heals absolute paths)
3) The first-run wizard asks for your cloud API key, then "Save and test"
4) Wait for the tray icon to turn green (cold start 1-3 min), then start talking
```

**Option B — from source.**

```bash
git clone https://github.com/speaktosi/SpeakTosi-Mini.git && cd SpeakTosi-Mini
cd app && npm install && cd ..
python -m venv engine/venv && engine/venv/Scripts/pip install -r engine/requirements.txt
python -m venv app/backend/.venv && app/backend/.venv/Scripts/pip install -r app/backend/requirements-backend.txt
# models + avatar assets + CosyVoice runtime: see docs/ASSETS.md
```

## Requirements

| | |
|---|---|
| OS | Windows 10/11 x64 |
| GPU | NVIDIA, **at least 12 GB VRAM** (16 GB recommended). MuseTalk engine about 9 GB, cloned voice about 2 GB per worker |
| RAM | **at least 16 GB** (32 GB recommended) |
| Disk | about 25 GB unpacked, plus about 20 GB for sessions/logs |
| Cloud | any OpenAI-compatible LLM endpoint with your own key (a cheap tier is fine) |

## Ports

| Port | Service |
|---|---|
| **8011** | Talking-head engine (MuseTalk) |
| 18091 / 18092 | Cloned voice (CosyVoice2 workers) |
| 18999 | Watchdog single-instance lock |
| 8765 | Backend API (the only endpoint the UI talks to) |

There is **no 8010** in this edition: the cartoon viseme engine was removed and the MuseTalk engine
also hosts the desktop-page render.

## FAQ

**Is SpeakTosi Mini really free?**
The code is MIT-licensed and free to self-host forever. There is no subscription and no account.
You supply your own LLM API key (a cheap model is enough), which is the only running cost.

**Does it work offline?**
Speech recognition, the talking-head renderer and the cloned voice all run locally. Only the "write
the reply" step calls an LLM — point it at a local OpenAI-compatible server and it runs fully offline.

**Which LLMs does it support?**
Any endpoint that speaks the OpenAI chat-completions protocol (DeepSeek, Qwen/DashScope, OpenAI,
Moonshot, a local llama.cpp or vLLM server ...). You pick the platform in the first-run wizard.

**How is this different from a talking-head demo?**
A demo makes a face move. SpeakTosi closes the loop: wake word, speech recognition, tutor persona
with a level and memory, live spoken reply, then an end-of-session review of your English.

**Can I use my own avatar, my own voice, or my own curriculum?**
In the code, yes — see [docs/TEACHERS.md](docs/TEACHERS.md) and [docs/ASSETS.md](docs/ASSETS.md).
Custom avatar and voice production plus industry-specific course scripts are offered through
🌐 <https://www.speaktosi.com>.

**Will it run on my laptop?**
It needs an NVIDIA GPU with **at least 12 GB VRAM** and 16 GB RAM. Weaker machines can still read the
code and run the backend, but the real-time talking head will not keep up.

**Is this an AI-powered English tutor or just a chatbot?**
A tutor. It has a persona and a level, it remembers earlier sessions, it asks and follows up, and it
ends with a review of your speaking — the talking head and the cloned voice are the delivery, not
the product.

**Is it good as a free AI English tutor for daily practice?**
Yes — MIT-licensed, unlimited sessions, no account and no per-minute fee. The only running cost is
your own LLM API key, and a cheap model is enough for conversation practice.

## Docs

[INSTALL](docs/INSTALL.md) · [ARCHITECTURE](docs/ARCHITECTURE.md) · [TEACHERS](docs/TEACHERS.md) ·
[ASSETS](docs/ASSETS.md) · [TROUBLESHOOTING](docs/TROUBLESHOOTING.md) ·
[PACKAGING](docs/PACKAGING.md) · [PRIVACY](docs/PRIVACY.md) · [CHANGELOG](CHANGELOG.md) ·
[CONTRIBUTING](CONTRIBUTING.md)

All documentation is written in **English** by default. Chinese versions live in
[`docs/zh-CN/`](docs/zh-CN/) (文档中文版).

## About SpeakTosi and custom work

SpeakTosi is a **customisable AI English tutor**: a photoreal talking-head avatar, live one-on-one
conversation and an end-of-session review.

- This repo is the **open-source Mini edition** (Arthur / Mia only) — free to self-host.
- Want **your own avatar as the teacher**, a **cloned voice**, **industry-specific course scripts**,
  or full deployment and integration support? See 🌐 **<https://www.speaktosi.com>**
  (you can also open an issue here).
- Custom avatars and voice assets are not part of the Mini edition.

## License

MIT — see [LICENSE](LICENSE). Third-party model weights and assets keep their own upstream
licenses — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
Thanks to the MuseTalk, CosyVoice2, LiveTalking, whisper, Electron and FastAPI projects.

<!-- Topics: ai english tutor · ai-powered english tutor · free ai english tutor · ai english coach · real-time musetalk english teacher · talking-head english teacher · musetalk talking head · english conversation practice · spoken english · esl · language learning · self-hosted · voice cloning · cosyvoice2 · electron · fastapi · windows -->
