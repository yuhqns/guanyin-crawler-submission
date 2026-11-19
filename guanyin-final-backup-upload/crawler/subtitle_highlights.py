"""有字幕影片路線：取字幕 → 切段給人／AI 讀 → 依「重點」在時間點截圖 → 輸出重點檔。

只用上傳者提供的字幕（不是自動字幕）。取字幕只讀字幕文字，不下載影片；
截圖需要影片檔，由使用者指定 --video（本機影片，要有權處理）。

三個步驟（在專案根目錄執行，編號用 page027 這種爬蟲編號）：

  1. 取字幕
       python -m crawler.subtitle_highlights fetch --id page027
     → output/videos/page027/page027_subtitles.json   （逐句字幕，每句有編號 c001、起訖秒數）
       output/videos/page027/page027_subtitle_chunks.md（分段好讀的版本，拿去挑重點用）

  2. 挑重點（由人或 AI 讀 chunks.md 後寫成 page027_highlights.json；程式不替你判斷重點）
       {"video_id": "...", "note": "誰挑的、依據什麼",
        "highlights": [{"cue": "c012", "title": "重點標題", "summary": "一兩句說明"}]}

  3. 截圖並輸出
       python -m crawler.subtitle_highlights render --id page027 --video 影片檔路徑
     → output/videos/page027/highlights/*.jpg 與 page027_highlights.md
       不給 --video 也能輸出重點檔，但會標明沒有截圖。

重點的截圖時間 = 該句字幕起訖的中間點，所以畫面上通常正好是那句話。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .transcribe_api import ROOT, _read_meta, _timestamp
from .video_triage import fetch_info

# 中文字幕優先順序（繁體在前）。沒有中文字幕時不亂挑，請用 --lang 指定。
LANG_PREFERENCE = ["zh-TW", "zh-Hant", "zh-Hant-TW", "zh-HK", "zh", "zh-Hans", "zh-CN", "yue-HK", "yue"]
CHUNK_SECONDS = 45      # 一段最長約幾秒
CHUNK_GAP_SECONDS = 3   # 兩句之間停頓超過幾秒就另起一段
FRAME_WIDTH = 1280
TIME_RE = re.compile(r"(?:(\d{2,}):)?(\d{2}):(\d{2})[.,](\d{3})")


def video_dir(record_id: str) -> Path:
    """某支影片的輸出資料夾。編號只允許英數字、底線、連字號，避免 --id 跑到專案外面。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", record_id or ""):
        raise SystemExit(f"編號只能用英文、數字、底線與連字號：{record_id!r}")
    return ROOT / "output" / "videos" / record_id


# ---------------------------------------------------------------- 字幕解析

def parse_vtt(vtt: str) -> list[dict[str, Any]]:
    """WebVTT → [{id, start, end, text}]。去掉標記、合併相鄰的重複句子、略過空句。"""
    cues: list[dict[str, Any]] = []
    start = end = None
    lines: list[str] = []

    def flush() -> None:
        nonlocal start, end, lines
        text = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if start is not None and end is not None and text:
            if cues and cues[-1]["text"] == text and start - cues[-1]["end"] < 0.5:
                cues[-1]["end"] = round(end, 3)
            else:
                cues.append({"start": round(start, 3), "end": round(end, 3), "text": text})
        start = end = None
        lines = []

    raw_lines = vtt.splitlines()
    for index, raw in enumerate(raw_lines):
        line = raw.strip()
        if "-->" in line:
            flush()
            times = [int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
                     for m in TIME_RE.finditer(line)]
            if len(times) >= 2:
                start, end = times[0], times[1]
            continue
        # 純數字行只有「緊接著時間行」時才是 cue 編號；字幕文字本身可能就是一個數字（例如年份）
        is_cue_number = line.isdigit() and index + 1 < len(raw_lines) and "-->" in raw_lines[index + 1]
        if not line or line == "WEBVTT" or is_cue_number or line.startswith(("NOTE", "Kind:", "Language:", "STYLE")):
            flush()
            continue
        if start is None:
            continue  # 時間行之前的雜訊
        line = html.unescape(re.sub(r"<[^>]+>", "", line))
        if line.strip():
            lines.append(line)
    flush()
    return [{"id": f"c{number:03d}", **cue} for number, cue in enumerate(cues, start=1)]


INLINE_TIME_RE = re.compile(r"<\d{2}:\d{2}:\d{2}[.,]\d{3}>")


