import os, socket, logging


def apply_network_workarounds(profile=None):
    """根据 JSR_NETWORK_PROFILE 环境变量配置网络。当前唯一 profile: gsjts。"""
    profile = profile or os.environ.get("JSR_NETWORK_PROFILE", "")
    if profile != "gsjts":
        return False
    os.environ["HTTP_PROXY"] = os.environ.get("HTTP_PROXY") or "http://127.0.0.1:7890"
    os.environ["HTTPS_PROXY"] = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7890"
    os.environ["http_proxy"] = os.environ["HTTP_PROXY"]
    os.environ["https_proxy"] = os.environ["HTTPS_PROXY"]
    no_proxy_default = "dashscope.aliyuncs.com,aliyuncs.com,aliyun.com,localhost,127.0.0.1"
    os.environ.setdefault("NO_PROXY", no_proxy_default)
    os.environ.setdefault("no_proxy", no_proxy_default)
    for k in ("ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    _orig = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **kw: [r for r in _orig(*a, **kw) if r[0] == socket.AF_INET]
    logging.info("[env_setup] GSJts profile: clash proxy + NO_PROXY aliyun + IPv4 only")
    return True


def parse_arxiv_link(link):
    """Extract arxiv id from a URL like https://arxiv.org/abs/2410.11758v2 -> 2410.11758."""
    import re
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,6})", link)
    if not m:
        m = re.search(r"([0-9]{4}\.[0-9]{4,6})", link)
    if not m:
        raise ValueError("cannot parse arxiv id from: %s" % link)
    return m.group(1)


def fetch_arxiv_by_id(arxiv_id):
    """Fetch one paper via arxiv API by id. Returns dict with title/submitted_date/pdf_url/etc.

    走 HTTPS_PROXY (clash) + retry on 429. 旧实现用 feedparser.parse(url) 直连 arxiv,
    在中国大陆经常超时/429, 失败时 entries=空被当成 not found 误报.
    """
    import feedparser, re, os, time, requests
    arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
    url = "https://export.arxiv.org/api/query?id_list=%s" % arxiv_id
    proxies = {}
    if os.environ.get("HTTPS_PROXY"):
        proxies["https"] = os.environ["HTTPS_PROXY"]
    if os.environ.get("HTTP_PROXY"):
        proxies["http"] = os.environ["HTTP_PROXY"]
    last_resp = None
    body = ""
    # 429 rate limit 比较粗暴, 必须给 arxiv API 充分恢复时间
    backoffs = [15, 30, 60, 90, 120, 180]
    for attempt, wait in enumerate(backoffs):
        try:
            r = requests.get(url, proxies=proxies or None, timeout=30)
            last_resp = r
            body = r.text
            if r.status_code == 200 and "<entry>" in body:
                break
            print(f"[fetch_arxiv_by_id] attempt {attempt+1}/{len(backoffs)} status={r.status_code} body_head={body[:80]!r}, wait {wait}s")
            time.sleep(wait)
        except Exception as exc:
            print(f"[fetch_arxiv_by_id] attempt {attempt+1}/{len(backoffs)} exception={exc}, wait {wait}s")
            time.sleep(wait)
            body = "request exception: %s" % exc
    feed = feedparser.parse(body)
    if not feed.entries:
        # API 持续不可达, 退到 HTML abstract 页面解析 (https://arxiv.org/abs/<id>)
        print(f"[fetch_arxiv_by_id] API 全失败, 尝试 HTML fallback https://arxiv.org/abs/{arxiv_id}")
        try:
            from bs4 import BeautifulSoup
            html_url = f"https://arxiv.org/abs/{arxiv_id}"
            html_resp = requests.get(html_url, proxies=proxies or None, timeout=30)
            if html_resp.status_code == 200:
                soup = BeautifulSoup(html_resp.text, "html.parser")
                title_el = soup.select_one("h1.title")
                abs_el = soup.select_one("blockquote.abstract")
                date_el = soup.select_one("div.dateline")
                title = (title_el.get_text(" ", strip=True).replace("Title:", "").strip()
                         if title_el else "")
                abstract = (abs_el.get_text(" ", strip=True).replace("Abstract:", "").strip()
                            if abs_el else "")
                date_txt = date_el.get_text(" ", strip=True) if date_el else ""
                import re as _re2
                m = _re2.search(r"(\d{1,2}\s+\w+\s+\d{4})", date_txt)
                submitted = ""
                if m:
                    from datetime import datetime as _dt
                    try:
                        submitted = _dt.strptime(m.group(1), "%d %b %Y").strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                if title:
                    print(f"[fetch_arxiv_by_id] HTML fallback OK: title={title[:80]!r}")
                    return {
                        "title": title,
                        "abstract": abstract,
                        "link": html_url,
                        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
                        "submitted_date": submitted,
                        "updated_date": submitted,
                    }
        except Exception as _ehf:
            print(f"[fetch_arxiv_by_id] HTML fallback 异常: {_ehf}")
        status = getattr(last_resp, "status_code", "n/a")
        raise RuntimeError(
            "arxiv id not found: %s (status=%s, body_head=%r)"
            % (arxiv_id, status, body[:300])
        )
    e = feed.entries[0]
    pdf_url = "https://arxiv.org/pdf/%s" % arxiv_id
    for l in getattr(e, "links", []) or []:
        if getattr(l, "type", "") == "application/pdf":
            pdf_url = l.href
            break
    return {
        "title": (e.title or "").replace("\n", " ").strip(),
        "abstract": (e.summary or "").strip(),
        "link": e.link,
        "pdf_url": pdf_url,
        "submitted_date": (e.published or "")[:10],
        "updated_date": (e.updated or "")[:10],
        "arxiv_id": arxiv_id,
    }
