"""src/ 源码完整性检查。

2026-09-21 在仓库里捞出两个**已提交进 HEAD** 的语法损坏文件:

- ``src/ai_image_generator.py``: 文件末尾粘进了代码生成模型的退化输出——一个
  markdown 围栏 + 5.5KB 的模型独白("I've created the file... We'll call.
  We'll run. We'll check." 循环刷了几百遍), 整个文件 ``unmatched '}'``。
- ``src/gui.py``: 一个 ``if __name__ == "__main__":`` 块被误粘到文件中段,
  ``IndentationError``。

两个文件都活了好几个月没被发现, 原因是**没有任何模块 import 它们**——
流水线跑不到, 单测覆盖不到, CI 也就照样绿。所以这里不依赖 import,
直接把 src/ 下每个 .py 都过一遍语法解析: 只要有文件坏了就立刻红。
"""

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

# 跳过的目录: 缓存、产物、第三方参考代码
_SKIP_DIRS = {"__pycache__", "output", "pic", "cache", "cache_resnet", "pic_resnet"}

# 代码生成模型退化输出的尾部特征。放在文件末尾的 markdown 围栏几乎只有一个来源:
# 把模型回复整段写进了 .py。prompt 字符串内部的围栏不会落在文件最后一行。
_MARKDOWN_FENCE = "```"


def _iter_src_files():
    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            # .bak / .bak_<ts> 是历史备份, 不是在跑的代码
            if ".bak" in name:
                continue
            yield os.path.join(dirpath, name)


SRC_FILES = sorted(_iter_src_files())


def test_src_tree_is_not_empty():
    """保证下面的参数化不会因为路径写错而变成 0 个用例(永真)。"""
    assert len(SRC_FILES) > 50, f"src/ 下只扫到 {len(SRC_FILES)} 个 py 文件, 路径可能不对"


@pytest.mark.parametrize("path", SRC_FILES, ids=lambda p: os.path.relpath(p, SRC))
def test_source_file_parses(path):
    """每个 src/*.py 都必须能语法解析, 哪怕没人 import 它。"""
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    try:
        ast.parse(source, filename=path)
    except SyntaxError as exc:
        pytest.fail(
            f"{os.path.relpath(path, ROOT)} 语法错误: 第 {exc.lineno} 行 {exc.msg}\n"
            "若是代码生成工具写坏的文件, 检查文件末尾有没有混进模型输出。"
        )


@pytest.mark.parametrize("path", SRC_FILES, ids=lambda p: os.path.relpath(p, SRC))
def test_source_file_has_no_trailing_model_output(path):
    """文件末尾不该出现 markdown 围栏——那是模型回复被整段写进 .py 的标志。

    按行读整个文件, 不能只取末尾若干字符: ai_image_generator.py 那行污染有
    5506 字节, 截末尾 4096 字符正好把行首的围栏截掉, 检测就失效了。
    """
    with open(path, "r", encoding="utf-8") as f:
        trailing = [ln for ln in f.read().split("\n") if ln.strip()]
    if not trailing:
        return
    assert _MARKDOWN_FENCE not in trailing[-1], (
        f"{os.path.relpath(path, ROOT)} 最后一行含 markdown 围栏, "
        "疑似把代码生成模型的回复原样写进了源文件"
    )
