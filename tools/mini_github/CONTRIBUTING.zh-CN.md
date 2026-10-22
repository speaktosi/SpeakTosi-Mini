# 贡献指南 (Contributing)

感谢您对 SpeakTosi Mini 的兴趣！本指南将帮助您了解如何为项目做出贡献。

## 目录
1. [开发环境搭建](#开发环境搭建)
2. [代码结构导航](#代码结构导航)
3. [提交与 PR 流程](#提交与-pr-流程)
4. [测试与自检](#测试与自检)
5. [安全与限制](#安全与限制)

## 开发环境搭建
项目仅支持 **Windows 10/11 x64** 系统。需要 NVIDIA 显卡 (显存 ≥ 12GB，推荐 16GB，RTX 50 系最佳)、内存 ≥ 16GB、磁盘 ≥ 25GB。

1. **Python 环境**：安装 Python 3.11 或 3.12。创建虚拟环境：
   ```cmd
   python -m venv venv
   .\venv\Scripts\activate
   ```
   *注意：引擎部分可能需要相关的专属虚拟环境。*
2. **Electron 前端**：确保 Node.js 已安装，`node_modules` 用于 Electron 依赖，在 `app/` 目录下执行：
   ```cmd
   cd app
   npm install
   ```

## 代码结构导航
- `app/`：Electron 前端代码。
- `app/backend/`：FastAPI 后端 (端口: 8765)。
- `engine/`：数字人渲染引擎 (LiveTalking + MuseTalk V15，端口: **8011**)，以及克隆语音 TTS (CosyVoice2-0.5B zero-shot，端口: 18091/18092)。
  *注：本 Mini 版**没有 8010** —— 卡通系 viseme 引擎已裁掉，8011 兼任“老师出现在桌面”窗口的渲染。*
- `engine/aiteacher_watchdog.py`：看门狗脚本，通过 18999 端口维持单实例锁。

## 提交与 PR 流程
1. **分支 (Branch)**：请从主分支切出特性分支。
2. **提交规范 (Commit)**：保持提交信息简洁明了。
3. **Pull Request (PR)**：请使用项目中提供的 PR 模板提交。

## 测试与自检
提交前，请进行**手工冒烟测试**：
1. 启动应用后，检查相关端口是否正常监听：**8011、18091、8765**（本档没有 8010）。
2. 检查托盘状态灯是否正常显示。

再跑一遍**版本不变量自检**（这是本简化版的核心事实：只该有两位老师）：

```cmd
python tools\check_mini_invariants.py
```

它检查 `AVATARS` 是否恰好两位、音色 preset 是否只有两位、看门狗有没有把主位/探针音色指回已裁掉的老师、
`engine/data/avatars` 里有没有多出别的老师。CI 也会跑同一个脚本（见 `.github/workflows/ci.yml`）。

## 安全与限制
- ⚠️ **绝对禁止提交任何 API 密钥 (API Key) 及凭据到仓库！**
- 确保遵循 `.gitignore` 中的规则，禁止提交任何大文件 (例如模型权重、生成的数据和日志)。
- 对话走用户自备的 OpenAI 兼容云端 API Key，应该手工复制 `config/student_keys.example.env` 到 `app/backend/.env` 并配置。