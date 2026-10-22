# AI Coach Voice Design Archive

**English** · [中文说明 →](zh-CN/VOICE_DESIGN_ARCHIVE.md)

> Purpose: Save the **voice design drafts and clone sources** for each coach, facilitating future regeneration / engine switching / cross-platform reproduction.
> Clone sources are uniformly placed in `audiocpp/webui/voice/<preset>_ref.wav` (**24 kHz · mono · 16-bit**).
> The `reference_text` must **exactly match the actual spoken content** of the reference audio (qwen3-tts ICL cloning relies on it), registered in `engine/data/tts_runtime.json`.

## I. Master List of Live Voices

| preset | Coach | Reference Audio | Reference Text | Design Draft |
| :--- | :--- | :--- | :--- | :--- |
| `arthur` | Coach Arthur | `arthur_ref.wav` | Registered in tts_runtime.json | TBD |
| `eleanor` | Coach Eleanor | `eleanor_ref.wav` | Her monologue (see below) | See §2 |
| `marcus` | Coach Marcus | `marcus_ref.wav` | Registered in tts_runtime.json | TBD |
| `sarah` | Coach Sarah | `sarah_ref.wav` | Registered in tts_runtime.json | TBD |
| `kate` | Coach Kate | `kate_ref.wav` | Her monologue (see below) | **See §3 (Complete ElevenLabs Design)** |
| `kai` | Coach Kai | `kai_ref.wav` | Her monologue (see below) | See §4 |
| `mia` | Coach Mia | `mia_ref.wav` | Registered in tts_runtime.json | TBD |
| `lingxue/lingfeng/lingyu/lingyun` | The 4 'Ling' coaches | `*_ref.wav` | Registered in tts_runtime.json | TBD |

## II. Coach Eleanor (Added 10/10)

- **Positioning**: 55 years old · Female · The Global Visionary
- **Voice Requirements**: Capable and composed, resonant yet warm and firm; mid-to-low register, steady, conveying a sense of scale; unhurried pacing.
- **Reference Audio**: `avatar/Coach-Eleanor.mp3` → `eleanor_ref.wav` (24 kHz mono, 25.7 s)
- **reference_text** (her exact recording, word-for-word):
  > Hello, I'm Eleanor. Clear communication begins with clear thinking. Whether you're giving a presentation, joining an international meeting, or defending an idea, confidence comes from structure. Make your point, support it with evidence, and finish with purpose. You don't need to sound forceful to be persuasive. You simply need to sound certain, thoughtful, and prepared.

## III. Coach Kate (Added 10/10) ★ Complete Design Draft

- **Positioning**: 30–35 years old · Female · The Natural Speaker
- **Persona Context (Original Requirement)**: Professional, friendly, bright, confident, but without being overbearing. Clear, engaging, American female voice with an urban feel, perceived age around 30–35; suitable for both daily spoken English and workplace communication.
- **Synthesis Target**: neutral General American; crisp pronunciation, smooth rhythm, medium pace; energetic but controlled; "smiling voice"; avoid sounding overly corporate / overly glamorous / breathy / theatrical / like a radio host. Overall modern, elegant, credible, energetic, suitable for long listening.

### ElevenLabs Voice Design Prompt (Original)
> A warm, clear, confident Black American female voice in her early 30s, with a bright and polished tone, natural warmth, and strong conversational presence. Her voice should sound intelligent, approachable, articulate, and encouraging, like a modern English coach who can move easily between professional communication and relaxed everyday conversation. Use a neutral General American accent with crisp pronunciation, smooth rhythm, and a medium speaking pace. Keep the energy lively but controlled, with a friendly smile in the voice and subtle emotional expressiveness. Avoid sounding overly corporate, overly glamorous, breathy, theatrical, or like a radio host. The overall sound should be modern, elegant, trustworthy, energetic, and comfortable for long listening sessions.

### Audition Script (Original)
> Hi, I'm Kate. Good English isn't about sounding perfect; it's about sounding clear, confident, and natural. We'll work on the way you express your ideas, respond in real conversations, and make your English feel more like you. Whether you're speaking at work, meeting new people, or simply sharing your thoughts, I'll help you sound relaxed, polished, and easy to understand.

### Implementation
- **Reference Audio**: `avatar/Coach-Kate.mp3` → `kate_ref.wav` (24 kHz mono, 24.0 s, her actual recording of the script above)
- **reference_text** = Word-for-word audition script above (registered in tts_runtime.json)
- **Preset Name**: `kate` | **avatar_id**: `coach_kate`

## IV. Coach Kai (Added 10/10)

- **Positioning**: Young Adult · Male · The Cyberpunk Geek
- **Voice Requirements**: Brisk pacing, highly energetic, witty and humorous, frequently uses slang and contractions; young male voice, neither greasy nor whiny.
- **Reference Audio**: `avatar/Coach-Kai.mp3` → `kai_ref.wav` (24 kHz mono, 20.6 s)
- **reference_text** (word-for-word):
  > Hey, I'm Kai. English in real life doesn't always sound like the English in textbooks. People shorten things, joke around, change topics fast, and use expressions you may never see in a classroom. That's what makes it fun. We'll talk about tech, everyday life, internet culture, and the kind of English people actually use when they're just being themselves.

---

*Created: 2026-10-10 | Maintenance: Whenever a new coach is added, record it in this archive first, then register in tts_runtime.json.*
