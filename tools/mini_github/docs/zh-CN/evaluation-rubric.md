# SpeakTosi · 会话评测表 / Session Evaluation Rubric（中英双显版 Bilingual CN-EN）

> 适用对象：时长 ≥ 5 分钟的英语对话会话。报告不是成绩单，是一份「语言体检 / language physical」。
> Scope: English conversation sessions of 5+ minutes. This report is not an exam score — it is a "language check-up".

评估总纲 / Governing Scale：
**Accuracy → Naturalness → Fluency → Precision → Depth**（准确性 → 自然度 → 流畅度 → 精确度 → 深度）
核心问题永远是「能不能自然地表达自己的意思」，而不是「用了多少高级词汇」。
The core question is always *"Can you express yourself naturally?"* — not *"How many fancy words did you use?"*

---

## 评估五维度 / The Five Dimensions

### 1. 用语准确性 / Word Choice & Usage
检查用户使用的单词、短语、搭配、固定表达是否准确、自然、符合语境。
Check whether the user's words, phrases, collocations and fixed expressions are accurate, natural, and context-appropriate.

**四级语感区分 / Four-level nuance scale：**

| 等级 Level | 含义 Meaning | 报告处理 How to report |
|---|---|---|
| technically correct | 技术上成立，但生硬/直译感强 | 指出，给自然替代 |
| grammatically correct but unnatural | 语法全对，母语者却不这么说 | **重点纠正对象**（"意思对，但母语者通常不会这么说"） |
| natural and idiomatic | 地道自然 | **不要动它**——不为了显示水平而强行修改已自然的表达 |
| unusually precise or sophisticated | 罕见地精确/高级 | 高亮表扬，说明高在哪里、什么场景可复用 |

**红线 / Hard rule**：不要为了显示水平而强行修改本来已经自然的表达。
Never "improve" an expression that is already natural just to show off.

### 2. 发音与语音识别问题 / Pronunciation & Speech Recognition
根据本次语音识别结果，找出：用户想说的词被 ASR 听成了另一个词；同一个词多次识别错误；因重音、元音、辅音、连读或语速导致识别困难的词。
From this session's ASR results, flag: words heard as other words; the same word mis-recognized repeatedly; words troubled by stress, vowels, consonants, linking, or speed.

**必须区分 / Must distinguish：**
- Possible pronunciation issue（疑似发音问题）
- ASR recognition error（识别引擎自身错误）

仅凭文字转录无法确定原因时，**不得武断判断**，标注：
> "Possible pronunciation issue — worth checking."（可能存在发音问题——值得核实。）

若系统留有原始音频（本包已留，存 `eval_cache/audio/`），进一步关注：word stress（单词重音）、sentence stress（句子重音）、intonation（语调）、connected speech（连读）、rhythm（节奏）、reduction（弱读）及影响可懂度的发音习惯。
With original audio available, also examine: word stress, sentence stress, intonation, connected speech, rhythm, reduction, and intelligibility-affecting habits.

### 3. 基础语法问题 / Grammar
只挑真正值得改正的错误，不把小问题都算成错误。按优先级 / Only errors worth fixing, in priority order:
1. 明确语法错误 / clear grammatical errors
2. 容易重复出现的错误 / recurring slip patterns
3. 正确但显得不自然的结构 / correct-but-unnatural structures
4. 更高级、更自然的句型替代 / more advanced, natural alternatives

每个问题格式 / Per item: **原句 → 修改后 → 简短原因 → 更自然的表达（如有必要）**
Original → Corrected → Why → More natural option (if needed).

**红线 / Hard rule**：不要为了追求"高级"而把用户原本正确、自然的句子改复杂。
Never complicate a sentence that was already correct and natural.

### 4. 逻辑条理与语言流畅度 / Clarity, Coherence & Fluency
评估用户能否 / Whether the user can:
- 清楚表达主要观点 state the main point clearly
- 合理组织信息 organize information sensibly
- 使用连接词推进思路 use connectors to advance the line of thought
- 避免重复、绕圈或突然跳跃 avoid repetition, circling, abrupt jumps
- 在停顿、修正和犹豫之后重新组织语言 re-group after pauses, self-corrections, hesitations
- 保持自然的 conversational flow

**核心观察 / Core observation**：听起来像是在翻译中文，还是像在用英语直接思考？
Does it sound like translated Chinese, or like thinking directly in English?

逻辑混乱处，不只指出问题，还要给出更自然的组织方式。
Where logic tangles, don't just flag it — show a better way to structure it.

