"""仅运行 Manim 生成（使用已缓存的 structured_plan + paper_text）"""
import os, sys, json, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
os.environ.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "3")

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from manim_engine import ManimEngine

with open("./cache/structured_plan.json") as f:
    plan = json.load(f)
with open("./cache/paper_text.txt") as f:
    paper_text = f.read()

print("Paper:", paper_text[:80])
engine = ManimEngine(paper_text=paper_text, structured_plan=plan, output_dir="./output/manim")
result = engine.run(tts=True, quality="medium", fmt="mp4")
if result:
    print("=== 完成: %s (%.1f MB) ===" % (result, os.path.getsize(result)/1024/1024))
else:
    print("=== 失败 ===")
