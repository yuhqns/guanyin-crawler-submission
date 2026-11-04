#!/usr/bin/env python3
"""Polite, repeatable crawler for the supplied Guanyin District source list."""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = None
    BeautifulSoup = None
from html.parser import HTMLParser
from urllib.request import Request, urlopen
from urllib.error import HTTPError

try:
    import trafilatura
except ImportError:
    trafilatura = None

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if HERE.name == "crawler" else HERE
USER_AGENT = "GuanyinKnowledgeCrawler/1.0 (respectful research; contact: local user)"
TERMS = ("觀音", "甘泉寺", "白沙岬", "草漯", "保障", "濱海", "蓮花", "崙坪", "新屋", "大潭", "坑溪", "藻礁", "甘泉", "保生")
SKIP_RE = re.compile(
    r"^(?:廣告|追蹤|ads?|advert|advertisement|banner|logo|icon|avatar|pixel|tracking)"
    r"(?:[-_](?:container|wrapper|image|img|button|link|pixel|banner|placeholder))?$",
    re.I,
)
NOISE = ("nav", "header", "footer", "aside", "script", "style", "noscript", "button", "svg")
# Many sites put navigation and consent UI in generic div/section tags.  The
# class/id check complements the semantic tags above without removing article
# content such as infoboxes or ordinary headings.
NOISE_TOKEN_RE = re.compile(
    r"^(?:(?:site|global|primary|main|top|mobile|desktop|base)[-_])?"
    r"(?:nav|navbar|navigation|menu|breadcrumb|breadcrumbs|cookie|consent|gdpr|subscribe|newsletter|"
    r"social|share|sharing|related|recommend|recommendations|comment|comments|reply|pagination|pager|"
    r"popup|modal|dialog|toolbar|sidebar|sticky|floating|advert|advertisement|promo|promotion|footer|header)"
    r"(?:[-_](?:bar|nav|navigation|menu|banner|notice|prompt|dialog|popup|section|articles?|posts?|"
    r"links?|buttons?|widget|container|wrapper|box|area|panel|list|items?|block))?$",
    re.I,
)
NOISE_TEXT_RE = re.compile(
    r"^(?:::|首頁|主選單|選單|跳到主要內容區塊|跳至內容|移至側邊欄|隱藏|"
    r"搜尋|進階搜尋|登入|註冊|Facebook|Plurk|Twitter|Line|Email|"
    r"Cookie|同意|訂閱(?:通知|RSS)?|分享|關於我們|聯絡我們|隱私權政策|"
    r"最新消息|重要資訊|線上解籤|線上點燈|VR尋燈導覽|回到頂端)$",
    re.I,
)


def is_noise_attrs(attrs: str) -> bool:
    tokens = re.findall(r"[0-9A-Za-z\u4e00-\u9fff]+(?:[-_][0-9A-Za-z\u4e00-\u9fff]+)*", attrs)
    return any(SKIP_RE.fullmatch(token) or NOISE_TOKEN_RE.fullmatch(token) for token in tokens)


def is_noise_element(tag: str, attrs: str) -> bool:
    # A number of government sites wrap the whole page in a form element.
    # Remove only forms identified as UI chrome; keep ordinary content forms.
    return tag in NOISE or is_noise_attrs(attrs) or (tag == "form" and is_noise_attrs(attrs))


def clean_url(url: str) -> str:
    return urldefrag(url.strip())[0]


def normalize_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[\t\xa0 ]+", " ", text)).strip()


def clean_text_lines(text: str) -> str:
    lines=[]
    previous=None
    for raw in text.splitlines():
        line=raw.strip()
        if not line or NOISE_TEXT_RE.fullmatch(line):
            continue
        if line == previous:
            continue
        lines.append(line)
        previous=line
    return normalize_text("\n".join(lines))


def choose_image_url(attrs: dict) -> str:
    """Select the largest usable candidate from normal and lazy image attrs."""
    for key in ("src", "data-src", "data-original", "data-lazy-src", "data-image", "data-url"):
        if attrs.get(key):
            return attrs[key]
    srcset = attrs.get("srcset") or attrs.get("data-srcset") or ""
    candidates = []
    for part in srcset.split(","):
        bits = part.strip().split()
        if not bits:
            continue
        score = 0
        if len(bits) > 1:
            match = re.match(r"(\d+)(w|x)?", bits[1])
            if match:
                score = int(match.group(1)) * (2 if match.group(2) == "x" else 1)
        candidates.append((score, bits[0]))
    return max(candidates, default=(0, ""))[1]


