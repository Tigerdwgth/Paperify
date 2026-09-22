"""compose 输入的下标对齐 + TTS 分段音频的保全。

覆盖:
1. compress_blog_assignments: blog/示意外部视频的 key 是 scene_defs 下标, 必须
   和 narration_audios / scene_image_paths 一样按 rendered_indices 压缩成
   scene_videos 下标, 否则任一 scene 渲染失败 (或 PAPERIFY_METHOD_ONLY 只渲
   MethodScene) 就错位, 成片张冠李戴;
2. run() 真的把压缩后的映射交给 compose;
3. generate_tts: 拼接/写盘抛异常时, 已经落盘的分段 mp3 必须保留在 _audio_parts,
   否则 compose 拿到 [] -> 全片静音。
"""
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
for p in (ROOT, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)


# ---------------------------------------------------------------- 下标压缩


def test_compress_keeps_index_when_nothing_dropped():
    from manim_engine import compress_blog_assignments
    assert compress_blog_assignments({2: "clip.mp4"}, [0, 1, 2, 3]) == {2: "clip.mp4"}


def test_compress_shifts_after_failed_scene():
    """TitleScene 渲染失败 -> 后面的 scene 在 scene_videos 里整体前移一位。"""
    from manim_engine import compress_blog_assignments
    assert compress_blog_assignments({3: "blog.mp4"}, [1, 2, 3]) == {2: "blog.mp4"}


def test_compress_method_only_ablation():
    """PAPERIFY_METHOD_ONLY=1 只渲 MethodScene (旧 idx 2) -> 新 idx 0。"""
    from manim_engine import compress_blog_assignments
    assert compress_blog_assignments({2: "blog.mp4"}, [2]) == {0: "blog.mp4"}


def test_compress_drops_unrendered_assignment():
    from manim_engine import compress_blog_assignments
    out = compress_blog_assignments({1: "dropped.mp4", 3: "kept.mp4"}, [0, 3])
    assert out == {1: "kept.mp4"}


def test_compress_empty_assignments():
    from manim_engine import compress_blog_assignments
    assert compress_blog_assignments({}, [0, 1]) == {}


# ---------------------------------------------------------------- run() 集成


def test_run_hands_compressed_blog_index_to_compose(tmp_path, monkeypatch):
    """第一幕渲染失败时, compose 拿到的 blog 映射必须指向压缩后的下标。"""
    import manim_engine as me
    from manim_engine import ManimEngine

    monkeypatch.chdir(tmp_path)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "blog_meta.json").write_text(
        json.dumps({"clip_meta": [{"path": "clip0.mp4"}]}), encoding="utf-8")

    engine = ManimEngine(
        paper_text="",
        structured_plan={
            "opening": {"script": "开场"},
            "intro": {"script": "背景"},
            "method": {"script": "方法"},
            "results": {"script": "结果"},
        },
        output_dir=str(tmp_path / "manim_out"),
    )

    monkeypatch.setattr(engine, "load_pipeline_images",
                        lambda: {"opening": [], "intro": [], "method": [], "results": []})

    import src.blog_video_overlay as bvo
    # ResultsScene (scene_defs 下标 3) 用 blog clip 替代 manim 渲染
    monkeypatch.setattr(bvo, "assign_blog_clips_to_scenes",
                        lambda *a, **k: {3: "/blog/results_clip.mp4"})

    monkeypatch.setattr(engine, "generate_manim_code", lambda sdef: "code")

    def _render(code, scene_name, quality="medium", fmt="mp4"):
        if scene_name == "TitleScene":
            return None  # 第一幕渲染失败 -> 后面全部前移
        return f"/videos/{scene_name}.mp4"

    monkeypatch.setattr(engine, "render_scene", _render)

    captured = {}

    def _compose(scene_videos, **kwargs):
        captured["scene_videos"] = scene_videos
        captured["kwargs"] = kwargs
        return str(tmp_path / "final.mp4")

    monkeypatch.setattr(engine, "compose", _compose)

    class _Ffmpeg:
        returncode = 1
        stdout = b""
        stderr = b""

    monkeypatch.setattr(me.subprocess, "run", lambda *a, **k: _Ffmpeg())

    out = engine.run(tts=False)

    assert out
    assert captured["scene_videos"] == [
        "/videos/IntroScene.mp4", "/videos/MethodScene.mp4", "/blog/results_clip.mp4"]
    assert captured["kwargs"]["blog_scene_videos"] == {2: "/blog/results_clip.mp4"}, \
        "blog 外部视频用了未压缩的 scene_defs 下标, compose 会张冠李戴"


# ---------------------------------------------------------------- TTS 分段


def test_audio_parts_survive_concat_failure(tmp_path, monkeypatch):
    """concatenate/write_audiofile 抛异常时, 分段 mp3 仍要留给 compose 逐段配音。"""
    import manim_engine as me
    from manim_engine import ManimEngine
    import src.utils.audio_helpers as ah

    engine = ManimEngine(paper_text="", structured_plan={},
                         output_dir=str(tmp_path / "manim_out"))

    monkeypatch.setattr(ah, "get_tts_config", lambda: ("cosyvoice-v2", "longxiaochun_v2"))
    monkeypatch.setattr(ah, "synthesize_tts", lambda text: b"ID3-fake-mp3-bytes")

    def _boom(path):
        raise OSError("moviepy 读不了这个 mp3")

    monkeypatch.setattr(me, "AudioFileClip", _boom)

    out = engine.generate_tts([{}, {}], ["第一段旁白", "第二段旁白"])

    assert out is None  # 合成音轨确实失败了
    parts = getattr(engine, "_audio_parts", [])
    assert len(parts) == 2 and all(parts), "分段 mp3 已在盘上却被丢掉 -> 全片静音"
    assert all(os.path.exists(p) for p in parts)


def test_audio_parts_align_with_failed_segment(tmp_path, monkeypatch):
    """单段合成失败用 None 占位, 与 scene 保持一一对齐 (原有行为不回归)。"""
    import manim_engine as me
    from manim_engine import ManimEngine
    import src.utils.audio_helpers as ah

    engine = ManimEngine(paper_text="", structured_plan={},
                         output_dir=str(tmp_path / "manim_out"))

    monkeypatch.setattr(ah, "get_tts_config", lambda: ("cosyvoice-v2", "longxiaochun_v2"))

    def _synth(text):
        if "第二段" in text:
            raise RuntimeError("TTS 服务 500")
        return b"ID3-fake-mp3-bytes"

    monkeypatch.setattr(ah, "synthesize_tts", _synth)
    monkeypatch.setattr(me, "AudioFileClip", lambda path: (_ for _ in ()).throw(OSError("boom")))

    engine.generate_tts([{}, {}, {}], ["第一段旁白", "第二段旁白", ""])

    parts = engine._audio_parts
    assert len(parts) == 3
    assert parts[0] and parts[1] is None and parts[2] is None
