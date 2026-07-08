"""Verify CJK rendering on K1 — used after font fix push."""
import os
os.environ['SDL_VIDEODRIVER'] = 'dummy'
import sys
sys.path.insert(0, '.')
import pygame
pygame.init()
pygame.display.set_mode((800, 480))

from screen.app import load_chinese_font

print("=== Font sizes ===")
for sz in [24, 48, 72]:
    f = load_chinese_font(sz)
    surf_zh = f.render('中文测试', True, (255, 255, 255))
    surf_en = f.render('Hello', True, (255, 255, 255))
    print(f'  size={sz:3}  zh width={surf_zh.get_width():4}  en width={surf_en.get_width():4}')

print()
print("=== Real strings used in the screen ===")
f = load_chinese_font(64)
for s in ['抬腿', '保持', '放下', '训练完成', '请保持静坐', '设备就绪']:
    surf = f.render(s, True, (255, 255, 255))
    expected = len(s) * 60   # ~60px per CJK char at size=64
    status = 'OK' if surf.get_width() >= expected * 0.5 else 'FAIL (no CJK)'
    print(f'  "{s}"  width={surf.get_width():4}  expected~{expected}  [{status}]')

print()
print("=== Render the full IDLE / WORKING scenes ===")
from screen.app import Renderer
import pygame
screen = pygame.display.set_mode((1024, 600))
r = Renderer(screen)
r.render_idle({'state':'IDLE'})
r.render_working({'state':'WORKING','sub_state':'BASELINE','patient':'测试',
                  'progress':{'countdown_s':125.0}})
r.render_working({'state':'WORKING','sub_state':'TRAINING.REP_HOLD','patient':'测试',
                  'progress':{'current_set':1,'sets_total':3,'current_rep':5,
                              'reps_total':12,'countdown_s':2.5}})
print("  All scenes rendered without exception.")
pygame.quit()
