"""影片總入口：看每支影片走到哪一步，並告訴你下一步該下什麼指令。

不會自己執行任何耗時或需要判斷的步驟（下載影片、語音辨識、挑重點、人工確認都要人決定），
只負責「盤點」：依分流結果（output/video_triage.csv）檢查各步驟的輸出檔存不存在，
把第一個還沒做的步驟和對應的指令列出來。

用法（在專案根目錄）：
    python -m crawler.video_triage            # 先產生分流結果（會連網讀影片資訊，不下載影片）
    python -m crawler.video_pipeline status   # 看進度與下一步
    python -m crawler.video_pipeline status --md output/video_status.md   # 同時存成 Markdown

各路線的步驟：
    video_with_subtitles   字幕 → 重點 → 重點輸出 → 截圖
    funasr_ocr_review      畫面字幕 OCR → FunASR → 人工確認網頁 → 確認版逐字稿
    auto_caption_compare   沒字幕路線的所有步驟 + 自動字幕 → 自動字幕比較
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any, Callable

from .transcribe_api import ROOT
from .video_triage import ROUTE_AUTO_COMPARE, ROUTE_NO_SUBTITLES, ROUTE_SUBTITLED, TYPE_VIDEO

FUNASR_PYTHON = "~/.venvs/funasr/bin/python"   # 裝了 requirements-funasr.txt 的 Python


def _first(folder: Path, *patterns: str) -> Path | None:
    for pattern in patterns:
        found = sorted(folder.glob(pattern))
        if found:
            return found[0]
    return None


def _stage(label: str, done: bool, detail: str, command: str) -> dict[str, Any]:
    return {"label": label, "done": done, "detail": detail, "command": command}


def _highlight_count(path: Path) -> int:
    """重點檔裡有幾個重點；讀不到或格式不對回傳 0。"""
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("highlights", [])
    except (OSError, ValueError, AttributeError):
        return 0
    return len(items) if isinstance(items, list) else 0


def subtitled_stages(row: dict[str, str], folder: Path, video: Path) -> list[dict[str, Any]]:
    i = row["id"]
    shots = sorted((folder / "highlights").glob("hl_*.jpg"))
    expected = _highlight_count(folder / f"{i}_highlights.json")
    render_cmd = f"python -m crawler.subtitle_highlights render --id {i}"
    with_video = f"{render_cmd} --video {video}"
    video_hint = with_video if video.is_file() else f"需要影片檔 {video}（要下載請先確認有權處理），再執行：{with_video}"
    shots_done = bool(shots) and len(shots) >= max(expected, 1)
    shots_detail = f"{len(shots)} 張" if not expected else f"{len(shots)}/{expected} 張"
    return [
        _stage("字幕", (folder / f"{i}_subtitles.json").is_file(), f"{i}_subtitles.json",
               f"python -m crawler.subtitle_highlights fetch --id {i}"),
        _stage("重點", (folder / f"{i}_highlights.json").is_file(), f"{i}_highlights.json",
               f"讀 output/videos/{i}/{i}_subtitle_chunks.md，挑重點寫成 {i}_highlights.json（格式見 subtitle_highlights.py 開頭）"),
        _stage("重點輸出", (folder / f"{i}_highlights.md").is_file(), f"{i}_highlights.md",
               with_video if video.is_file() else render_cmd),
        _stage("截圖", shots_done, shots_detail, video_hint),
    ]


def _open_questions(confirmed: Path) -> str:
    """確認版逐字稿開頭會寫「沒有答案 N 個」；N>0 代表還有地方沒人決定，要在狀態裡提醒。"""
    try:
        head = "\n".join(confirmed.read_text(encoding="utf-8").splitlines()[:12])
    except OSError:
        return ""
    match = re.search(r"沒有答案\s*(\d+)\s*個", head)
    return f"；還有 {match.group(1)} 處沒人決定" if match and int(match.group(1)) else ""


def no_subtitle_stages(row: dict[str, str], folder: Path, video: Path) -> list[dict[str, Any]]:
    i, vid = row["id"], row["video_id"]
    ocr = folder / f"{i}_ocr.md"
    raw = _first(folder, f"{i}_funasr_glossary_raw.json", f"{i}_funasr_raw.json")
    review = _first(folder, "*_review.html")
    confirmed = folder / f"{i}_confirmed.md"
    need_video = "" if video.is_file() else f"（需要影片檔 {video}，要下載請先確認有權處理）"
    raw_name = raw.name if raw else f"{i}_funasr_glossary_raw.json"
    return [
        _stage("畫面字幕 OCR", ocr.is_file(), ocr.name,
               f"python -m crawler.subtitle_ocr --video-id {vid} --video {video}" + need_video),
        _stage("FunASR 語音辨識", raw is not None, raw_name,
               f"{FUNASR_PYTHON} -m crawler.transcribe_funasr --video-id {vid} --audio {video}" + need_video),
        _stage("人工確認網頁", review is not None or confirmed.is_file(), review.name if review else "（已有確認版時可略過）",
               f"python -m crawler.review_page build --video-id {vid} --raw {folder / raw_name} --ocr {ocr} --video {video}"),
        _stage("確認版逐字稿", confirmed.is_file(), confirmed.name + _open_questions(confirmed),
               f"在確認網頁選好後按「下載我的選擇」，再執行：python -m crawler.review_page apply --video-id {vid} "
               f"--raw {folder / raw_name} --ocr {ocr} --decisions <下載的 json>"),
    ]


def auto_stages(row: dict[str, str], folder: Path, video: Path) -> list[dict[str, Any]]:
    i = row["id"]
    return no_subtitle_stages(row, folder, video) + [
        _stage("自動字幕", (folder / f"{i}_auto_captions.json").is_file(), f"{i}_auto_captions.json",
               f"python -m crawler.subtitle_highlights fetch --id {i} --auto"),
        _stage("自動字幕比較", (folder / f"{i}_auto_vs_asr.md").is_file(), f"{i}_auto_vs_asr.md",
               f"python -m crawler.caption_compare --id {i}"),
    ]


ROUTES: dict[str, Callable[[dict[str, str], Path, Path], list[dict[str, Any]]]] = {
    ROUTE_SUBTITLED: subtitled_stages,
    ROUTE_NO_SUBTITLES: no_subtitle_stages,
    ROUTE_AUTO_COMPARE: auto_stages,
}


def read_triage(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise SystemExit(f"找不到分流結果 {path}；請先執行 python -m crawler.video_triage")
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [r for r in csv.DictReader(f) if r.get("type") == TYPE_VIDEO]


def inspect(row: dict[str, str], root: Path = ROOT) -> dict[str, Any]:
    """盤點單支影片：回傳 {row, stages, next}；next 是第一個還沒做的步驟（全做完為 None）。"""
    builder = ROUTES.get(row.get("route", ""))
    if not builder:
        return {"row": row, "stages": [], "next": {"label": "無法自動判斷", "command": row.get("reason", "")}}
    folder = root / "output" / "videos" / row["id"]
    video = root / "local_videos" / f"{row['video_id']}.mp4"
    stages = builder(row, folder, video)
    pending = next((s for s in stages if not s["done"]), None)
    return {"row": row, "stages": stages, "next": pending}


def render(results: list[dict[str, Any]]) -> str:
    lines = ["# 影片處理進度", ""]
    for result in results:
        row = result["row"]
        title = row.get("title") or row.get("video_id") or row["id"]
        lines += [f"## {row['id']}　{title}", "",
                  f"- 分流：{row['subtype']}（路線 `{row['route']}`）"]
        for stage in result["stages"]:
            mark = "✔" if stage["done"] else "✘"
            lines.append(f"- {mark} {stage['label']}　{stage['detail'] if stage['done'] else ''}".rstrip())
        nxt = result["next"]
        lines += ["", f"**下一步：{nxt['label']}**" if nxt else "**這支影片的步驟都完成了。**"]
        if nxt:
            lines += ["", "```", nxt["command"], "```"]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="影片總入口：盤點每支影片的進度與下一步。")
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("status", help="看進度與下一步")
    s.add_argument("--triage", type=Path, default=ROOT / "output" / "video_triage.csv")
    s.add_argument("--md", type=Path, help="同時存成 Markdown")
    args = ap.parse_args()
    results = [inspect(row) for row in read_triage(args.triage)]
    text = render(results)
    print(text)
    if args.md:
        args.md.write_text(text, encoding="utf-8")
        print(f"已寫入 {args.md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
