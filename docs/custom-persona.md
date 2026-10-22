# Customize Persona and Teaching Corpus

**English** · [中文说明 →](zh-CN/custom-persona.md)

## Persona (Name + Teaching Style)

Desktop App → Double-click system tray icon → Coach window → Settings page:
- **Name**: Takes effect across the entire pipeline immediately upon saving (interface address, AI self-introduction, review notes title, exported filename), no restart required.
- **System Persona**: Edit the teaching role description directly (e.g., "You are an IELTS speaking examiner, answering concisely and fond of follow-up questions").

The persona is saved to `app/backend/config/persona.json`, which is read immediately by both the backend and the engine (`engine/llm.py` hot-reloading, mtime-aware).

## Corpus (Optional Advanced)

Conversations default to the LLM API you configured. If you want **offline/private corpus teaching**, you can turn the corpus into a vector database and integrate it into the backend's chat assembly pipeline (at the `_sys_prompt` + message concatenation point in `app/backend/server.py`). The recommended community approach: local llama.cpp + RAG (Retrieval-Augmented Generation). This part is left blank for the open-source version, extend it yourself as needed.

## Review Notes

Floating history panel → "Export Notes" (导出笔记): Automatically generates Markdown organized by conversation turns (dynamic coach name, includes vocabulary lists and mistake highlights), which can be saved and reused as teaching corpus material.