class SimpleHTML(HTMLParser):
    """Dependency-free fallback parser for basic article text and page metadata."""
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.title = ""
        self.site = ""
        self.meta = {}
        self.links, self.images = [], []
        self.stack = []
        self.in_title = False
        self.all_text, self.main_text = [], []
        self.has_main = False
        self.anchor_text = []
        self.anchor_url = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key and a.get("content"):
                self.meta[key.lower()] = a["content"]
        if tag == "title":
            self.in_title = True
        classes = f"{a.get('class', '')} {a.get('id', '')} {a.get('role', '')}"
        hide = is_noise_element(tag, classes) or (self.stack[-1][0] if self.stack else False)
        in_main = tag in ("main", "article") or (self.stack[-1][1] if self.stack else False)
        if tag in ("main", "article"):
            self.has_main = True
        if tag not in ("meta", "img", "br", "hr", "input", "link", "source", "area", "wbr", "embed", "param"):
            self.stack.append((hide, in_main, tag))
        if tag == "a":
            self.anchor_url = a.get("href", "")
            self.anchor_text = []
        if tag == "img":
            src = choose_image_url(a)
            src = urljoin(self.url, src)
            if src and not SKIP_RE.search(src) and urlparse(src).scheme in ("http", "https"):
                self.images.append({"url": src, "alt": a.get("alt", ""), "caption": ""})

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "a" and self.anchor_url:
            href = clean_url(urljoin(self.url, self.anchor_url))
            if urlparse(href).scheme in ("http", "https"):
                self.links.append({"text": normalize_text(" ".join(self.anchor_text)), "url": href})
            self.anchor_url, self.anchor_text = "", []
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][2] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.anchor_url:
            self.anchor_text.append(data)
        hidden = self.stack[-1][0] if self.stack else False
        main = self.stack[-1][1] if self.stack else False
        text = normalize_text(data)
        if text and not hidden:
            self.all_text.append(text)
            if main:
                self.main_text.append(text)


def parse_html_simple(url: str, html: str) -> dict:
    parser = SimpleHTML(url)
    parser.feed(html)
    meta = parser.meta
    title = meta.get("og:title") or meta.get("twitter:title") or normalize_text(parser.title)
    site = meta.get("og:site_name") or (urlparse(url).hostname or "")
    body = clean_text_lines("\n".join(parser.main_text if parser.has_main and parser.main_text else parser.all_text))
    links = list({x["url"]: x for x in reversed(parser.links)}.values())[::-1]
    images = list({x["url"]: x for x in reversed(parser.images)}.values())[::-1]
    return {"title": title, "site_name": site, "author": meta.get("author") or meta.get("article:author", ""),
            "published_at": meta.get("article:published_time") or meta.get("date") or meta.get("datepublished", ""),
            "content": body, "links": links, "images": images}


def http_get(url: str, timeout: int, params=None):
    if params:
        from urllib.parse import urlencode
        url += ("&" if "?" in url else "?") + urlencode(params)
    if requests:
        return requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7"})
    try:
        with urlopen(req, timeout=timeout) as response:
            class Response:
                status_code = response.status
                url = response.geturl()
                headers = response.headers
                content = response.read()
                encoding = response.headers.get_content_charset() or "utf-8"
                apparent_encoding = encoding
                @property
                def text(self):
                    return self.content.decode(self.encoding or "utf-8", errors="replace")
                def raise_for_status(self):
                    if self.status_code >= 400:
                        raise RuntimeError(f"HTTP {self.status_code}")
                def json(self):
                    return json.loads(self.text)
            return Response()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc


def parse_html(url: str, html: str) -> dict:
    if BeautifulSoup is None:
        return parse_html_simple(url, html)
    try:
        return parse_html_bs4(url, html)
    except Exception as exc:
        result = parse_html_simple(url, html)
        result["extraction_note"] = f"進階解析失敗，已使用內建解析器：{type(exc).__name__}: {exc}"
        return result