def parse_auto_vtt(vtt: str) -> list[dict[str, Any]]:
    """YouTube 自動字幕（滾動式）→ [{id, start, end, text}]。

    自動字幕每個時間塊會重複上一句、再接新的一句，只有「新的一句」帶逐字時間標記
    （<00:00:01.235>）。所以只取帶標記的行，就不會重複。沒有任何標記時（別的來源），
    退回一般解析，並去掉和上一塊相同的行。"""
    blocks: list[tuple[float, float, list[str]]] = []
    start = end = None
    lines: list[str] = []
    for raw in vtt.splitlines() + [""]:
        line = raw.rstrip("\r\n")
        if "-->" in line:
            times = [int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000
                     for m in TIME_RE.finditer(line)]
            start, end, lines = (times[0], times[1], []) if len(times) >= 2 else (None, None, [])
        elif not line:  # 真正的空行才是分隔；YouTube 會在句子前塞只有空白的行，那不算
            if start is not None and lines:
                blocks.append((start, end, lines))
            start = end = None
            lines = []
        elif start is not None:
            lines.append(line)
    tagged = [(s, e, [x for x in ls if INLINE_TIME_RE.search(x)]) for s, e, ls in blocks]
    tagged = [b for b in tagged if b[2]]
    if not tagged:
        cues = parse_vtt(vtt)
        out, previous = [], ""
        for cue in cues:
            if cue["text"] != previous:
                out.append(cue)
            previous = cue["text"]
        return [{**c, "id": f"c{n:03d}"} for n, c in enumerate(out, start=1)]
    cues = []
    for s, e, ls in tagged:
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", " ".join(ls)))).strip()
        if text:
            cues.append({"start": round(s, 3), "end": round(e, 3), "text": text})
    # 每一句的結束時間延到下一句開始（自動字幕塊本身很短），但不超過原本結束後 10 秒
    for current, nxt in zip(cues, cues[1:]):
        current["end"] = round(max(current["end"], min(nxt["start"], current["end"] + 10)), 3)
    return [{"id": f"c{n:03d}", **c} for n, c in enumerate(cues, start=1)]


def pick_auto_language(auto_languages: list[str], original: str = "", wanted: str = "") -> str:
    """選自動字幕語言：指定的優先，其次帶 -orig 的（影片原聲辨識），其次影片原語言；否則不亂挑。"""
    if wanted:
        if wanted not in auto_languages:
            raise ValueError(f"沒有 {wanted} 自動字幕；有：{' '.join(auto_languages) or '無'}")
        return wanted
    orig = [k for k in auto_languages if k.endswith("-orig")]
    if orig:
        chinese = [k for k in orig if k.lower().startswith(("zh", "yue"))]
        return (chinese or orig)[0]
    if original and original in auto_languages:
        return original
    raise ValueError("找不到影片原聲的自動字幕（沒有 -orig 軌）；要用哪一種請加 --lang")


def pick_language(manual_languages: list[str], wanted: str = "") -> str:
    """選字幕語言。指定的優先；否則依中文優先順序；都沒有就丟錯，不亂挑。"""
    if wanted:
        if wanted not in manual_languages:
            raise ValueError(f"沒有 {wanted} 字幕；這支影片的上傳者字幕有：{' '.join(manual_languages) or '無'}")
        return wanted
    for lang in LANG_PREFERENCE:
        if lang in manual_languages:
            return lang
    if not manual_languages:
        raise ValueError("這支影片沒有上傳者提供的字幕（請走沒有字幕的路線）")
    raise ValueError(f"沒有中文字幕；上傳者字幕有：{' '.join(manual_languages)}，要用哪一種請加 --lang")


def make_chunks(cues: list[dict[str, Any]], max_seconds: float = CHUNK_SECONDS,
                gap_seconds: float = CHUNK_GAP_SECONDS) -> list[list[dict[str, Any]]]:
    """把逐句字幕切成好讀的段：停頓夠久或這段夠長就另起一段。"""
    chunks: list[list[dict[str, Any]]] = []
    for cue in cues:
        if chunks:
            current = chunks[-1]
            long_enough = cue["end"] - current[0]["start"] > max_seconds
            paused = cue["start"] - current[-1]["end"] > gap_seconds
            if not (long_enough or paused):
                current.append(cue)
                continue
        chunks.append([cue])
    return chunks


# ---------------------------------------------------------------- 步驟 1：取字幕

