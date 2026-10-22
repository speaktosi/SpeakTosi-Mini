# Customize Your Coach Avatar (Digital Human)

**English** · [中文说明 →](zh-CN/custom-avatar.md)

Engine = LiveTalking (MuseTalk V15). Changing the avatar = preparing a source video **mainly front-facing, with mouth closed**, running avatar generation once, and obtaining the `engine/data/avatars/<your_name>/` asset pack.

## Source Video Requirements

- 1080p or higher, front-facing half-body, uniform lighting;
- **Mouth closed entirely or very little speaking** (the mouth is driven in real-time by the model; opening the mouth in the source video will cause visual glitches);
- 10~60 seconds looped is sufficient (head-to-tail connection is more natural).

## Steps

1. Place the video anywhere in `tools/`.
2. Run avatar generation (in the packaged engine environment):
   ```bat
   engine\venv\Scripts\python.exe engine\genavatar.py --video 你的视频.mp4 --avatar_id my_teacher --model museavatar_v15
   ```
   (The generation process takes about 10~30 minutes, outputting latents + source frames to `engine/data/avatars/my_teacher/`.)
3. Change the `avatar_id` in `app/backend/config/app.json` to `my_teacher`.
4. System Tray → "Restart Service Stack" (重启服务栈).
5. Synchronously update the avatar image: `app/assets/teacher_bust.png` (overlay avatar, recommend 512px PNG with transparent background).

## VRM 3D Mode

`app/assets/` comes with three VRM example models; the settings page allows toggling between 2D video / 3D VRM rendering modes.
