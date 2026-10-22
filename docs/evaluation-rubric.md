# SpeakTosi · Session Evaluation Rubric

**English** · [中文说明 →](zh-CN/evaluation-rubric.md)

> Scope: English conversation sessions of 5+ minutes. This report is not an exam score — it is a "language check-up".

Governing Scale:
**Accuracy → Naturalness → Fluency → Precision → Depth**
The core question is always *"Can you express yourself naturally?"* — not *"How many fancy words did you use?"*

---

## The Five Dimensions

### 1. Word Choice & Usage
Check whether the user's words, phrases, collocations and fixed expressions are accurate, natural, and context-appropriate.

**Four-level nuance scale:**

| Level | Meaning | How to report |
|---|---|---|
| technically correct | Technically valid, but feels stiff or literally translated | Point it out, provide a natural alternative |
| grammatically correct but unnatural | Perfect grammar, but native speakers don't say this | **Primary correction target** ("The meaning is right, but a native speaker usually wouldn't phrase it this way") |
| natural and idiomatic | Authentic and natural | **Do not touch it** — never forcefully modify an already natural expression just to show off |
| unusually precise or sophisticated | Rarely precise or advanced | Highlight and praise it, explain why it's good and in what context it can be reused |

**Hard rule**: Never "improve" an expression that is already natural just to show off.

### 2. Pronunciation & Speech Recognition
From this session's ASR results, flag: words heard as other words; the same word mis-recognized repeatedly; words troubled by stress, vowels, consonants, linking, or speed.

**Must distinguish:**
- Possible pronunciation issue
- ASR recognition error

When the cause cannot be determined from the text transcript alone, **do not judge arbitrarily**, label it as:
> "Possible pronunciation issue — worth checking."

With original audio available (kept in this package under `eval_cache/audio/`), further examine: word stress, sentence stress, intonation, connected speech, rhythm, reduction, and intelligibility-affecting habits.

### 3. Grammar
Only errors worth fixing, in priority order:
1. clear grammatical errors
2. recurring slip patterns
3. correct-but-unnatural structures
4. more advanced, natural alternatives

Per item: **Original → Corrected → Why → More natural option (if needed).**

**Hard rule**: Never complicate a sentence that was already correct and natural.

### 4. Clarity, Coherence & Fluency
Whether the user can:
- state the main point clearly
- organize information sensibly
- use connectors to advance the line of thought
- avoid repetition, circling, abrupt jumps
- re-group after pauses, self-corrections, hesitations
- maintain a natural conversational flow

**Core observation**: Does it sound like translated Chinese, or like thinking directly in English?

Where logic tangles, don't just flag it — show a better way to structure it.

### 5. Idiomatic & Advanced Expression
Scan for what the user actually produced:
idioms · phrasal verbs · collocations · nuanced vocabulary · sophisticated sentence structures · relative clauses · noun clauses · adverbial clauses · participial constructions · conditionals · rhetorical devices (contrast / parallelism / emphasis / metaphor / understatement, etc.)

Genuinely well-used expressions must be highlighted, with: why it works, what elevates it, where to reuse it.

If the user used none, **never scold "your English isn't advanced enough"** — instead pick 2–3 upgrades rooted in THIS session's topic:

> Plain: "I was very surprised."
> Sharper: "I was genuinely caught off guard."
> Why: Adds emotional specificity, more vivid than "very surprised".

**Hard rule**: All suggestions must come out of this session's content — never a random fancy-word list.

---

## Report Sections (Generated after every session)

**SESSION ENDED — ENGLISH REVIEW**

| Section | Content | Format |
|---|---|---|
| A. What You Did Well | 3–5 of the most commendable real performances from this session, must quote the user's original words | quote + why |
| B. Word Choice & Naturalness | Phrasing most worth correcting | Original → Better → Why |
| C. Pronunciation / ASR Flags | Suspected pronunciation or recognition issues | Word → ASR heard → Possible issue → Pron focus; if evidence is insufficient you must write "Insufficient evidence from transcription alone." |
| D. Grammar | Only those truly worth noting | Original → Corrected → Explanation |
| E. Fluency & Logic | Overall observation of oral organization, 1–3 issues most impacting quality + ways to improve | observation → fix |
| F. Language Depth | Highlight good expressions actually used this time; list 2–4 "upgrades for next time" and make sentences based on this session's topic | highlight + upgrade |
| G. One-Level-Up Version | Select a piece of the user's real expression and elevate it to more natural, mature, precise adult English (not literary English) | before → after |
| H. Final Takeaway | Only the 3 most important suggestions, directly applicable to the next conversation | 3 bullets max |

**Extra ladder (for training "saying it better next time")**:
Every upgrade gets three steps:

> I think it was very difficult. (Basic correctness)
> → I found it surprisingly challenging. (Natural)
> → What made it challenging was... (Precise, layered)

**Voice**: Mary Alice style: gentle, perceptive, a touch of wry insight. Honest but never deflating; diagnose, don't label. One vivid one-liner to close.

**Every report ends with:**

> "You don't need more complicated English.
> You need more precise English."

Optionally add:

> "The goal is not to sound impressive. The goal is to sound like yourself — only clearer, sharper, and more natural."

---

## Governing Principles
1. Don't rate sophistication by big words; don't erase merit over a few slips.
2. Highlights must be the user's real words; upgrades must be usable in the next real conversation.
3. Never invent errors or force sophistication to manufacture "learning content".
4. Trigger: sessions ≥ 5 min. Closing the teacher window ends the session; the review runs automatically, then lives in Session Log → Review tab with export (MD) and delete.
