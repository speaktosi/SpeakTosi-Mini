# Customize Your Coach Voice (Local TTS Cloning)

**English** · [中文说明 →](zh-CN/custom-voice.md)

The `audiocpp/` in the package is the local voice service (qwen3-tts, 0.6B base model). **Changing voice = providing a reference audio + a configuration file**, completely offline.

## Steps

1. Record a **10~30 second** clean human voice sample (no background noise, normal speaking rate), save as `wav`, either 24k or 44.1k sample rate is fine.
2. Place the sample into `audiocpp/voices/` (create the directory).
3. Edit `tools/scripts/tts_demo_voice.json` (this is a template, `{AUDIOCPP_ROOT}` will be automatically rendered by the watchdog as the local absolute path):
   - Point the reference audio path to your new sample;
   - Change `id` to your own voice name (e.g., `my-voice`).
4. System Tray → "Restart Watchdog" (重启看门狗), the new voice takes effect.
5. Desktop App → Settings page → Audition, confirm the voice tone and speaking rate (±25% speed change does not alter the tone—WSOLA pitch-preserving algorithm).

## Want Higher Similarity?

The 0.6B base model can be fine-tuned using your own voice data (a few hours of annotated data can significantly improve fidelity). The fine-tuning script entry point is in `tools/scripts/`, and the open-source community also provides qwen3-tts training recipes.