### 5. 语言深度与表达层次 / Idiomatic & Advanced Expression
全面扫描本次对话中用户**主动使用**的 / Scan for what the user actually produced:
idioms · phrasal verbs · collocations · nuanced vocabulary · sophisticated sentence structures · relative clauses · noun clauses · adverbial clauses · participial constructions · conditionals · rhetorical devices（contrast / parallelism / emphasis / metaphor / understatement 等）

凡本次真正用得好的表达必须【高亮】，并说明：为什么好 / 高在哪里 / 什么场景值得复用。
Genuinely well-used expressions must be highlighted, with: why it works, what elevates it, where to reuse it.

若本次未使用某类高级表达，**不得批评"你的英语不够高级"**，而应从本次话题中挑 2–4 个最适合加入的表达：
If the user used none, never scold — instead pick 2–4 upgrades rooted in THIS session's topic:

> 普通 / Plain: "I was very surprised."
> 更自然有层次 / Sharper: "I was genuinely caught off guard."
> 说明 / Why: 增加了情绪的具体性，比 very surprised 更有画面感。

**红线 / Hard rule**：推荐必须建立在本次会话内容上，不得随机罗列高级词汇。
All suggestions must come out of this session's content — never a random fancy-word list.

---

## 报告固定结构 / Report Sections（每次会话结束后生成）

**SESSION ENDED — ENGLISH REVIEW ｜ 会话评测**

| 节 | 内容 Content | 格式 Format |
|---|---|---|
| A. What You Did Well ｜ 亮点表现 | 本次最值得肯定的 3–5 个真实表现，必须引用用户原话 | quote + why |
| B. Word Choice & Naturalness ｜ 用语与自然度 | 最值得修改的用语 | Original → Better → Why |
| C. Pronunciation / ASR Flags ｜ 发音/识别疑点 | 疑似发音或识别问题 | Word → ASR heard → Possible issue → Pron focus；证据不足必须写 "Insufficient evidence from transcription alone." |
| D. Grammar ｜ 语法 | 只列真正值得注意的 | Original → Corrected → Explanation |
| E. Fluency & Logic ｜ 流畅与逻辑 | 口语组织整体观察，1–3 个最影响质量的问题+改进方式 | observation → fix |
| F. Language Depth ｜ 语言深度 | 高亮本次真实用到的好表达；列 2–4 个"下次可升级"并结合本次话题造句 | highlight + upgrade |
| G. One-Level-Up Version ｜ 升一档示范 | 选用户一段真实表达，提升到更自然、成熟、精确的成年人英语（不是文学英语） | before → after |
| H. Final Takeaway ｜ 三条建议 | 只有 3 条最重要建议，可直接用于下一次对话 | 3 bullets max |

**附加栏 / Extra ladder（对照升级栏，训练"下一次说得更好"）**：
每个升级点给出三级阶梯 / every upgrade gets three steps:

> I think it was very difficult.（基础正确）
> → I found it surprisingly challenging.（自然）
> → What made it challenging was...（精确·有层次）

**语气 / Voice**：Mary Alice 式——温柔、敏锐、略带洞察力。诚实但不打击；指出问题但不贴标签；偶尔一句有画面感的旁白总结本次表现。
Mary Alice style: gentle, perceptive, a touch of wry insight. Honest but never deflating; diagnose, don't label. One vivid one-liner to close.

**每份报告必须以此收尾 / Every report ends with:**

> "You don't need more complicated English.
> You need more precise English."
> （你需要的不是更复杂的英语，而是更精确的英语。）

必要时补充 / Optionally add:

> "The goal is not to sound impressive. The goal is to sound like yourself — only clearer, sharper, and more natural."
> （目标不是听起来厉害，而是听起来像你自己——只是更清楚、更锋利、更自然。）

---

## 重要原则 / Governing Principles
1. 不因复杂词汇多用而自动判定水平高；不因几个基础错误而否定整体表现。
   Don't rate sophistication by big words; don't erase merit over a few slips.
2. 优秀表达必须来自用户真实使用；改进建议必须服务于下一次真实交流。
   Highlights must be the user's real words; upgrades must be usable in the next real conversation.
3. 不为制造学习内容而人为增加错误或高级表达。
   Never invent errors or force sophistication to manufacture "learning content".
4. 评测触发门槛：会话时长 ≥ 5 分钟；关闭老师窗口即视为会话结束，评测自动开始，完成后在「会话记录 → 评测」中查看，历史记录保留，可自行导出（MD）或删除。
   Trigger: sessions ≥ 5 min. Closing the teacher window ends the session; the review runs automatically, then lives in Session Log → Review tab with export (MD) and delete.
