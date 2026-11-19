"""影片分流器：讀 urls.csv，判斷每個網址是什麼類型，影片再依字幕狀況分流。

只讀資訊，不下載影片、不下載音訊（用 yt-dlp 的 --dump-single-json --skip-download）。
結果寫成「分析檔」csv，每個網址一列，後面的處理程式依「處理路線」欄決定交給誰：

    類型   子類型                    處理路線
    影片   有字幕（上傳者提供）       video_with_subtitles   取字幕 → 分析重點 → 重點截圖（尚未實作）
    影片   只有自動字幕               auto_caption_compare   抓自動字幕 + 照沒字幕的做法處理，兩邊比較（使用者 2026-11-19 決定）
    影片   沒有字幕                   funasr_ocr_review      FunASR 兩模型 + 畫面字幕 OCR + 人工確認
    影片   無法判斷                   重試或人工確認         連不上或被擋，不猜
    網頁   待網頁分類                 web                    交給網頁分類與爬蟲（同事負責）

用法（在專案根目錄）：
    python -m crawler.video_triage                       # 讀 urls.csv，寫 output/video_triage.csv
    python -m crawler.video_triage --only-videos         # 只列影片
    python -m crawler.video_triage --input other.csv --out out.csv --limit 5

編號規則和 crawler.py 相同（網址去除追蹤參數後去重，再依序 page001、page002……），
所以這份分析檔的編號可以直接對應 data/records.jsonl 與 output/ 底下的檔案。
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent

TYPE_VIDEO = "影片"
TYPE_WEB = "網頁"

SUB_UPLOADER = "有字幕（上傳者提供）"
SUB_AUTO = "只有自動字幕"
SUB_NONE = "沒有字幕"
SUB_UNKNOWN = "無法判斷"
SUB_WEB = "待網頁分類"

ROUTE_SUBTITLED = "video_with_subtitles"
ROUTE_NO_SUBTITLES = "funasr_ocr_review"
ROUTE_AUTO_COMPARE = "auto_caption_compare"
ROUTE_RETRY = "重試或人工確認"
ROUTE_WEB = "web"

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
FETCH_TIMEOUT = 90
FIELDS = ["id", "url", "type", "subtype", "route", "reason", "video_id", "title", "channel",
          "duration_seconds", "manual_languages", "auto_languages"]


def is_youtube(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS


def _caption_languages(table: Any) -> list[str]:
    """字幕語言清單；直播聊天室（live_chat）不是字幕，要排除。"""
    if not isinstance(table, dict):
        return []
    return sorted(key for key, tracks in table.items() if key != "live_chat" and tracks)


def classify_info(info: dict[str, Any]) -> dict[str, Any]:
    """依 yt-dlp 回傳的影片資訊判斷字幕狀況。純函式，不連網。

    回傳的不是「單支影片」的完整資料（頻道、播放清單、空資料、缺字幕欄位）時，
    沒辦法知道有沒有字幕，一律標「無法判斷」，不能當成「沒有字幕」。"""
    problem = ""
    if info.get("_type") not in (None, "video"):
        problem = f"回傳的是 {info.get('_type')}（不是單支影片，可能是頻道或播放清單網址）"
    elif not info.get("id"):
        problem = "回傳的資料沒有影片 ID"
    elif "subtitles" not in info and "automatic_captions" not in info:
        problem = "回傳的資料沒有字幕欄位"
    if problem:
        return {"video_id": info.get("id", ""), "title": info.get("title", ""),
                "channel": info.get("channel") or info.get("uploader") or "",
                "duration_seconds": "", "manual_languages": "", "auto_languages": "",
                "subtype": SUB_UNKNOWN, "route": ROUTE_RETRY, "reason": f"讀不到完整的影片資訊：{problem}"}
    manual = _caption_languages(info.get("subtitles"))
    auto = _caption_languages(info.get("automatic_captions"))
    # 自動字幕清單裡一大半是「機器翻譯成各國語言」；真正的原聲辨識語言通常帶 -orig。
    auto_original = [k for k in auto if k.endswith("-orig")]
    base = {
        "video_id": info.get("id", ""),
        "title": info.get("title", ""),
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration_seconds": info.get("duration") if info.get("duration") is not None else "",
        "manual_languages": " ".join(manual),
        "auto_languages": " ".join(auto_original) if auto_original else (str(len(auto)) + " 種" if auto else ""),
    }
    if manual:
        has_chinese = any(k.lower().startswith("zh") for k in manual)
        reason = "上傳者提供字幕：" + " ".join(manual)
        if not has_chinese:
            reason += "（沒有中文字幕，需要另外處理）"
        return {**base, "subtype": SUB_UPLOADER, "route": ROUTE_SUBTITLED, "reason": reason}
    if auto:
        return {**base, "subtype": SUB_AUTO, "route": ROUTE_AUTO_COMPARE,
                "reason": "沒有上傳者字幕，只有 YouTube 自動產生的字幕（可能有錯字）；"
                          "抓下自動字幕，並照沒字幕的做法（FunASR + 畫面字幕 OCR）一起處理，最後兩邊比較"}
    return {**base, "subtype": SUB_NONE, "route": ROUTE_NO_SUBTITLES,
            "reason": "沒有任何字幕（上傳者字幕與自動字幕都沒有）"}


def _ytdlp_command() -> list[str]:
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    return [sys.executable, "-m", "yt_dlp"]


def fetch_info(url: str, timeout: int = FETCH_TIMEOUT) -> dict[str, Any]:
    """用 yt-dlp 只讀影片資訊。失敗就丟 RuntimeError，帶最後一行錯誤訊息。"""
    command = _ytdlp_command() + ["--dump-single-json", "--skip-download", "--no-warnings",
                                  "--no-playlist", url]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 yt-dlp，請先安裝（brew install yt-dlp）") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"yt-dlp 逾時（{timeout} 秒）") from exc
    if done.returncode != 0:
        lines = [x for x in done.stderr.strip().splitlines() if x.strip()]
        raise RuntimeError(lines[-1] if lines else f"yt-dlp 結束碼 {done.returncode}")
    try:
        return json.loads(done.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("yt-dlp 回傳的不是有效資料") from exc


def triage_url(page_id: str, url: str, fetcher: Callable[[str], dict[str, Any]] = fetch_info) -> dict[str, Any]:
    """判斷單一網址，回傳分析檔的一列。"""
    row = {key: "" for key in FIELDS}
    row.update(id=page_id, url=url)
    if not is_youtube(url):
        row.update(type=TYPE_WEB, subtype=SUB_WEB, route=ROUTE_WEB,
                   reason="不是 YouTube 網址，視為網頁（嵌在網頁裡的影片不在這裡處理）")
        return row
    row["type"] = TYPE_VIDEO
    try:
        info = fetcher(url)
    except Exception as exc:  # 連不上、被擋、影片下架：不猜，標成無法判斷
        row.update(subtype=SUB_UNKNOWN, route=ROUTE_RETRY, reason=f"讀不到影片資訊：{exc}")
        return row
    row.update(classify_info(info))
    return row


def read_urls(path: Path) -> list[str]:
    """讀 csv 的 url 欄，處理方式和 crawler.py 一致（去追蹤參數、去重、保持順序）。"""
    from .crawler import clean_url
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        names = {x.strip().lower(): x for x in (reader.fieldnames or [])}
        if "url" not in names:
            raise SystemExit("輸入 CSV 必須有 url 欄位")
        col = names["url"]
        urls = [clean_url(row[col]) for row in reader if row.get(col, "").strip()]
    return list(dict.fromkeys(urls))


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：用 Excel 開啟中文不會亂碼
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="影片分流器：判斷每個網址的類型與影片字幕狀況（只讀資訊，不下載）。")
    ap.add_argument("--input", type=Path, default=ROOT / "urls.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "output" / "video_triage.csv")
    ap.add_argument("--limit", type=int, default=0, help="只處理前 N 個網址；0 表示全部")
    ap.add_argument("--only-videos", action="store_true", help="只輸出影片列")
    ap.add_argument("--add", action="append", default=[], metavar="編號=網址",
                    help="額外加入不在 csv 裡的影片（例如嵌在網頁裡的），可重複；例：OW6SiepWHCU=https://www.youtube.com/watch?v=OW6SiepWHCU")
    args = ap.parse_args()

    if args.limit < 0:
        raise SystemExit("--limit 不能是負數")
    urls = read_urls(args.input)
    if args.limit:
        urls = urls[:args.limit]
    jobs = [(f"page{index:03d}", url) for index, url in enumerate(urls, start=1)]
    for item in args.add:
        name, _, url = item.partition("=")
        if not name.strip() or not url.strip():
            raise SystemExit(f"--add 的格式是 編號=網址，收到：{item}")
        if any(name.strip() == existing for existing, _ in jobs):
            raise SystemExit(f"--add 的編號 {name.strip()} 已經存在，請換一個編號")
        jobs.append((name.strip(), url.strip()))
    rows = []
    for page_id, url in jobs:
        row = triage_url(page_id, url)
        if row["type"] == TYPE_VIDEO or not args.only_videos:
            rows.append(row)
        if row["type"] == TYPE_VIDEO:
            print(f"{row['id']}  {row['subtype']}  {row['reason']}")
    write_rows(args.out, rows)
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['type']}／{row['subtype']}"
        counts[key] = counts.get(key, 0) + 1
    print("\n分流結果：")
    for key, number in sorted(counts.items()):
        print(f"  {key}：{number}")
    print(f"已寫入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
