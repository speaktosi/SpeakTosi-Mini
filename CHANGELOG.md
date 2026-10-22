# Changelog

This file documents the release history of **SpeakTosi Mini (2-Teacher Edition)**. For the complete history of the 13-teacher version, refer to the development repository.

## Documentation/SEO — 2026-10-22 (Repo copy only, no functional changes)

### Documentation
- **Default README language changed to English** (for international users): `README.md` is now in English, and `README.zh-CN.md` is in Chinese;
  Removed duplicate content `README.en.md` (duplicate content harms search rankings).
- English README rewritten as a **Search-Friendly Version**: The title now includes *Free Open-Source AI English Tutor with a Real-Time Talking-Head Teacher*, and the text naturally covers search terms like AI English tutor / AI English coach / MuseTalk talking head / spoken English practice / self-hosted, etc., and adds an FAQ section (Free? Offline? Which LLMs are supported? What GPU is required?).
- Synchronized renaming in the release documentation layer `tools/mini_github/`, and the mini-code whitelist in `tools/package_share.py` was changed from `README.en.md` to `README.zh-CN.md` (otherwise the next packaging would overwrite the repo's README with the old Chinese version).
- Reset GitHub repo About description and topics with English keywords (English description + 20 topics).

## 1.0.3 — 2026-10-22

### Fixes
- **Root cause fix for "sudden disconnection" mid-conversation (watchdog misjudging "busy" as "dead")**:
  On Windows, when the listening queue is full, new connections return `WSAECONNREFUSED / WinError 10061 "Target machine actively refused"`, which is the **exact same error code** as "the service is actually dead" (Linux silently drops the packet, so this only manifests on Windows).
  The old health probe used this to declare death, `kill`ing and restarting the actively working engine/TTS — empirical testing showed it killed 4 times within 13 minutes (19:10~19:19), while before being killed, the engine logs still showed normal rendering `actual avg final fps:25.x` and the TTS was still synthesizing normally (counter-evidence).
  Now changed to "**Busy does not mean dead**": When the probe is refused/returns 503, it first arbitrates port ownership (`netstat` confirms the port is still `LISTENING` by the local stack process → judged as "busy", no counting, no interruption; only if this persists for 8 consecutive rounds will it fallback to recovering a true zombie).
  Simultaneously increased the TTS service listening queue from 5 → **128**, and engine aiohttp `backlog` → **512**, to make the "busy" event itself occur less frequently.
- **Late callback crash on reconnecting**: The `connectionstatechange` listener in `app/renderer/overlay.js` reads the mutable `pc` variable. After `teardown()` sets `pc` to `null`, late events from the old connection would throw `Cannot read properties of null (reading 'connectionState')` (empirical testing showed this error spamming the screen alongside every mistaken kill round).
  Now, a **snapshot of the current connection instance** is taken first inside the listener, and late events for deprecated connections are ignored (this also eliminates "old connection falsely reporting disconnection → triggering redundant reconnections").

### Optimizations
- **Cold start staggering**: The secondary TTS worker (18092) was changed from "parallel cold-reading weights with the first sentence path" to **boot group 2** (waiting until `lx_tts` is healthy before starting).
  - Empirical basis: Dual TTS cold starting simultaneously and cold-reading weights takes about **76s** each, whereas hot starting (weights already in page cache) only takes about **20s**.
  - The first sentence only needs `lx_tts` (+ engine); the secondary worker queued after group 2 reads the **same weights already in the page cache**, making it faster itself, and it no longer competes with the first sentence path for Disk/CPU.
  - Safety: If the engine endpoint table already lists 18092 but the secondary worker is not yet ready, `_tts_pick` will fall back to the backup endpoint and automatically remove the failed endpoint for 60s, preventing silence.
  - Rollback: Change `boot_group` of `lx_tts2` back to `1` in `aiteacher_watchdog.py`.

## 1.0.1 — 2026-10-20

### Added
- **Response length adjustable in 5 tiers** (Settings page → "Response Length"): Unlimited / Long (200 words) / Standard (120 words · Factory default) / Short (80 words) / Minimal (50 words, ensures fast initial speaking speed).
  Both the text chain (`app/backend/server.py`) and voice chain (`engine/llm.py`) share the same `_len_rule()`, ensuring exact consistency byte-by-byte (the packaging self-check will compare them to prevent drift between the two chains).
  - Fixed old specification flaw: Previously, "≤80 words" was hard-tied to "splitting into 1-2 short turn sentences and stopping after speaking", which empirically only produced about 25 words, overriding the 4-8 sentence persona contract in `avatar/persona/CoachArthur.md` (students felt the "response was too short").
  - Now graded by tiers: ≤60 words = Minimal 1-2 sentences / 61-100 words = Short 2-4 sentences / >100 words = Standard 3-6 sentences. Tested with the same question and model: 50→19 words, 80→33 words, 120→53 words, 200→79 words, Unlimited→72 words (the old 80 tier only gave 25 words before the change).
- Official website entry: **www.speaktosi.com** provided at the bottom of the Settings page and README; `app/main.js` allows external links (delegated to the system browser to open, no longer popping up blank windows).
- Customization service instructions (exclusive teacher avatars / cloned voices / course scripts) added to the README (CN/EN) and in-package installation instructions.

### Fixes
- **Engine config path self-healing in package**: 4 hardcoded paths like `C:\Speaktosi\...\config\app.json` in `engine/llm.py` changed to `_cfg_json_path()` —— in-package relative path priority → fallback to old machine absolute path → can be overridden with the `SPEAKTOSI_APP_JSON` environment variable.
  After unpacking on a new machine/drive, the engine will definitively read this package's config (previously relied only on rewriting source code on first boot as a fallback).
- **Voice registry portability across machines**: When `ref_wav` in `voice_registry.json` is missing on other machines, the reading end falls back to the reference voice of the same name within the package; the writing end normalizes slashes uniformly to `C:/...` (previously it hard-checked for existence, which would cause the TTS to throw errors directly on a new machine).

## 1.0.0 — 2026-10-20

Initial release of the Mini Edition: Only includes two real-person teachers, **Coach Arthur** and **Coach Mia**.

### Added
- `tools/package_share.py --mini`: Self-contained Mini package (≈25 GB), ready to use after extraction; the output includes `BUILD-INFO.txt` (actual file count/size/rule fingerprints).
- `tools/package_share.py --mini-code --tree`: GitHub repository package (code + documentation only).
- `tools/mini_edition.py`: Mini package trimming rules (each with forensic evidence), zero modifications to the development machine source code, can revert to the original lineup at any time via `--lowmem`.
- Complete documentation set: README (CN/EN), INSTALL, ARCHITECTURE, TEACHERS, ASSETS, TROUBLESHOOTING, PACKAGING, PRIVACY, and `.github/` (issue / PR templates + CI).
- Packager Mini specific self-check: Residual assets of other teachers, `AVATARS` narrowing, voice counts, field-by-field comparison of the persona generator and `server.py`.

### Changed (relative to 13-teacher dev version)
- **Engine topology**: Trimmed the cartoon-style viseme engine (8010). The Mini version only has one engine, MuseTalk (8011), which also serves as the host for the `engine/web/desktop.html` ("teacher appears on desktop") window; port references for the desktop window/status light/maintenance scripts synchronized to 8011.
  - Reason: viseme's mouth shapes are shared cartoon layers (`avatar/viseme/*.png`) pasted onto the idle frames; pasting cartoon mouths on a real-person base image results in misaligned facial features; and previously testing musetalk assets fed into the viseme engine caused it to freeze.
- **Engine startup primary**: `aiteacher_demo` (demo teacher) → `coach_arthur`, voice `default` → `arthur`.
  This ensures that connecting (desktop window) "without the avatar parameter" renders the teacher for this package, not the trimmed demo teacher.
- **TTS deep probe voice**: `lingxue` → `arthur`. The watchdog uses "actually synthesizing a sentence" to determine if TTS is alive; probing a trimmed voice using the old voice would cause 18091 to forever be judged "stuck" → repeatedly killed (infinite loop).
- **Operation gear**: `ultra` (defined as "only run cartoon engine") is meaningless in this package → fallback to `lite` when `ultra` is requested.
- **Factory defaults**: Default teacher = `coach_arthur`, voice = `arthur`, wake word removed personal entries, Cloud Key emptied.
- **Assets**: The avatar packages, idle sequences, personas, reference audios, voice anchors for the other 11 teachers, and the 13-teacher roster document are not included in the package.

### Removed (relative to dev repository)
- Remnants of the old local Qwen3-TTS service (`audiocpp/{models,gpu,venv,cpu}`, ≈7.7 GB, retired on 10-15 and not loaded).
- Development period handover reports (`docs/SpeakTosi_1019_*.md`) and the 13-teacher roster (`docs/AI_COACHES_PROFILES.md`).
- Personal state: conversations/memories, facial feature calibration, window positions, `*.env`.

### Known Limitations
- Windows x64 + NVIDIA GPU only; no fallback gear if VRAM < 11 GB (this package has no cartoon engine).
- Self-contained package is ≈25 GB, please note upload times for cloud drive distribution.
- No automatic update mechanism.

---

## Upstream History (Summary)

| Date | Milestone |
|---|---|
| 10-06 | Dual-engine architecture: Cartoon style (viseme) / Real-person style (MuseTalk) shunting |
| 10-13 | Voice stabilization: CosyVoice2 cloning + per-synthesis parameters (seed/temperature/f0/tolerance) to cure voice drift |
| 10-15 | Multiple teacher voices unified into single port 18091 (removed local Qwen3-TTS, saved 5~6 GB VRAM) |
| 10-16 | Dual TTS worker parallel prefetching, curing "stuck after speaking two sentences" |
| 10-17/18 | Cold start order correction + parallel startup groups; 12 GB VRAM adaptation; startup acceleration |
| 10-19 | Main program self-containment + packaged sharing (share / full / lowmem tiers) |
| 10-20 | **Mini 2-Teacher Edition** (This version) |
