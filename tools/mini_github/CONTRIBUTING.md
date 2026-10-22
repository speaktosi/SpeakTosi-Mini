# Contributing Guidelines

> 🌐 English · [中文 →](CONTRIBUTING.zh-CN.md)

Thank you for your interest in SpeakTosi-Mini! This guide will help you understand how to contribute to the project.

## Table of Contents
1. [Development Environment Setup](#development-environment-setup)
2. [Code Structure Navigation](#code-structure-navigation)
3. [Commit and PR Process](#commit-and-pr-process)
4. [Testing and Self-Check](#testing-and-self-check)
5. [Security and Limitations](#security-and-limitations)

## Development Environment Setup
The project only supports **Windows 10/11 x64** systems. It requires an NVIDIA graphics card (VRAM ≥ 12GB, 16GB recommended, RTX 50 series optimal), RAM ≥ 16GB, and disk space ≥ 25GB.

1. **Python Environment**: Install Python 3.11 or 3.12. Create a virtual environment:
   ```cmd
   python -m venv venv
   .\venv\Scripts\activate
   ```
   *Note: The engine part may require its own specific virtual environment.*
2. **Electron Frontend**: Ensure Node.js is installed. `node_modules` is used for Electron dependencies. Run the following in the `app/` directory:
   ```cmd
   cd app
   npm install
   ```

## Code Structure Navigation
- `app/`: Electron frontend code.
- `app/backend/`: FastAPI backend (Port: 8765).
- `engine/`: Digital human rendering engine (LiveTalking + MuseTalk V15, Port: **8011**), and cloned voice TTS (CosyVoice2-0.5B zero-shot, Ports: 18091/18092).
  *Note: This Mini version **does not have 8010** — the cartoon style viseme engine has been cut, and 8011 handles the rendering of the "teacher appears on desktop" window.*
- `engine/aiteacher_watchdog.py`: Watchdog script, maintains a single-instance lock via port 18999.

## Commit and PR Process
1. **Branch**: Please cut off a feature branch from the main branch.
2. **Commit**: Keep commit messages concise and clear.
3. **Pull Request (PR)**: Please submit using the provided PR template in the project.

## Testing and Self-Check
Before submitting, please perform a **manual smoke test**:
1. After starting the application, check whether the relevant ports are listening normally: **8011, 18091, 8765** (there is no 8010 in this version).
2. Check if the tray status light displays normally.

Run the **version invariant self-check** again (this is the core fact of this simplified version: there should be exactly two teachers):

```cmd
python tools\check_mini_invariants.py
```

It checks whether `AVATARS` contains exactly two teachers, whether there are only two voice presets, whether the watchdog has pointed the primary/probe voices back to a cut teacher, and whether there are any extra teachers in `engine/data/avatars`. CI will also run the same script (see `.github/workflows/ci.yml`).

## Security and Limitations
- ⚠️ **It is strictly prohibited to commit any API keys or credentials to the repository!**
- Ensure you follow the rules in `.gitignore`, and do not commit any large files (e.g., model weights, generated data, and logs).
- The chat uses a user-provided OpenAI-compatible cloud API Key. You should manually copy `config/student_keys.example.env` to `app/backend/.env` and configure it.
