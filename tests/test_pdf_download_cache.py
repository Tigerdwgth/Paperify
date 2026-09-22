# -*- coding: utf-8 -*-
"""测试 PDF 下载缓存按「来源 URL」命中, 而不是只看「文件新旧」。

复现的 bug: 缓存路径 ``./cache/cached_pdf.pdf`` 对所有论文是常量, 旧实现只判断
文件 mtime 是否在 1 小时内就复用, 于是多篇模式 (max_papers > 1) 下第 2 篇起
全部读到第 1 篇的 PDF。修复后命中要求「同一来源 URL」+「不超过 1 小时」
两个条件同时成立, URL 记录在 ``./cache/cached_pdf.meta.json``。

只 mock 网络层 (requests.get), 缓存判定逻辑本身走真实代码。
"""
import json
import os
import sys
import time

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "src")
for p in (ROOT, SRC):
    if p not in sys.path:
        sys.path.insert(0, p)

pytest.importorskip("moviepy", reason="paperagent_workflow 依赖 moviepy")

import paperagent_workflow as wf  # noqa: E402

URL_A = "https://arxiv.org/pdf/1111.11111"
URL_B = "https://arxiv.org/pdf/2222.22222"
CACHE_PDF = os.path.join("./cache", "cached_pdf.pdf")
CACHE_META = os.path.join("./cache", "cached_pdf.meta.json")


class _FakeResponse:
    """最小 requests.Response 替身, 内容按 URL 区分。"""

    def __init__(self, content):
        self.content = content
        self.status_code = 200

    def raise_for_status(self):
        return None


def _body(url):
    return ("%%PDF-1.4 body-of %s" % url).encode("utf-8")


@pytest.fixture
def download_calls(monkeypatch, tmp_path):
    """把 requests.get 换成按 URL 返回不同内容的假下载, 并切到临时工作目录。"""
    calls = []

    def _fake_get(url, timeout=60):
        calls.append(url)
        return _FakeResponse(_body(url))

    monkeypatch.setattr("requests.get", _fake_get)
    monkeypatch.chdir(tmp_path)
    os.makedirs("./cache", exist_ok=True)
    return calls


def _read(path):
    with open(path, "rb") as f:
        return f.read()


# ---------------- 缓存键 = URL ----------------

def test_second_url_gets_its_own_pdf(download_calls):
    """两个不同 URL 连续下载: 第 2 个必须真下载, 拿到的是它自己的内容。"""
    path1 = wf.download_if_remote(URL_A)
    content1 = _read(path1)
    path2 = wf.download_if_remote(URL_B)
    content2 = _read(path2)

    assert download_calls == [URL_A, URL_B], "不同 URL 必须各下载一次"
    assert content1 == _body(URL_A)
    assert content2 == _body(URL_B), "第 2 篇读到了第 1 篇的 PDF"


def test_same_url_within_ttl_reuses_cache(download_calls):
    """同一 URL 且在 1 小时内: 复用缓存, 不重复下载。"""
    path1 = wf.download_if_remote(URL_A)
    path2 = wf.download_if_remote(URL_A)

    assert download_calls == [URL_A], "同一 URL 1 小时内应复用缓存"
    assert os.path.abspath(path1) == os.path.abspath(path2)
    assert _read(path2) == _body(URL_A)


def test_same_url_but_stale_redownloads(download_calls):
    """同一 URL 但缓存超过 1 小时: 重新下载 (新鲜度条件不成立)。"""
    wf.download_if_remote(URL_A)
    stale = time.time() - 7200
    os.utime(CACHE_PDF, (stale, stale))

    wf.download_if_remote(URL_A)

    assert download_calls == [URL_A, URL_A], "过期缓存必须重新下载"


def test_legacy_cache_without_meta_redownloads(download_calls):
    """老版本遗留的 cached_pdf.pdf 没有元数据: 视为未命中, 重新下载。"""
    wf.download_if_remote(URL_A)
    os.remove(CACHE_META)

    wf.download_if_remote(URL_A)

    assert download_calls == [URL_A, URL_A], "缺少 URL 元数据时不得盲目复用"


def test_corrupt_meta_treated_as_miss(download_calls):
    """元数据损坏 (半截 JSON): 视为未命中, 不抛异常。"""
    wf.download_if_remote(URL_A)
    with open(CACHE_META, "w", encoding="utf-8") as f:
        f.write('{"url": "https://arxiv')

    wf.download_if_remote(URL_A)

    assert download_calls == [URL_A, URL_A]


def test_meta_records_source_url(download_calls):
    """下载后元数据里记录的就是本次来源 URL。"""
    wf.download_if_remote(URL_B)

    with open(CACHE_META, "r", encoding="utf-8") as f:
        meta = json.load(f)
    assert meta["url"] == URL_B


# ---------------- 元数据读写辅助函数 ----------------

def test_meta_path_derived_from_cache_path():
    assert wf._pdf_cache_meta_path("./cache/cached_pdf.pdf") == "./cache/cached_pdf.meta.json"


def test_read_meta_url_missing_file_returns_empty(tmp_path):
    assert wf._read_pdf_cache_url(str(tmp_path / "nope.meta.json")) == ""


def test_read_meta_url_non_dict_returns_empty(tmp_path):
    meta = tmp_path / "x.meta.json"
    meta.write_text('["not", "a", "dict"]', encoding="utf-8")
    assert wf._read_pdf_cache_url(str(meta)) == ""


def test_write_then_read_meta_roundtrip(tmp_path):
    meta = tmp_path / "sub" / "x.meta.json"
    wf._write_pdf_cache_url(str(meta), URL_A)
    assert wf._read_pdf_cache_url(str(meta)) == URL_A


# ---------------- 本地路径行为不变 ----------------

def test_local_existing_path_untouched(monkeypatch, tmp_path):
    """本地已存在的文件仍然直接返回, 不进缓存逻辑 (老行为不变)。"""
    pdf = tmp_path / "local.pdf"
    pdf.write_bytes(b"%PDF-1.4 local")
    assert wf.download_if_remote(str(pdf)) == str(pdf)


def test_local_missing_path_returns_minus_one(tmp_path):
    """不存在的本地路径仍返回 -1 哨兵 (老行为不变)。"""
    assert wf.download_if_remote(str(tmp_path / "nope.pdf")) == -1