def _download_vtt(url: str, lang: str, auto: bool = False) -> str:
    exe = shutil.which("yt-dlp")
    command = ([exe] if exe else [sys.executable, "-m", "yt_dlp"]) + [
        "--skip-download", "--write-auto-subs" if auto else "--write-subs",
        # --sub-langs 是正規表示式；加上頭尾錨點，指定 zh 就只抓 zh，不會連 zh-TW 一起抓
        "--sub-langs", "^" + re.escape(lang) + "$", "--sub-format", "vtt",
        "--no-warnings", "--no-playlist", "-o", "%(id)s.%(ext)s", url]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            done = subprocess.run(command, capture_output=True, text=True, cwd=tmp, timeout=120)
        except FileNotFoundError as exc:
            raise RuntimeError("找不到 yt-dlp，請先安裝（brew install yt-dlp）") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("字幕下載逾時（120 秒）") from exc
        files = sorted(Path(tmp).glob("*.vtt"))
        exact = [f for f in files if f.name.endswith(f".{lang}.vtt")]
        if done.returncode != 0 or not (exact or files):
            tail = [x for x in done.stderr.strip().splitlines() if x.strip()]
            raise RuntimeError(f"字幕下載失敗：{tail[-1] if tail else '沒有產生字幕檔'}")
        return (exact or files)[0].read_text(encoding="utf-8-sig")


def render_chunks_md(title: str, video_id: str, language: str, chunks: list[list[dict[str, Any]]]) -> str:
    lines = [f"# {title}｜字幕分段", "",
             f"- YouTube Video ID：{video_id}",
             f"- 字幕：上傳者提供（{language}）",
             f"- 共 {sum(len(c) for c in chunks)} 句、{len(chunks)} 段",
             "- 每句前面的 cNNN 是句子編號；挑重點時請寫在 highlights.json 的 cue 欄位", ""]
    for number, chunk in enumerate(chunks, start=1):
        lines += [f"## 第 {number} 段　{_timestamp(chunk[0]['start'])} – {_timestamp(chunk[-1]['end'])}", ""]
        lines += [f"- `{c['id']}` **[{_timestamp(c['start'])}]** {c['text']}" for c in chunk]
        lines.append("")
    return "\n".join(lines)


def fetch_auto(args: argparse.Namespace, info: dict[str, Any], url: str) -> int:
    """抓 YouTube 自動字幕（影片原聲辨識那一軌），存成 <id>_auto_captions.json。
    和上傳者字幕分開存，不覆蓋；之後用 crawler.caption_compare 跟沒字幕路線的結果比較。"""
    auto = sorted(k for k, v in (info.get("automatic_captions") or {}).items() if k != "live_chat" and v)
    try:
        language = pick_auto_language(auto, info.get("language") or "", args.lang)
    except ValueError as exc:
        raise SystemExit(str(exc))
    try:
        cues = parse_auto_vtt(_download_vtt(url, language, auto=True))
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    if not cues:
        raise SystemExit("自動字幕檔是空的")
    folder = video_dir(args.id)
    folder.mkdir(parents=True, exist_ok=True)
    data = {"video_id": info.get("id", ""), "title": info.get("title", ""), "url": url,
            "language": language, "source": "auto", "cues": cues}
    target = folder / f"{args.id}_auto_captions.json"
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{args.id}：自動字幕 {language}，{len(cues)} 句 → {target}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    video_dir(args.id)  # 先檢查編號，再讀任何檔案
    title, url = _read_meta(args.id)
    url = args.url or url
    if not url:
        raise SystemExit(f"找不到 {args.id} 的網址，請加 --url")
    video_dir(args.id)  # 先檢查編號
    try:
        info = fetch_info(url)
    except RuntimeError as exc:
        raise SystemExit(f"讀不到影片資訊：{exc}")
    if args.auto:
        return fetch_auto(args, info, url)
    manual = sorted(k for k, v in (info.get("subtitles") or {}).items() if k != "live_chat" and v)
    try:
        language = pick_language(manual, args.lang)
    except ValueError as exc:
        has_auto = any(k != "live_chat" and v for k, v in (info.get("automatic_captions") or {}).items())
        hint = "（這支影片有自動字幕，要抓請加 --auto）" if not manual and has_auto else ""
        raise SystemExit(f"{exc}{hint}")
    try:
        cues = parse_vtt(_download_vtt(url, language))
    except RuntimeError as exc:
        raise SystemExit(str(exc))
    if not cues:
        raise SystemExit("字幕檔是空的")
    video_id = info.get("id", "")
    folder = video_dir(args.id)
    folder.mkdir(parents=True, exist_ok=True)
    video_title = info.get("title", "") or (title if title != args.id else "")
    data = {"video_id": video_id, "title": video_title, "url": url,
            "language": language, "source": "uploader", "cues": cues}
    target = folder / f"{args.id}_subtitles.json"
    if target.is_file() and (folder / f"{args.id}_highlights.json").is_file():
        try:
            same = json.loads(target.read_text(encoding="utf-8")).get("cues") == cues
        except (OSError, ValueError):
            same = False
        if not same:
            print("警告：這次抓到的字幕和上次不同；既有的 highlights.json 用的是舊編號，"
                  "句子編號可能對不上，請重新檢查重點。")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    chunks = make_chunks(cues)
    (folder / f"{args.id}_subtitle_chunks.md").write_text(
        render_chunks_md(title if title != args.id else info.get("title", args.id), video_id, language, chunks),
        encoding="utf-8")
    print(f"{args.id}：字幕 {language}，{len(cues)} 句，{len(chunks)} 段 → {folder}")
    return 0


