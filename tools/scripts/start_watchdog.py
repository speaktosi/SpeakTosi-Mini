# -*- coding: utf-8 -*-
r"""启动 SpeakTosi 看门狗(后台无窗口) + 生成桌面一键启动器(路径为当前安装根).

背景: 旧版写死 D:\Software\LiveTalking(搬家前路径) -> 搬家后点它无效。本版从脚本位置推导根。
"""
import os, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENG = os.path.join(ROOT, 'engine')
PYW = os.path.join(ENG, 'venv', 'Scripts', 'pythonw.exe')
WD = os.path.join(ENG, 'aiteacher_watchdog.py')
DESK = os.path.join(os.path.expanduser('~'), 'Desktop', '启动SpeakTosi看门狗.bat')

lines = [
    '@echo off',
    'REM SpeakTosi watchdog launcher (paths derived from install root)',
    'cd /d "%s"' % ENG,
    'start "SpeakTosiWatchdog" /min "%s" "%s"' % (PYW, WD),
    'exit',
]
open(DESK, 'wb').write(('\r\n'.join(lines) + '\r\n').encode('gbk', 'replace'))
print('desktop launcher:', DESK)

ps = ("$env:PYTHONPATH=$null; $env:PYTHONHOME=$null; $env:PYTHONIOENCODING='utf-8'; "
      "Start-Process -FilePath '%s' -ArgumentList '%s' -WorkingDirectory '%s' -WindowStyle Hidden" % (PYW, WD, ENG))
subprocess.run(['powershell', '-NoProfile', '-Command', ps], timeout=60)
print('watchdog launched (hidden)')
