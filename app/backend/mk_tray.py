# -*- coding: utf-8 -*-
"""从 teacher_cut.png 生成 16x16 托盘图标"""
from PIL import Image
im = Image.open(r"D:\Software\ai-girlfriend\assets\teacher_cut.png")
# 取头部区域做方形缩略
im2 = im.crop((700, 30, 940, 270)).resize((16, 16), Image.LANCZOS)
im2.save(r"D:\Software\ai-girlfriend\assets\tray.png")
print("tray.png OK")