# ---------------------------------------------------------------- 步驟 3：截圖與輸出

def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"找不到 {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(f"{path.name} 不是有效的 JSON：{exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"{path.name} 的最外層必須是物件（{{...}}）")
    return data


def frame_time(cue: dict[str, Any]) -> float:
    """截圖時間：該句字幕起訖的中間點。"""
    return round((cue["start"] + cue["end"]) / 2, 2)


def resolve_highlights(highlights: list[dict[str, Any]], cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把重點的 cue 編號對回字幕句。編號不存在、重複、缺標題都當成錯誤，不默默略過。"""
    by_id = {c["id"]: c for c in cues}
    seen: set[str] = set()
    out = []
    for number, item in enumerate(highlights, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {number} 個重點的格式不對，應該是 {{\"cue\": …, \"title\": …}}")
        cue_id = item.get("cue", "")
        if cue_id not in by_id:
            raise ValueError(f"第 {number} 個重點的 cue「{cue_id}」不在字幕裡")
        if cue_id in seen:
            raise ValueError(f"第 {number} 個重點的 cue「{cue_id}」重複了")
        if not str(item.get("title", "")).strip():
            raise ValueError(f"第 {number} 個重點沒有 title")
        seen.add(cue_id)
        out.append({**item, "cue_data": by_id[cue_id]})
    return sorted(out, key=lambda h: h["cue_data"]["start"])


def video_duration(video: Path) -> float:
    try:
        done = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                               "-of", "default=nw=1:nk=1", str(video)], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffprobe，請先安裝 FFmpeg（brew install ffmpeg）") from exc
    try:
        return float(done.stdout.strip())
    except ValueError:
        raise RuntimeError(f"讀不到影片長度：{video}")


def grab_frame(video: Path, seconds: float, out: Path) -> None:
    command = ["ffmpeg", "-y", "-v", "error", "-ss", str(seconds), "-i", str(video), "-frames:v", "1",
               "-vf", f"scale='min({FRAME_WIDTH},iw)':-2", "-q:v", "2", str(out)]
    try:
        done = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffmpeg，請先安裝 FFmpeg（brew install ffmpeg）") from exc
    if done.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        raise RuntimeError(f"截圖失敗（{seconds} 秒）：{done.stderr.strip()[-200:]}")


def neighbours(cues: list[dict[str, Any]], cue_id: str, around: int = 1) -> list[dict[str, Any]]:
    index = next(i for i, c in enumerate(cues) if c["id"] == cue_id)
    return cues[max(0, index - around): index + around + 1]


def render_highlights_md(title: str, video_id: str, url: str, language: str, note: str,
                         items: list[dict[str, Any]], cues: list[dict[str, Any]], frames: dict[str, str],
                         video_given: bool) -> str:
    lines = [f"# {title}｜重點", "",
             f"- YouTube Video ID：{video_id}",
             f"- 原始網址：{url or '未知'}",
             f"- 字幕來源：上傳者提供（{language}）",
             f"- 重點數量：{len(items)}",
             f"- 重點怎麼挑的：{note or '未註明'}",
             "- 注意：重點是依字幕內容挑選，未經人工確認。", ""]
    if not video_given:
        lines += ["> 這次沒有指定影片檔，所以沒有截圖。取得有權處理的影片後，加 `--video` 重跑即可。", ""]
    for number, item in enumerate(items, start=1):
        cue = item["cue_data"]
        lines += [f"## {number}. {item['title']}　[{_timestamp(cue['start'])}]", ""]
        if item.get("summary"):
            lines += [str(item["summary"]).strip(), ""]
        if item["cue"] in frames:
            lines += [f"![{item['title']}]({frames[item['cue']]})", ""]
        for near in neighbours(cues, item["cue"]):
            mark = "**" if near["id"] == item["cue"] else ""
            lines.append(f"- [{_timestamp(near['start'])}] {mark}{near['text']}{mark}")
        lines.append("")
    return "\n".join(lines)


def cmd_render(args: argparse.Namespace) -> int:
    folder = video_dir(args.id)
    subtitles = load_json(folder / f"{args.id}_subtitles.json")
    highlights = load_json(args.highlights or folder / f"{args.id}_highlights.json")
    cues = subtitles.get("cues")
    if not isinstance(cues, list) or not cues:
        raise SystemExit("字幕檔裡沒有字幕句子")
    if highlights.get("video_id") and subtitles.get("video_id") and highlights["video_id"] != subtitles["video_id"]:
        raise SystemExit(f"重點檔的 video_id（{highlights['video_id']}）和字幕檔（{subtitles['video_id']}）不一樣，"
                         "重點可能是別支影片的")
    highlight_list = highlights.get("highlights", [])
    if not isinstance(highlight_list, list):
        raise SystemExit("重點檔的 highlights 必須是清單")
    try:
        items = resolve_highlights(highlight_list, cues)
    except ValueError as exc:
        raise SystemExit(f"重點檔有問題：{exc}")
    if not items:
        raise SystemExit("重點檔裡沒有任何重點")
    frames: dict[str, str] = {}
    if args.video:
        if not args.video.is_file():
            raise SystemExit(f"找不到影片檔：{args.video}")
        try:
            duration = video_duration(args.video)
        except RuntimeError as exc:
            raise SystemExit(str(exc))
        for item in items:
            if frame_time(item["cue_data"]) > duration:
                raise SystemExit(f"重點 {item['cue']} 的時間 {frame_time(item['cue_data'])} 秒超過影片長度 "
                                 f"{duration:.0f} 秒，影片檔和字幕可能不是同一支")
        out_dir = folder / "highlights"
        out_dir.mkdir(parents=True, exist_ok=True)
        # 先全部截到暫存資料夾，成功了才換掉舊的；中途失敗時舊截圖原封不動
        scratch = Path(tempfile.mkdtemp(prefix=".hl_tmp_", dir=folder))
        try:
            for number, item in enumerate(items, start=1):
                seconds = frame_time(item["cue_data"])
                name = f"hl_{number:02d}_{_timestamp(seconds).replace(':', '-')}.jpg"
                grab_frame(args.video, seconds, scratch / name)
                frames[item["cue"]] = f"highlights/{name}"
            for old in out_dir.glob("hl_*.jpg"):
                old.unlink()  # 只清掉這支影片自己上次產生的重點截圖
            for new in scratch.glob("hl_*.jpg"):
                shutil.move(str(new), str(out_dir / new.name))
        except RuntimeError as exc:
            raise SystemExit(str(exc))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    title, url = _read_meta(args.id)
    title = subtitles.get("title") or title
    url = subtitles.get("url") or url
    md = render_highlights_md(title, subtitles.get("video_id", ""), url, subtitles.get("language", ""),
                              highlights.get("note", ""), items, cues, frames, bool(args.video))
    target = folder / f"{args.id}_highlights.md"
    target.write_text(md, encoding="utf-8")
    print(f"{args.id}：{len(items)} 個重點，截圖 {len(frames)} 張 → {target}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="有字幕影片：取字幕、切段、依重點截圖並輸出。")
    sub = ap.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="取得上傳者字幕並切段")
    f.add_argument("--id", required=True, help="爬蟲編號，例如 page027")
    f.add_argument("--url", default="", help="影片網址；省略時從 output/videos/<id>/video.md 讀")
    f.add_argument("--lang", default="", help="指定字幕語言（例如 zh-TW）；省略時中文優先")
    f.add_argument("--auto", action="store_true", help="改抓 YouTube 自動字幕（只有自動字幕的影片用）")
    f.set_defaults(func=cmd_fetch)
    r = sub.add_parser("render", help="依重點檔截圖並輸出重點 Markdown")
    r.add_argument("--id", required=True)
    r.add_argument("--video", type=Path, help="本機影片檔（截圖用）；省略就不截圖")
    r.add_argument("--highlights", type=Path, help="重點檔；省略時用 output/videos/<id>/<id>_highlights.json")
    r.set_defaults(func=cmd_render)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
