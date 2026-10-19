#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mini 版不变量自检 —— 这个仓库只该有两位老师(CI 与本地都能跑, 零依赖)。

为什么单独一个脚本: "简化版"不是一次性动作, 是**必须一直成立的事实**。
有人把别的老师/卡通素材/额外音色加回来时, CI 应该立刻红。

只检查发布件事实, 不加载任何模型/环境(因此 CI 里秒级通过):
  1. app/backend/server.py 的 AVATARS 恰好 = {coach_arthur, coach_mia}(AST 精确解析)
  2. tools/scripts/tts_demo_voice.json 的 voice_presets ⊆ {arthur, mia}(界面音色下拉的来源)
  3. engine/data/voice_registry.json 只含这两位 + default 别名
  4. engine/aiteacher_watchdog.py 的启动主位/REF_FILE/深探音色不再指向已裁掉的老师
  5. engine/data/avatars/ 若存在, 只允许这两位老师的形象包

退出码非 0 = 不变量被破坏。
"""
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEACHERS = ("coach_arthur", "coach_mia")
VOICES = ("arthur", "mia")
fail = []


def read(rel):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8", errors="ignore") as f:
        return f.read()


# 1) AVATARS: 恰好两位
src = read("app/backend/server.py")
if src is None:
    fail.append("缺 app/backend/server.py")
else:
    ids = None
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "AVATARS" for t in node.targets):
            ids = []
            for el in getattr(node.value, "elts", []):
                if not isinstance(el, ast.Dict):
                    continue
                for k, v in zip(el.keys, el.values):
                    if isinstance(k, ast.Constant) and k.value == "avatar_id" and isinstance(v, ast.Constant):
                        ids.append(v.value)
    if ids is None:
        fail.append("server.py 里找不到 AVATARS 列表")
    elif set(ids) != set(TEACHERS):
        fail.append("AVATARS 不是恰好两位老师: %s (期望 %s)" % (sorted(ids), sorted(TEACHERS)))
    else:
        print("[ok] AVATARS = %s" % sorted(ids))

# 2) 界面音色下拉的来源
tpl = read("tools/scripts/tts_demo_voice.json")
if tpl is None:
    fail.append("缺 tools/scripts/tts_demo_voice.json")
else:
    names = []
    for m in json.loads(tpl).get("models", []):
        names += list((m.get("voice_presets") or {}).keys())
    extra = sorted(set(names) - set(VOICES))
    if extra or not names:
        fail.append("音色 preset 应为 %s, 实为 %s" % (sorted(VOICES), sorted(set(names))))
    else:
        print("[ok] 音色 preset = %s" % sorted(set(names)))

# 3) 音色注册表
reg = read("engine/data/voice_registry.json")
if reg is None:
    fail.append("缺 engine/data/voice_registry.json")
else:
    keys = set(json.loads(reg).keys())
    extra = sorted(keys - set(VOICES) - {"default"})
    if extra:
        fail.append("voice_registry 有多余音色: %s" % extra)
    else:
        print("[ok] voice_registry = %s" % sorted(keys))

# 4) 看门狗实参
wd = read("engine/aiteacher_watchdog.py")
if wd is None:
    fail.append("缺 engine/aiteacher_watchdog.py")
else:
    bad = [t for t in ("'--avatar_id', 'lingxue_cartoon'", "'--avatar_id', 'aiteacher_demo'",
                       "'--REF_FILE', 'lingxue'", "'--REF_FILE', 'default'", "'voice': 'lingxue'")
           if t in wd]
    if bad:
        fail.append("看门狗仍把主位/REF_FILE/深探音色指向已裁掉的老师: %s" % bad)
    else:
        print("[ok] 看门狗主位 / REF_FILE / 深探音色均指向保留阵容")

# 5) 形象包目录(仓库档不带素材; 若带了必须只有两位)
av = os.path.join(ROOT, "engine", "data", "avatars")
if os.path.isdir(av):
    got = {d for d in os.listdir(av) if os.path.isdir(os.path.join(av, d))}
    extra = sorted(got - set(TEACHERS))
    if extra:
        fail.append("engine/data/avatars 有多余老师: %s" % extra)
    else:
        print("[ok] 形象包目录 = %s" % sorted(got))

if fail:
    print("\n[FAIL] Mini 不变量被破坏:")
    for f in fail:
        print("  - " + f)
    sys.exit(1)
print("\n[ok] Mini 不变量全部通过: 只有两位老师(Arthur / Mia)。")