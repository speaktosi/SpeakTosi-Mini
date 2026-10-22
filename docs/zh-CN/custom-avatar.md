# 定制你的老师形象（数字人）

引擎 = LiveTalking (MuseTalk V15)。换形象 = 准备一段**正脸、闭嘴为主**的源视频，跑一次 avatar 生成，得到 `engine/data/avatars/<你的名字>/` 资产包。

## 源视频要求

- 1080p 或更高，正面半身，光照均匀；
- **全程闭嘴或极少说话**（嘴部由模型实时驱动，源视频张嘴会穿帮）；
- 10~60 秒循环即可（首尾相接更自然）。

## 步骤

1. 视频放入 `tools/` 任意位置。
2. 运行形象生成（包内引擎环境）：
   ```bat
   engine\venv\Scripts\python.exe engine\genavatar.py --video 你的视频.mp4 --avatar_id my_teacher --model museavatar_v15
   ```
   （生成过程约 10~30 分钟，产出 latents + 源帧到 `engine/data/avatars/my_teacher/`。）
3. 把 `app/backend/config/app.json` 的 `avatar_id` 改成 `my_teacher`。
4. 托盘 → 「重启服务栈」。
5. 同步更新形象图：`app/assets/teacher_bust.png`（浮层头像，建议 512px PNG 透明底）。

## VRM 3D 模式

`app/assets/` 自带三个 VRM 示例模型；设置页可切换 2D 视频 / 3D VRM 渲染模式。
