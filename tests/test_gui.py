"""src/gui.py 结构回归测试。

2026-09-21 修复的三个缺陷, 都是"文件能提交进仓、一运行才炸"的类型:

1. 一个 `if __name__ == "__main__":` 块被误粘到文件中段, 缩进错乱直接
   IndentationError; 且它调用的 `demo` 要到后面才定义, 就算缩进修好也是
   NameError。
2. 文件里存在两份 `with gr.Blocks(...) as demo:` UI 定义, 后一份静默覆盖
   前一份 —— 前一份精心写的界面全部作废。
3. `process_arxiv_summary` 返回 4 个值, 而 `submit_btn.click` 的 outputs
   只挂了 1 个组件, Gradio 运行期才会报输出数量不匹配。

这三条都能被纯静态分析钉死, 不需要 import gradio, 所以这里走 AST:
既快又不会在 CI 里拉起重依赖或触发 gradio 的联网版本检查。
"""

import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUI_PATH = os.path.join(ROOT, "src", "gui.py")


@pytest.fixture(scope="module")
def gui_tree() -> ast.Module:
    with open(GUI_PATH, "r", encoding="utf-8") as f:
        source = f.read()
    # 缺陷 1 的直接回归: 文件必须能通过语法解析
    return ast.parse(source, filename=GUI_PATH)


def _iter_blocks_with(tree: ast.Module):
    """产出所有 `with gr.Blocks(...)` 语句。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "Blocks"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "gr"
            ):
                yield node


def _iter_main_guards(tree: ast.Module):
    """产出所有顶层 `if __name__ == "__main__":` 块。"""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
        ):
            yield node


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到函数 {name}")


def test_gui_source_parses(gui_tree):
    """缺陷 1: 曾经整个文件 IndentationError。"""
    assert isinstance(gui_tree, ast.Module)


def test_only_one_blocks_ui_definition(gui_tree):
    """缺陷 2: 两份 UI 定义时, 后一份会把前一份整个覆盖掉。"""
    blocks = list(_iter_blocks_with(gui_tree))
    assert len(blocks) == 1, f"gr.Blocks UI 定义应只有一份, 实际 {len(blocks)} 份"


def test_single_main_guard_is_last_statement(gui_tree):
    """缺陷 1: 入口块只能有一个, 且必须在 demo 定义之后(即文件末尾)。"""
    guards = list(_iter_main_guards(gui_tree))
    assert len(guards) == 1, f"入口块应只有一个, 实际 {len(guards)} 个"

    blocks = list(_iter_blocks_with(gui_tree))
    assert guards[0].lineno > blocks[0].lineno, "入口块必须位于 gr.Blocks 定义之后"
    assert guards[0] is gui_tree.body[-1], "入口块应是文件最后一个顶层语句"


def test_handler_returns_are_uniform(gui_tree):
    """所有 return 分支必须返回同样数量的值, 否则 Gradio 输出对不齐。"""
    fn = _find_function(gui_tree, "process_arxiv_summary")
    widths = {
        len(node.value.elts)
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
    }
    assert widths, "process_arxiv_summary 应返回元组"
    assert len(widths) == 1, f"各 return 分支返回值个数不一致: {sorted(widths)}"


def test_click_outputs_match_handler_return_width(gui_tree):
    """缺陷 3: click 的 outputs 数量必须等于 handler 返回值数量。"""
    fn = _find_function(gui_tree, "process_arxiv_summary")
    return_width = next(
        len(node.value.elts)
        for node in ast.walk(fn)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)
    )

    click_calls = [
        node
        for node in ast.walk(gui_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "click"
    ]
    assert click_calls, "submit_btn 必须绑定 click 事件, 否则界面按钮点了没反应"

    for call in click_calls:
        outputs = next(
            (kw.value for kw in call.keywords if kw.arg == "outputs"), None
        )
        assert outputs is not None, "click 必须显式声明 outputs"
        assert isinstance(outputs, ast.List), "outputs 应写成列表, 便于与返回值逐项对齐"
        assert len(outputs.elts) == return_width, (
            f"click outputs 挂了 {len(outputs.elts)} 个组件, "
            f"但 handler 返回 {return_width} 个值"
        )
