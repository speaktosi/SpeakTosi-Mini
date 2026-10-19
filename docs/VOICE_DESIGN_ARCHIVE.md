# AI 导师音色设计档案 (Voice Design Archive)

> 用途：保存每位导师**音色的设计稿与克隆源**，便于日后重生成 / 换引擎 / 跨平台复刻。
> 克隆源统一放 `audiocpp/webui/voice/<preset>_ref.wav`（**24 kHz · 单声道 · 16-bit**）。
> `reference_text` 必须与参考音频**实际朗读内容一致**（qwen3-tts ICL 克隆依赖它），登记在 `engine/data/tts_runtime.json`。

## 一、已上线音色总表

| preset | 导师 | 参考音频 | 参考文本 | 设计稿 |
| :--- | :--- | :--- | :--- | :--- |
| `arthur` | Coach Arthur | `arthur_ref.wav` | tts_runtime.json 已登记 | 待补 |
| `eleanor` | Coach Eleanor | `eleanor_ref.wav` | 其自述（见下） | 见 §2 |
| `marcus` | Coach Marcus | `marcus_ref.wav` | tts_runtime.json 已登记 | 待补 |
| `sarah` | Coach Sarah | `sarah_ref.wav` | tts_runtime.json 已登记 | 待补 |
| `kate` | Coach Kate | `kate_ref.wav` | 其自述（见下） | **见 §3（完整 ElevenLabs 设计稿）** |
| `kai` | Coach Kai | `kai_ref.wav` | 其自述（见下） | 见 §4 |
| `mia` | Coach Mia | `mia_ref.wav` | tts_runtime.json 已登记 | 待补 |
| `lingxue/lingfeng/lingyu/lingyun` | 凌字辈 4 位 | `*_ref.wav` | tts_runtime.json 已登记 | 待补 |

## 二、Coach Eleanor（1010 新增）

- **定位**：55 岁 · 女 · 干练豁达导师 (The Global Visionary)
- **音色要求**：干练从容、掷地有声而温暖坚定；中低音区、稳重、有格局感；语速从容不拖沓。
- **参考音频**：`avatar/Coach-Eleanor.mp3` → `eleanor_ref.wav`（24 kHz mono，25.7 s）
- **reference_text**（即其录音原句，逐字）：
  > Hello, I'm Eleanor. Clear communication begins with clear thinking. Whether you're giving a presentation, joining an international meeting, or defending an idea, confidence comes from structure. Make your point, support it with evidence, and finish with purpose. You don't need to sound forceful to be persuasive. You simply need to sound certain, thoughtful, and prepared.

## 三、Coach Kate（1010 新增）★ 完整设计稿

- **定位**：30–35 岁 · 女 · 自然流利导师 (The Natural Speaker)
- **人设口径（原始需求）**：专业、亲切、明亮、自信，但没有强势压迫感。清澈、有感染力、稍带都市感的美式女声，年龄感约 30–35 岁；既能做日常口语，也能做职场沟通。
- **合成目标**：neutral General American；清晰发音、流畅节奏、中等语速；活力但克制；"声音里带笑"；避免过度企业腔 / 过度华丽 / 气声 / 戏剧化 / 电台主持味。整体现代、优雅、可信、有能量、适合长时间收听。

### ElevenLabs Voice Design Prompt（原文）
> A warm, clear, confident Black American female voice in her early 30s, with a bright and polished tone, natural warmth, and strong conversational presence. Her voice should sound intelligent, approachable, articulate, and encouraging, like a modern English coach who can move easily between professional communication and relaxed everyday conversation. Use a neutral General American accent with crisp pronunciation, smooth rhythm, and a medium speaking pace. Keep the energy lively but controlled, with a friendly smile in the voice and subtle emotional expressiveness. Avoid sounding overly corporate, overly glamorous, breathy, theatrical, or like a radio host. The overall sound should be modern, elegant, trustworthy, energetic, and comfortable for long listening sessions.

### 试听台词（原文）
> Hi, I'm Kate. Good English isn't about sounding perfect; it's about sounding clear, confident, and natural. We'll work on the way you express your ideas, respond in real conversations, and make your English feel more like you. Whether you're speaking at work, meeting new people, or simply sharing your thoughts, I'll help you sound relaxed, polished, and easy to understand.

### 落地
- **参考音频**：`avatar/Coach-Kate.mp3` → `kate_ref.wav`（24 kHz mono，24.0 s，即上段台词的本人录音）
- **reference_text** = 上段试听台词逐字（已登记 tts_runtime.json）
- **预设名**：`kate` ｜ **avatar_id**：`coach_kate`

## 四、Coach Kai（1010 新增）

- **定位**：青年 · 男 · 潮流极客助手 (The Cyberpunk Geek)
- **音色要求**：语速轻快、能量充沛、诙谐幽默，常用口头小词与缩略；青年男声、不油不嗲。
- **参考音频**：`avatar/Coach-Kai.mp3` → `kai_ref.wav`（24 kHz mono，20.6 s）
- **reference_text**（逐字）：
  > Hey, I'm Kai. English in real life doesn't always sound like the English in textbooks. People shorten things, joke around, change topics fast, and use expressions you may never see in a classroom. That's what makes it fun. We'll talk about tech, everyday life, internet culture, and the kind of English people actually use when they're just being themselves.

---

*建立日期：2026-10-10 ｜ 维护：每新增导师先落本档，再登记 tts_runtime.json。*