def parse_html_bs4(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    author = ""
    published_at = ""
    for key in ("author", "article:author", "byl", "byline"):
        tag = soup.find("meta", attrs={"name": key}) or soup.find("meta", attrs={"property": key})
        if tag and tag.get("content"):
            author = normalize_text(tag["content"])
            break
    for key in ("article:published_time", "datepublished", "date", "pubdate"):
        tag = soup.find("meta", attrs={"name": key}) or soup.find("meta", attrs={"property": key})
        if tag and tag.get("content"):
            published_at = normalize_text(tag["content"])
            break
    if not published_at:
        time_tag = soup.find("time", datetime=True)
        if time_tag:
            published_at = normalize_text(time_tag.get("datetime", ""))
    # Keep page links for the link inventory even when the link is inside
    # navigation or recommendation chrome that should not enter article text.
    links = []
    for a in soup.find_all("a", href=True):
        href = clean_url(urljoin(url, a["href"]))
        if urlparse(href).scheme in ("http", "https"):
            links.append({"text": normalize_text(a.get_text(" ", strip=True)), "url": href})
    for el in soup.find_all(list(NOISE)):
        el.decompose()
    # Preserve a main/article container before attribute-based pruning. Some
    # CMS pages put the article inside a generic form or wrapper.
    main_candidate = soup.find("main") or soup.find("article") or soup.find(id=re.compile(r"^(content|article|main)$", re.I))
    for el in list(soup.find_all(True)):
        attrs = " ".join([str(el.get("class", "")), str(el.get("id", "")), str(el.get("role", ""))])
        if is_noise_element(el.name, attrs):
            el.decompose()

    def meta(*names: str) -> str:
        for name in names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                return normalize_text(tag["content"])
        return ""

    title = meta("og:title", "twitter:title") or (soup.title.get_text(" ", strip=True) if soup.title else "")
    site = meta("og:site_name") or (urlparse(url).hostname or "")
    main = main_candidate or soup.find("main") or soup.find("article") or soup.find(id=re.compile(r"content|article|main", re.I)) or soup.body or soup
    if trafilatura:
        extracted = trafilatura.extract(str(soup), url=url, include_comments=False, include_tables=True,
                                       favor_recall=True, deduplicate=True)
        body = clean_text_lines(extracted or main.get_text("\n", strip=True))
    else:
        body = normalize_text(main.get_text("\n", strip=True))

    images = []
    for img in soup.find_all("img"):
        src = choose_image_url(img.attrs)
        src = urljoin(url, src)
        if src and not SKIP_RE.search(src) and urlparse(src).scheme in ("http", "https"):
            images.append({"url": src, "alt": normalize_text(img.get("alt", "")),
                           "caption": normalize_text(img.find_parent("figure").get_text(" ", strip=True)) if img.find_parent("figure") else ""})
    # Deduplicate while preserving source order.
    links = list({item["url"]: item for item in reversed(links)}.values())[::-1]
    images = list({item["url"]: item for item in reversed(images)}.values())[::-1]
    return {"title": normalize_text(title), "site_name": site, "author": author,
            "published_at": published_at, "content": body,
            "links": links, "images": images}


def _find_local_media(media_dir: Path | None, video_id: str) -> Path | None:
    """Find a user-provided media file whose name starts with the YouTube ID."""
    if not media_dir or not media_dir.is_dir() or not video_id:
        return None
    ignored = {".wav", ".json", ".txt", ".vtt"}
    for path in sorted(media_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() not in ignored and path.stem.startswith(video_id):
            return path
    return None


def _download_audio(url: str, video_id: str, target_dir: Path, timeout: int) -> Path:
    """Download and cache audio-only media for a video the user may process."""
    if not video_id or not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        raise RuntimeError(f"無效的 YouTube video ID：{video_id!r}")
    target_dir.mkdir(parents=True, exist_ok=True)

    # Reuse a previous successful audio download instead of downloading it again.
    ignored = {".part", ".ytdl", ".json", ".description", ".jpg", ".webp"}
    cached = [
        path for path in sorted(target_dir.glob(f"{video_id}.*"))
        if path.is_file() and path.suffix.lower() not in ignored and path.stat().st_size > 0
    ]
    if cached:
        return cached[0]

    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError("缺少 yt-dlp；請執行 py -m pip install -r requirements.txt") from exc

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(target_dir / "%(id)s.%(ext)s"),
        "socket_timeout": timeout,
        "retries": 2,
        "fragment_retries": 2,
        "overwrites": False,
        "continuedl": True,
        "max_filesize": 2 * 1024 * 1024 * 1024,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        raise RuntimeError(f"yt-dlp 音訊下載失敗：{type(exc).__name__}: {exc}") from exc

    candidates = [
        path for path in sorted(target_dir.glob(f"{video_id}.*"))
        if path.is_file() and path.suffix.lower() not in ignored and path.stat().st_size > 0
    ]
    if not candidates:
        raise RuntimeError(f"yt-dlp 未產生可用音訊檔：{video_id}")
    return candidates[0]


def _transcription_fallback(
    url: str,
    info: dict,
    media_dir: Path | None,
    work_dir: Path,
    model_name: str,
    download_audio: bool,
    timeout: int,
) -> dict:
    """Return Whisper fields, without turning an optional fallback into a crawl failure."""
    downloaded = False
    try:
        try:
            from crawler.transcribe import transcribe_local_media
        except ImportError:
            import importlib.util
            transcribe_path = Path(__file__).resolve().parent / "transcribe.py"
            spec = importlib.util.spec_from_file_location("_guanyin_transcribe", transcribe_path)
            if not spec or not spec.loader:
                raise RuntimeError(f"找不到轉錄模組：{transcribe_path}")
            transcribe_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(transcribe_module)
            transcribe_local_media = transcribe_module.transcribe_local_media

        video_id = str(info.get("id") or "")
        media_path = _find_local_media(media_dir, video_id)
        source = "local_media"
        if media_path is None and download_audio:
            media_path = _download_audio(url, video_id, work_dir / "downloads", timeout)
            source = "downloaded_audio"
            downloaded = True
        if media_path is None:
            return {
                "transcription_available": False,
                "transcription_note": "沒有找到本機影片，且未取得可供 Whisper 辨識的音訊",
                "transcription_source": "",
                "downloaded_audio": False,
            }
        segments, language = transcribe_local_media(media_path, work_dir / "audio", model_name)
        text = "\n".join(segment["text"] for segment in segments)
        if not text:
            return {
                "transcription_available": False,
                "transcription_note": f"{source} 未產生可用逐字稿",
                "downloaded_audio": downloaded,
            }
        return {
            "transcription": text,
            "transcription_segments": segments,
            "transcription_available": True,
            "transcription_language": language,
            "transcription_source": source,
            "downloaded_audio": downloaded,
            "transcription_note": "",
        }
    except Exception as exc:
        return {
            "transcription_available": False,
            "transcription_note": f"語音辨識失敗：{type(exc).__name__}: {exc}",
            "downloaded_audio": downloaded,
        }


def youtube_record(
    url: str,
    timeout: int,
    media_dir: Path | None = None,
    work_dir: Path | None = None,
    model_name: str = "small",
    download_audio: bool = True,
) -> dict:
    # yt-dlp provides metadata and caption tracks without downloading the video.
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": timeout,
                "extract_flat": False, "noplaylist": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        subs = info.get("subtitles", {}) or {}
        auto = info.get("automatic_captions", {}) or {}
        tracks = subs or auto
        transcript = ""
        transcript_segments = []
        if tracks:
            langs = sorted(tracks, key=lambda x: (not x.startswith("zh"), not x.startswith("en")))
            for lang in langs:
                choices = [x for x in tracks[lang] if x.get("ext") == "vtt"] or tracks[lang]
                if choices:
                    track = choices[0]
                    if track.get("url"):
                        resp = http_get(track["url"], timeout=timeout)
                        resp.raise_for_status()
                        transcript_segments = parse_vtt_segments(resp.text)
                        transcript = "\n".join(item["text"] for item in transcript_segments)
                        if transcript:
                            break
        result = {"title": info.get("title", ""), "site_name": "YouTube", "content": transcript,
                  "links": [], "images": ([{"url": info["thumbnail"], "alt": "影片縮圖", "caption": ""}] if info.get("thumbnail") else []),
                  "video_id": info.get("id"), "channel": info.get("channel") or info.get("uploader"),
                  "duration_seconds": info.get("duration"), "caption_available": bool(transcript),
                  "transcript_segments": transcript_segments if transcript else [],
                  "caption_note": "" if transcript else "頁面未提供可讀字幕",
                  "transcription_available": False, "transcription_segments": [], "transcription_note": "",
                  "downloaded_audio": False}
        # Always run Whisper independently of platform caption availability.
        # Keep platform captions in content/transcript_segments; Whisper output
        # is stored only in transcription/transcription_segments.
        fallback = _transcription_fallback(
            url, info, media_dir, work_dir or Path.cwd() / "audio_work", model_name, download_audio, timeout
        )
        result.update(fallback)
        if result.get("transcription_available"):
            source_label = "下載音訊" if result.get("transcription_source") == "downloaded_audio" else "本機影片"
            result["transcription_note"] = ""
            result["caption_note"] = result.get("caption_note") or "頁面未提供可讀字幕"
            result["transcription_source_label"] = source_label
        elif result.get("transcription_note"):
            result["transcription_note"] = result["transcription_note"]
        return result
    except ImportError:
        return youtube_oembed(url, timeout)
    except Exception as exc:
        raise RuntimeError(f"YouTube 擷取失敗：{type(exc).__name__}: {exc}") from exc

def youtube_oembed(url: str, timeout: int) -> dict:
    endpoint = "https://www.youtube.com/oembed"
    resp = http_get(endpoint, timeout=timeout, params={"url": url, "format": "json"})
    resp.raise_for_status()
    data = resp.json()
    return {"title": data.get("title", ""), "site_name": "YouTube", "content": "",
            "links": [], "images": ([{"url": data["thumbnail_url"], "alt": "影片縮圖", "caption": ""}] if data.get("thumbnail_url") else []),
            "channel": data.get("author_name"), "caption_available": False, "transcript_segments": [],
            "transcription_available": False, "transcription_segments": [], "transcription_note": "",
            "downloaded_audio": False,
            "caption_note": "未安裝 yt-dlp，僅取得影片基本資訊；無法讀取字幕"}


def parse_vtt(vtt: str) -> str:
    return "\n".join(item["text"] for item in parse_vtt_segments(vtt))


def parse_vtt_segments(vtt: str) -> list[dict]:
    segments, seen = [], set()
    cue_start = None
    cue_lines = []
    timestamp_re = re.compile(r"(?:(\d{2,}):)?(\d{2}):(\d{2})[.,](\d{3})")
    def flush():
        nonlocal cue_start, cue_lines
        text = normalize_text(" ".join(cue_lines))
        if cue_start is not None and text and (cue_start, text) not in seen:
            segments.append({"start_seconds": cue_start, "timestamp": format_timestamp(cue_start), "text": text})
            seen.add((cue_start, text))
        cue_start, cue_lines = None, []
    for raw in vtt.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or line.isdigit() or line.startswith(("NOTE", "Kind:", "Language:")):
            flush()
            continue
        if "-->" in line:
            flush()
            match = timestamp_re.search(line)
            if match:
                hours = int(match.group(1) or 0)
                minutes, seconds, millis = int(match.group(2)), int(match.group(3)), int(match.group(4))
                cue_start = hours * 3600 + minutes * 60 + seconds + millis / 1000
            continue
        line = re.sub(r"<[^>]+>", "", line).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        if line:
            cue_lines.append(line)
    flush()
    return segments


def format_timestamp(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"


def relevant(url: str, text: str) -> bool:
    return any(term in (url + " " + text) for term in TERMS)


def failure_status(exc: Exception, http_status: int | str | None = "") -> str:
    """Classify access blocks separately from ordinary crawl failures."""
    try:
        code = int(http_status) if http_status else 0
    except (TypeError, ValueError):
        code = 0
    detail = f"{type(exc).__name__}: {exc}"
    # HTTP errors and transport/TLS failures mean the URL could not be read
    # in this run, so expose that state explicitly in the status report.
    if (code >= 400 and code != 404) or re.search(
        r"HTTP\s+(401|403|408|429|5\d\d)|CERTIFICATE_VERIFY_FAILED|SSL:|timed? ?out|timeout|"
        r"getaddrinfo failed|Name or service not known|Connection(?:Error|Reset|Refused)|拒絕",
        detail,
        re.I,
    ):
        return "inaccessible"
    return "failed"


def failure_reason(exc: Exception, status: str, http_status: int | str | None = "") -> str:
    """Give a human-readable reason while retaining the original exception."""
    detail = f"{type(exc).__name__}: {exc}"
    if status == "inaccessible":
        return f"無法存取：{detail}"
    if http_status == 404 or re.search(r"HTTP\s+404", detail, re.I):
        return f"找不到資源：{detail}"
    return detail


def write_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="爬取觀音區來源網頁與有限延伸連結")
    ap.add_argument("--input", type=Path, default=ROOT / "urls.csv")
    ap.add_argument("--output", type=Path, default=ROOT / "data")
    ap.add_argument("--max-depth", type=int, default=1, choices=range(0, 4))
    ap.add_argument("--max-extra", type=int, default=40, help="額外延伸頁面總數上限")
    ap.add_argument("--per-domain", type=int, default=5, help="每個網域的延伸頁面上限")
    ap.add_argument("--delay", type=float, default=1.5, help="同一網域請求間隔秒數")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0, help="除錯用來源筆數限制；0 表示全部")
    ap.add_argument("--media-dir", type=Path, help="含有已合法取得影音檔的資料夾；檔名需以 YouTube video ID 開頭")
    audio_group = ap.add_mutually_exclusive_group()
    audio_group.add_argument("--download-audio", dest="download_audio", action="store_true",
                             help="啟用影片音訊下載供 Whisper 辨識（目前為預設行為）")
    audio_group.add_argument("--no-download-audio", dest="download_audio", action="store_false",
                             help="停用音訊下載；仍可使用 --media-dir 中的本機影片")
    ap.set_defaults(download_audio=True)
    ap.add_argument("--model", default="small", help="faster-whisper 模型名稱，預設 small")
    ap.add_argument("--frame-count", type=int, choices=(3, 4, 5), default=5,
                    help="後處理時每部本機影片擷取的代表畫面數量")
    ap.add_argument("--no-postprocess", action="store_true",
                    help="只執行爬蟲，不自動啟動後處理")
    ap.add_argument("--no-extend", action="store_true")
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    records_path = args.output / "records.jsonl"
    status_path = args.output / "crawl_status.csv"
    for p in (records_path, status_path):
        if p.exists():
            p.unlink()
    with args.input.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "url" not in [x.strip().lower() for x in reader.fieldnames]:
            raise SystemExit("輸入 CSV 必須有 url 欄位")
        col = next(x for x in reader.fieldnames if x.strip().lower() == "url")
        seeds = [clean_url(row[col]) for row in reader if row.get(col, "").strip()]
    seeds = list(dict.fromkeys(seeds))
    if args.limit:
        seeds = seeds[:args.limit]
    if not seeds:
        raise SystemExit("CSV 中沒有可爬取網址")

    session = requests.Session() if requests else None
    if session:
        session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7"})
    seen, queued, domain_count, last_request = set(), set(), {}, {}
    queue = deque()
    # 原始 CSV 網址使用 page001、page002……作為第 0 層 ID。
    for i, url in enumerate(seeds, start=1):
        page_id = f"page{i:03d}"
        queue.append((url, 0, "seed", "", page_id, ""))
        queued.add(url)

    # 記錄每個父頁面已分配的子頁面序號。
    child_counts = {}
    extra_added = 0
    rows = []
    while queue:
        url, depth, source_type, discovered_from, page_id, parent_id = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        domain = (urlparse(url).hostname or "").lower()
        started = datetime.now(timezone.utc).isoformat()
        http_status = ""
        try:
            if not domain or urlparse(url).scheme not in ("http", "https"):
                raise ValueError("無效的 HTTP/HTTPS 網址")
            wait = args.delay - (time.monotonic() - last_request.get(domain, 0))
            if wait > 0:
                time.sleep(wait)
            last_request[domain] = time.monotonic()
            if "youtube.com" in domain or domain == "youtu.be":
                extracted = youtube_record(
                    url,
                    args.timeout,
                    media_dir=args.media_dir,
                    work_dir=args.output / "audio_work",
                    model_name=args.model,
                    download_audio=args.download_audio,
                )
                final_url, status, content_type = url, 200, "video/youtube"
            else:
                response = session.get(url, timeout=args.timeout) if session else http_get(url, args.timeout)
                http_status = response.status_code
                response.raise_for_status()
                final_url, status = response.url, response.status_code
                content_type = response.headers.get("content-type", "")
                if "html" not in content_type.lower():
                    raise ValueError(f"非 HTML 內容：{content_type}")
                response.encoding = response.apparent_encoding or response.encoding
                extracted = parse_html(final_url, response.text)
            row = {"id": page_id, "parent_id": parent_id,
                   "source_url": url, "final_url": final_url, "source_type": source_type,
                   "discovered_from": discovered_from,
                   "depth": depth, "fetched_at": started, "http_status": status,
                   "content_type": content_type, "title": extracted.pop("title", ""),
                   "site_name": extracted.pop("site_name", domain), **extracted}
            row["status"] = "success" if row.get("title") or row.get("content") else "partial"
            write_jsonl(records_path, row)
            rows.append({"id": page_id, "parent_id": parent_id,
                         "url": url, "final_url": final_url, "source_type": source_type,
                         "depth": depth, "status": row["status"], "http_status": status, "crawled_at": started,
                         "title": row["title"], "reason": row.get("caption_note", "")})
            print(f"[{row['status']}] depth={depth} {url}")
            if args.no_extend or depth >= args.max_depth or extra_added >= args.max_extra:
                continue
            for link in row.get("links", []):
                candidate = clean_url(link["url"])
                c_domain = (urlparse(candidate).hostname or "").lower()
                path = urlparse(candidate).path.lower()
                if candidate in seen or candidate in queued or c_domain in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "facebook.com", "www.facebook.com", "instagram.com", "www.instagram.com", "twitter.com", "x.com", "line.naver.jp"):
                    continue
                if "action=edit" in candidate.lower() or "redlink=1" in candidate.lower() or "/special:" in candidate.lower():
                    continue
                if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".pdf", ".mp4", ".mp3", ".zip")):
                    continue
                if not relevant(candidate, link.get("text", "")):
                    continue
                if domain_count.get(c_domain, 0) >= args.per_domain:
                    continue
                domain_count[c_domain] = domain_count.get(c_domain, 0) + 1
                extra_added += 1
                child_counts[page_id] = child_counts.get(page_id, 0) + 1
                child_id = f"{page_id}_{child_counts[page_id]}"
                queue.append((candidate, depth + 1, "extension", url, child_id, page_id))
                queued.add(candidate)
                if extra_added >= args.max_extra:
                    break
        except Exception as exc:
            # Some HTTP clients raise before the response is assigned to the
            # normal success path. Preserve its status code when available.
            exc_response = getattr(exc, "response", None)
            if getattr(exc_response, "status_code", None):
                http_status = exc_response.status_code
            if not http_status:
                match = re.search(r"HTTP\s+(\d{3})", str(exc), re.I)
                if match:
                    http_status = match.group(1)
            status = failure_status(exc, http_status)
            reason = failure_reason(exc, status, http_status)
            rows.append({"id": page_id, "parent_id": parent_id,
                         "url": url, "final_url": "", "source_type": source_type,
                         "depth": depth, "status": status, "http_status": http_status, "crawled_at": started,
                         "title": "", "reason": reason})
            print(f"[{status}] depth={depth} {url} — {reason}", file=sys.stderr)
    fields = ["id", "parent_id", "url", "final_url", "source_type", "depth", "status", "http_status", "title", "reason", "crawled_at"]
    with status_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"完成爬取：來源 {len(seeds)} 筆，處理 {len(rows)} 頁，延伸 {extra_added} 筆；輸出 {args.output}")

    # 預設在爬取完成後接續執行後處理，讓整個專案只需一個入口命令。
    # --no-postprocess 可保留單獨執行爬蟲的彈性。
    if not args.no_postprocess:
        postprocess_script = ROOT / "postprocess.py"
        if not postprocess_script.is_file():
            print(f"找不到後處理程式：{postprocess_script}", file=sys.stderr)
            return 1

        command = [
            sys.executable,
            str(postprocess_script),
            "--frame-count", str(args.frame_count),
        ]
        if args.media_dir:
            command.extend(["--media-dir", str(args.media_dir.resolve())])

        print("\\n開始執行後處理：", " ".join(command))
        try:
            result = subprocess.run(command, cwd=ROOT, check=False)
        except OSError as exc:
            print(f"無法啟動 postprocess.py：{exc}", file=sys.stderr)
            return 1
        if result.returncode != 0:
            print(f"後處理失敗，結束碼：{result.returncode}", file=sys.stderr)
            return result.returncode
        print("完整流程完成：爬取與後處理均已完成。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
