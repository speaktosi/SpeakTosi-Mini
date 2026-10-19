@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0_env.bat"

if not exist "%PY%" (
  echo [realtime] venv python not found: %PY%
  pause
  exit /b 1
)
REM ============================================================
REM  Realtime voice: VAD(silero) -> ASR -> LLM -> TTS, served on
REM  ws://127.0.0.1:8765/v1/realtime + http://127.0.0.1:8765/realtime/
REM
REM  This script starts ONLY the Python realtime backend. It needs TWO
REM  audio.cpp C++ servers already running (one TTS, one ASR) so both
REM  models can be served at the same time. Start them in separate windows:
REM
REM    run_server.bat qwen3-tts 8080        (TTS, GPU)
REM    run_server_asr.bat                   (ASR on :8081, GPU or CPU)
REM
REM  English demo swaps the TTS model: run_server.bat pocket-tts 8080
REM  (and set AUDIOCPP_TTS_MODEL below to pocket-tts).
REM
REM  All values below are DEFAULTS -- the browser Settings panel overrides
REM  them per session (saved in localStorage) and pushes them via session.update.
REM ============================================================

REM --- TTS: audio.cpp C++ server (must be running on :18080) ---
REM     voice_ref uses an ABSOLUTE path so the C++ server finds it regardless
REM     of its working directory. WEBUI_DIR points at the webui\ folder.
REM     NOTE: :8080 is taken by the local llama-server (LLM brain), so TTS
REM     lives on :18080 here.
set AUDIOCPP_TTS_SERVER=http://127.0.0.1:18080
set AUDIOCPP_TTS_MODEL=qwen3-tts
set "AUDIOCPP_TTS_VOICE_REF=%WEBUI_DIR%\voice\zh-Bowen_man.wav"
set AUDIOCPP_TTS_REF_TEXT=简单来说，人工智能是一门致力于让计算机和机器像人一样思考和行动的科学领域。它的目标是模拟、延伸和扩展人的智能，使机器能够胜任通常需要人类智慧才能完成的任务，比如解决问题、感知环境、理解语言，甚至进行创造。AI是一个非常广泛的领域，它包含了计算机科学、语言学、神经科学，甚至哲学和心理学的多个学科的知识。

REM --- ASR: audio.cpp C++ server (must be running on :8081) ---
set AUDIOCPP_ASR_SERVER=http://127.0.0.1:8081
set AUDIOCPP_ASR_MODEL=qwen3-asr
set AUDIOCPP_ASR_LANGUAGE=zh

REM --- LLM: local llama-server (Qwen3.8-27B) Chat Completions API ---
set AUDIOCPP_LLM_BASE_URL=http://127.0.0.1:8080/v1
if not defined AUDIOCPP_LLM_API_KEY (
  set "AUDIOCPP_LLM_API_KEY=llama"
)
set AUDIOCPP_LLM_MODEL=Qwen3.8-27B-Uncensored-noMTP-Q4_K_M

REM --- Port for the realtime WebSocket backend ---
set AUDIOCPP_REALTIME_PORT=8765

echo [realtime] backend starting on ws://127.0.0.1:8765/v1/realtime
echo [realtime] UI at           http://127.0.0.1:8765/realtime/
echo [realtime] expects TTS @ http://127.0.0.1:18080  (run_server.bat qwen3-tts 18080)
echo [realtime] expects ASR @ http://127.0.0.1:8081  (run_server_asr.bat)
echo [realtime] expects LLM @ http://127.0.0.1:8080  (local llama-server, Qwen3.8-27B)
echo.

cd /d "%~dp0"
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:%AUDIOCPP_REALTIME_PORT%/realtime/'"
"%PY%" "%WEBUI_DIR%\realtime_server.py"
endlocal
pause
