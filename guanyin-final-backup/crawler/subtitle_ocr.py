"""把影片畫面上「壓在畫面裡的字幕」讀成帶時間點的文字（macOS 的 Apple Vision）。

流程：
1. 先每秒切 1 張畫面下半部（不預設字幕在哪裡），用 Apple Vision 讀文字
   （crawler/subtitle_ocr.swift），從結果自己找出這部影片的字幕列
   （每句都不一樣、位置和字高一致的那一列；置中、靠左、靠右都可以）。
2. 依找到的位置重新截圖：每秒切 2 張，只留字幕那一條，再讀一次。
3. 依位置濾掉台標、人名條、畫面裡的雜字，只留字幕列裡的文字。
4. 字幕列裡有字、但不在正中間的畫面（例如字幕和「資料來源」擠在同一列，被讀成一整行），
   依這部影片字幕的對齊方式（置中、靠左或靠右）重新裁切後再讀一次，不直接放棄。
5. 連續幾張相同的字幕合併成一句，並用多張畫面投票決定文字。
6. 這支程式只看畫面，完全不用語音辨識的結果（它是拿來驗證語音辨識的標準答案，
   不能反過來被語音影響）。讀得沒把握的句子照實標出來，連續一段時間畫面上沒有讀到字幕的
   時段也列出來，交給人看畫面確認。

用法：
    python -m crawler.subtitle_ocr --video-id AWP_v5I0zYA --video /path/to/video.mp4
    python -m crawler.subtitle_ocr --video-id AWP_v5I0zYA --url https://youtu.be/AWP_v5I0zYA

用 --url 時，影片只會下載到暫存資料夾，切完畫面就刪除；專案裡只留下文字。
只能在 macOS 執行（需要 swiftc 與 ffmpeg；--url 另外需要 yt-dlp）。
"""
from __future__ import annotations

import argparse
import difflib
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .transcribe_api import ROOT, _lookup_record, _norm, _read_meta, _timestamp

FPS = 2                 # 每秒切幾張畫面
SEARCH_FPS = 1          # 找字幕位置時，每秒切幾張
SEARCH_TOP = 0.50       # 找字幕位置時，切畫面下半部（不預設字幕在哪裡）
SEARCH_HEIGHT = 0.50
BAND_HEIGHT = 0.20      # 找到字幕列後，重新截圖的那一條佔畫面高度多少
BAND_ABOVE_ROW = 0.06   # 那一條從字幕列上緣再往上多留多少
# 以下位置都是「整張畫面」的比例（0 在上、1 在下）
ANCHOR_TOLERANCE = 0.03  # 字幕的對齊位置（置中、靠左或靠右）容許差多少
MIN_ANCHOR_SHARE = 0.35  # 同一列的字幕裡，至少這個比例對齊同一個位置，才認定它有固定的對齊方式
ROW_BOTTOM_TOLERANCE = 0.03     # 字幕下緣和這部影片的字幕列差多少以內算同一列
ROW_HEIGHT_RANGE = (0.5, 1.8)   # 字高相對於這部影片字幕常見字高的範圍
GAP_SECONDS = 4.0       # 連續幾秒沒讀到字幕，就列為「沒讀到字幕的時段」
EXTRA_ROW_EVIDENCE = 8   # 另一種字幕樣式：同一個位置至少要有幾句「不同的」字幕
EXTRA_MAX_FRAMES = 4.0   # 另一種樣式的字幕，每句平均最多停留幾張畫面（每秒 1 張）；停得更久的是人名條、來源標籤
EXTRA_BOTTOM_TOLERANCE = 0.02   # 另一種樣式：下緣差多少以內算同一列
MAX_EXTRA_ROWS = 3
EXTRA_HEIGHT_RANGE = (0.4, 2.0)  # 另一種樣式的字高，相對於主要字幕字高的範圍（片頭的大標題、特效字不算字幕）
CAPTION_SIMILAR = 0.60  # 兩段文字相似到這個程度，算同一句字幕（避免辨識的小誤差被當成不同句）
MIN_ROW_EVIDENCE = 10   # 同一個高度至少要有幾句「不同的」長文字，才敢說找到了字幕列
RECAPTURE_MARGIN = 0.01  # 重新裁切時，字幕左右各多留的空間
SAME_SUBTITLE = 0.70    # 兩張畫面的文字相似到這個程度就算同一句
DROPPED_CHAR = "的"     # 實測 Apple Vision 常把這個字整個漏掉（畫面上明明有）
SWIFT_SOURCE = Path(__file__).with_name("subtitle_ocr.swift")


def _run(command: list[str], what: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{what}失敗：找不到 {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"{what}失敗：{' / '.join(detail)}") from exc


def download_video(url: str, work_dir: Path, height: int) -> Path:
    """Download the picture only (no sound) into the temporary folder."""
    target = work_dir / "video.%(ext)s"
    _run([sys.executable, "-m", "yt_dlp", "-q", "--no-progress", "--no-warnings",
          "-f", f"bestvideo[height<={height}]", "-o", str(target), url], "下載影片畫面（需要 yt-dlp）")
    found = sorted(work_dir.glob("video.*"))
    if not found:
        raise RuntimeError("下載影片畫面失敗：沒有產生檔案")
    return found[0]


def extract_frames(video: Path, frames_dir: Path, fps: int, top: float, height: float) -> int:
    frames_dir.mkdir(parents=True, exist_ok=True)
    crop = f"fps={fps},crop=iw:ih*{height}:0:ih*{top}"
    _run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", crop, "-q:v", "2",
          str(frames_dir / "f_%05d.jpg")], "切畫面（需要 ffmpeg）")
    return len(list(frames_dir.glob("f_*.jpg")))


def build_vision(work_dir: Path) -> Path:
    """Compile the Swift tool into the temporary folder."""
    if sys.platform != "darwin":
        raise RuntimeError("畫面字幕辨識只能在 macOS 執行（使用 Apple Vision）")
    binary = work_dir / "subtitle_ocr"
    _run(["swiftc", "-O", str(SWIFT_SOURCE), "-o", str(binary)], "編譯字幕辨識工具（需要 Xcode 命令列工具）")
    return binary


def run_vision(binary: Path, frames_dir: Path) -> str:
    return _run([str(binary), str(frames_dir)], "辨識畫面字幕").stdout.decode("utf-8", errors="replace")


def parse_lines(tsv: str, band_top: float, band_height: float) -> dict[int, list[dict[str, Any]]]:
    """Every text line Vision found, per frame, with positions as fractions of the whole picture."""
    frames: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for line in tsv.splitlines():
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        name, conf, left, right, top, height, text = parts
        match = re.search(r"(\d+)", name)
        try:
            conf_f, left_f, right_f, top_f, height_f = map(float, (conf, left, right, top, height))
        except ValueError:
            continue
        if not match or conf_f < 0 or len(_norm(text)) < 2:
            continue
        frames[int(match.group(1))].append({
            "text": text.strip(), "conf": conf_f, "left": left_f, "right": right_f,
            "bottom": band_top + (top_f + height_f) * band_height,
            "height": height_f * band_height,
        })
    return frames


def _center(line: dict[str, Any]) -> float:
    return (line["left"] + line["right"]) / 2


def _anchor_value(line: dict[str, Any], kind: str) -> float:
    return {"center": _center(line), "left": line["left"], "right": line["right"]}[kind]


def find_anchor(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How the subtitles in the row are aligned: centred, left-aligned or right-aligned, and where."""
    best: tuple[str, float, float] | None = None
    for kind in ("center", "left", "right"):          # 平手時優先採用置中
        values = [_anchor_value(line, kind) for line in rows]
        top = max(values, key=lambda v: sum(1 for w in values if abs(w - v) <= ANCHOR_TOLERANCE))
        near = [w for w in values if abs(w - top) <= ANCHOR_TOLERANCE]
        share = len(near) / len(values)
        if best is None or share > best[2]:
            best = (kind, statistics.median(near), share)
    widths = sorted(line["right"] - line["left"] for line in rows)
    kind, position, share = best
    return {"anchor": kind if share >= MIN_ANCHOR_SHARE else "none", "x": position,
            "width": widths[int(0.95 * (len(widths) - 1))]}


def find_subtitle_row(lines: dict[int, list[dict[str, Any]]]) -> dict[str, Any] | None:
    """Find where this video's subtitles sit.

    The subtitle row is the height where the most *different* long texts appear (a station logo or
    watermark shows the same text every frame, so it does not count), and then how they are aligned.
    """
    long_lines = [line for found in lines.values() for line in found if len(_norm(line["text"])) >= 5]
    if len(long_lines) < MIN_ROW_EVIDENCE:
        return None
    texts: dict[float, set[str]] = defaultdict(set)
    for line in long_lines:
        texts[round(line["bottom"], 2)].add(_norm(line["text"]))
    mode = max(texts, key=lambda height: len(texts[height]))
    if len(texts[mode]) < MIN_ROW_EVIDENCE:
        return None
    same = [line for line in long_lines if abs(line["bottom"] - mode) <= ROW_BOTTOM_TOLERANCE]
    row = {"bottom": statistics.median(line["bottom"] for line in same),
           "height": statistics.median(line["height"] for line in same)}
    row.update(find_anchor(same))
    return row


def _distinct_captions(members: list[tuple[int, dict[str, Any]]]) -> list[str]:
    kept: list[str] = []
    for _, line in members:
        if not any(_similar(line["text"], seen) >= CAPTION_SIMILAR for seen in kept):
            kept.append(line["text"])
    return kept


def find_extra_rows(lines: dict[int, list[dict[str, Any]]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Other subtitle styles in the same video (an opening that uses small captions on the left, say).

    Nothing about the look is assumed. A place counts as another subtitle row when many *different* captions
    appear there, aligned the same way, and each stays only briefly (people and source labels sit in a fixed
    place too, but each stays for many seconds, so they do not count), and the text is about as large as
    the main subtitles (a big title card is not a subtitle).
    """
    pool = [(index, line) for index in sorted(lines) for line in lines[index]
            if len(_norm(line["text"])) >= 2]
    taken = list(rows)
    found: list[dict[str, Any]] = []
    while len(found) < MAX_EXTRA_ROWS:
        pool = [(i, l) for i, l in pool if not any(abs(l["bottom"] - r["bottom"]) <= ROW_BOTTOM_TOLERANCE for r in taken)]
        seeds = {(round(l["bottom"], 2), kind, round(_anchor_value(l, kind), 2))
                 for _, l in pool for kind in ("center", "left", "right")}
        best: tuple[tuple[int, bool], list[tuple[int, dict[str, Any]]], str] | None = None
        for bottom, kind, position in seeds:
            members = [(i, l) for i, l in pool if abs(l["bottom"] - bottom) <= EXTRA_BOTTOM_TOLERANCE
                       and abs(_anchor_value(l, kind) - position) <= ANCHOR_TOLERANCE]
            if len(members) < EXTRA_ROW_EVIDENCE:
                continue
            size = statistics.median(l["height"] for _, l in members)
            if not rows[0]["height"] * EXTRA_HEIGHT_RANGE[0] <= size <= rows[0]["height"] * EXTRA_HEIGHT_RANGE[1]:
                continue
            captions = _distinct_captions(members)
            frames = len({i for i, _ in members})
            if len(captions) < EXTRA_ROW_EVIDENCE or frames / len(captions) > EXTRA_MAX_FRAMES:
                continue
            key = (len(captions), kind == "center")        # 平手時優先採用置中
            if best is None or key > best[0]:
                best = (key, members, kind)
        if best is None:
            break
        _, members, kind = best
        lines_in = [l for _, l in members]
        widths = sorted(l["right"] - l["left"] for l in lines_in)
        row = {"bottom": statistics.median(l["bottom"] for l in lines_in),
               "height": statistics.median(l["height"] for l in lines_in),
               "anchor": kind, "x": statistics.median(_anchor_value(l, kind) for l in lines_in),
               "width": widths[int(0.95 * (len(widths) - 1))], "captions": best[0][0]}
        found.append(row)
        taken.append(row)
    return found


def describe_row(row: dict[str, Any]) -> str:
    names = {"center": "置中", "left": "靠左", "right": "靠右", "none": "沒有固定的對齊位置"}
    where = f"（約在畫面寬度 {row['x'] * 100:.0f}% 處）" if row["anchor"] != "none" else ""
    return f"畫面高度約 {row['bottom'] * 100:.0f}%，{names[row['anchor']]}{where}"


def _in_row(line: dict[str, Any], row: dict[str, Any]) -> bool:
    return (abs(line["bottom"] - row["bottom"]) <= ROW_BOTTOM_TOLERANCE
            and row["height"] * ROW_HEIGHT_RANGE[0] <= line["height"] <= row["height"] * ROW_HEIGHT_RANGE[1])


def pick_lines(lines: dict[int, list[dict[str, Any]]], row: dict[str, Any]
               ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Per frame: the line in the subtitle row that sits where this video's subtitles are aligned
    (centred, left or right), or, if none, a line that runs across that spot to re-capture."""
    accepted: dict[int, dict[str, Any]] = {}
    flagged: dict[int, dict[str, Any]] = {}
    kind, position = row["anchor"], row["x"]
    for index, found in lines.items():
        best, off = None, None
        for line in found:
            if not _in_row(line, row):
                continue
            width = line["right"] - line["left"]
            if kind == "none" or abs(_anchor_value(line, kind) - position) <= ANCHOR_TOLERANCE:
                if best is None or width > best["width"]:
                    best = {"text": line["text"], "conf": line["conf"], "width": width}
            elif line["left"] - ANCHOR_TOLERANCE < position < line["right"] + ANCHOR_TOLERANCE:
                # 橫跨字幕該在的位置：多半是字幕和旁邊的字（例如資料來源）被讀成同一行
                if off is None or width > off["right"] - off["left"]:
                    off = line
        if best:
            accepted[index] = best
        elif off:
            flagged[index] = off
    return accepted, flagged


def _crop_window(line: dict[str, Any], row: dict[str, Any]) -> tuple[float, float]:
    """Left and right edge (as fractions of the picture width) to cut around where the subtitle must be."""
    kind, position, width = row["anchor"], row["x"], row["width"]
    if kind == "left":
        begin, end = position - RECAPTURE_MARGIN, position + width + RECAPTURE_MARGIN
    elif kind == "right":
        begin, end = position - width - RECAPTURE_MARGIN, position + RECAPTURE_MARGIN
    else:
        # 置中：知道左邊緣，右邊緣就是以字幕中心線對稱的另一側
        begin, end = line["left"] - RECAPTURE_MARGIN, 2 * position - line["left"] + RECAPTURE_MARGIN
        if end - begin < 0.1:
            begin, end = position - width / 2 - RECAPTURE_MARGIN, position + width / 2 + RECAPTURE_MARGIN
    return max(0.0, begin), min(1.0, end)


def recapture(flagged: dict[int, dict[str, Any]], frames_dir: Path, work_dir: Path,
              binary: Path, row: dict[str, Any], band_top: float) -> dict[int, dict[str, Any]]:
    """Cut those frames again around where the subtitle must be, and read them again.

    Subtitles keep to one alignment, so the cut is fixed by it (a centred subtitle is mirror-symmetric,
    a left- or right-aligned one has a known edge and a usual width); anything else on the row
    (a source credit at the side, say) falls outside the new crop.
    """
    if not flagged:
        return {}
    out_dir = work_dir / "recapture"
    out_dir.mkdir(exist_ok=True)
    windows: dict[int, tuple[float, float]] = {}
    for index, line in flagged.items():
        begin, end = _crop_window(line, row)
        windows[index] = (begin, end)
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(frames_dir / f"f_{index:05d}.jpg"),
              "-vf", f"crop=iw*{end - begin:.4f}:ih:iw*{begin:.4f}:0", "-q:v", "2",
              str(out_dir / f"f_{index:05d}.jpg")], "重新裁切字幕位置（需要 ffmpeg）")
    again = parse_lines(run_vision(binary, out_dir), band_top, BAND_HEIGHT)
    for index, found in again.items():                # 裁切後的座標換回整張畫面的座標
        begin, end = windows.get(index, (0.0, 1.0))
        for line in found:
            line["left"], line["right"] = begin + line["left"] * (end - begin), begin + line["right"] * (end - begin)
    accepted, _ = pick_lines(again, row)
    return accepted


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b), autojunk=False).ratio()


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip(" ：:，,。.、;；-—")


def merge_subtitles(frames: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Join consecutive frames showing the same subtitle and vote on the text."""
    groups: list[dict[str, Any]] = []
    for index in sorted(frames):
        frame = frames[index]
        last = groups[-1] if groups else None
        # 允許中間漏掉一張畫面
        if last and index - last["last"] <= 2 and max(
                _similar(frame["text"], seen) for seen in set(last["texts"])) >= SAME_SUBTITLE:
            last["last"] = index
            last["texts"].append(frame["text"])
            last["confs"].append(frame["conf"])
        else:
            groups.append({"first": index, "last": index, "texts": [frame["text"]], "confs": [frame["conf"]]})

    subtitles = []
    for group in groups:
        score: dict[str, float] = defaultdict(float)
        best_conf: dict[str, float] = defaultdict(float)
        for text, conf in zip(group["texts"], group["confs"]):
            key = _clean(text)
            score[key] += conf
            best_conf[key] = max(best_conf[key], conf)
        winner = max(score, key=lambda key: (score[key], len(key)))
        # 有些畫面讀到「的」、有些漏掉：其他字都一樣時，採用有「的」的那個
        fuller = [key for key in score
                  if key.replace(DROPPED_CHAR, "") == winner.replace(DROPPED_CHAR, "") and len(key) > len(winner)]
        if fuller:
            best_conf[max(fuller, key=len)] = max(best_conf[winner], best_conf[max(fuller, key=len)])
            score[max(fuller, key=len)] += score[winner]
            winner = max(fuller, key=len)
        if not _norm(winner):
            continue
        count = len(group["texts"])
        share = score[winner] / sum(score.values())
        # 「不夠有把握」：只出現一張、信心偏低，或各張畫面讀出來的字很不一致
        weak = count < 2 or best_conf[winner] < 0.5 or share < 0.5
        subtitles.append({
            "start": (group["first"] - 1) / FPS,
            "end": group["last"] / FPS,
            "text": winner,
            "frames": count,
            "conf": best_conf[winner],
            "weak": weak,
            "variants": sorted(score, key=lambda key: -score[key]),
            "source": "ocr",
        })
    return subtitles


def find_gaps(subtitles: list[dict[str, Any]], duration: float) -> list[tuple[float, float]]:
    """Stretches where no subtitle was read for a while (the picture shows none, or it was missed)."""
    gaps, cursor = [], 0.0
    for subtitle in subtitles:
        if subtitle["start"] - cursor >= GAP_SECONDS:
            gaps.append((cursor, subtitle["start"]))
        cursor = max(cursor, subtitle["end"])
    if duration - cursor >= GAP_SECONDS:
        gaps.append((cursor, duration))
    return gaps


def render_markdown(title: str, video_id: str, url: str, lines: list[dict[str, Any]],
                    gaps: list[tuple[float, float]], video_name: str, notes: list[str]) -> str:
    weak = [line for line in lines if line["weak"]]
    out = [f"# {title}", "",
           f"- YouTube Video ID：{video_id}",
           f"- 原始網址：{url}",
           "- 辨識方式：畫面字幕辨識（Apple Vision，繁體中文）；完全沒有使用語音辨識的結果",
           *[f"- {note}" for note in notes],
           f"- 時間點來源：字幕出現在畫面上的時間（每秒取 {FPS} 張畫面）",
           f"- 影片檔：{video_name}（只用來切畫面，不保留）",
           f"- 畫面字幕句數：{len(lines)}；其中讀得沒把握：{len(weak)} 句；"
           f"連續 {GAP_SECONDS:.0f} 秒以上沒讀到字幕的時段：{len(gaps)} 段",
           "", "## 畫面字幕", ""]
    for line in lines:
        mark = "（待確認）" if line["weak"] else ""
        style = f" ｜樣式{line['style']}" if line.get("style", 1) > 1 else ""
        out.append(f"- **[{_timestamp(line['start'])}]** {line['text']}{mark}{style}")
    if weak:
        out += ["", "### 讀得沒把握的句子", "",
                "只出現一張畫面、信心偏低，或各張畫面讀出來的字不一致。可能是背景裡的雜字，也可能是字幕讀錯，"
                "請看畫面確認。右邊列出各張畫面讀到的版本。", ""]
        for line in weak:
            versions = " ／ ".join(line["variants"])
            out.append(f"- **[{_timestamp(line['start'])}]** {line['text']} ｜ 讀到的版本：{versions}")
    if gaps:
        out += ["", "### 沒讀到字幕的時段", "",
                "這些時段畫面上沒有讀到字幕：可能真的沒有（片頭、片尾、純畫面），也可能字幕換了位置或樣式而漏讀。"
                "請看畫面確認。", ""]
        for begin, end in gaps:
            out.append(f"- {_timestamp(begin)} – {_timestamp(end)}（{end - begin:.0f} 秒）")
    return "\n".join(out) + "\n"


def _video_duration(video: Path) -> float:
    result = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(video)], "讀取影片長度（需要 ffprobe）")
    try:
        return float(result.stdout.decode().strip())
    except ValueError as exc:
        raise RuntimeError("讀取影片長度失敗") from exc


def read_style(binary: Path, video: Path, work_dir: Path, row: dict[str, Any], name: str,
               refine: bool) -> tuple[dict[int, dict[str, Any]], dict[str, Any], int]:
    """Cut the pictures around one subtitle row, read them, re-capture the off-position ones."""
    band_top = min(max(0.0, row["bottom"] - row["height"] - BAND_ABOVE_ROW), 1.0 - BAND_HEIGHT)
    frames_dir = work_dir / name
    print(f"{name}：字幕位置 {describe_row(row)}；重新截圖…")
    count = extract_frames(video, frames_dir, FPS, band_top, BAND_HEIGHT)
    print(f"辨識 {count} 張畫面的字幕（13 分鐘的影片約需 3–5 分鐘）…")
    lines = parse_lines(run_vision(binary, frames_dir), band_top, BAND_HEIGHT)
    if refine:
        row = find_subtitle_row(lines) or row
    accepted, flagged = pick_lines(lines, row)
    recovered = recapture(flagged, frames_dir, work_dir, binary, row, band_top)
    print(f"字幕不在預期位置而重新裁切：{len(flagged)} 張，補回 {len(recovered)} 張")
    accepted.update(recovered)
    return accepted, row, len(recovered)


def read_subtitle_frames(video: Path, work_dir: Path) -> list[dict[str, Any]]:
    """Find where the subtitles are (every style the video uses), then read each one."""
    binary = build_vision(work_dir)
    print("先找字幕在畫面的哪裡…")
    search_dir = work_dir / "search"
    extract_frames(video, search_dir, SEARCH_FPS, SEARCH_TOP, SEARCH_HEIGHT)
    searched = parse_lines(run_vision(binary, search_dir), SEARCH_TOP, SEARCH_HEIGHT)
    row = find_subtitle_row(searched)
    if row is None:
        raise RuntimeError("找不到字幕列：這部影片可能沒有壓字幕，或字幕太少")
    shutil.rmtree(search_dir)
    extra = find_extra_rows(searched, [row])
    if extra:
        print(f"另外找到 {len(extra)} 種字幕樣式")
    styles = []
    for number, style_row in enumerate([row] + extra, start=1):
        frames, final_row, recovered = read_style(binary, video, work_dir, style_row, f"frames_{number}", number == 1)
        styles.append({"style": number, "row": final_row, "frames": frames, "recovered": recovered})
    return styles


def main() -> int:
    parser = argparse.ArgumentParser(description="用 Apple Vision 把影片畫面上的字幕讀成帶時間點的文字")
    parser.add_argument("--video-id", required=True, help="YouTube Video ID，例如 AWP_v5I0zYA")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--video", type=Path, help="已經在電腦裡的影片檔路徑")
    source.add_argument("--url", help="影片網址；只下載畫面到暫存資料夾，用完即刪")
    parser.add_argument("--height", type=int, default=720, help="下載的畫質上限（預設 720，實測 1080 沒有比較準）")
    parser.add_argument("--out", type=Path, help="輸出 markdown 路徑（預設 output/videos/<id>/<id>_ocr.md）")
    args = parser.parse_args()

    record_id = _lookup_record(args.video_id)
    title, url = _read_meta(record_id)
    folder = ROOT / "output" / "videos" / record_id
    out_path = args.out or folder / f"{record_id}_ocr.md"

    try:
        with tempfile.TemporaryDirectory(prefix="guanyin_ocr_") as temp:
            work_dir = Path(temp)
            if args.url:
                print("下載影片畫面到暫存資料夾…")
                video = download_video(args.url, work_dir, args.height)
            else:
                video = args.video
                if not video.is_file():
                    raise RuntimeError(f"找不到影片檔：{video}")
            duration = _video_duration(video)
            styles = read_subtitle_frames(video, work_dir)
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    subtitles: list[dict[str, Any]] = []
    notes: list[str] = []
    for style in styles:
        found = merge_subtitles(style["frames"])
        for subtitle in found:
            subtitle["style"] = style["style"]
        subtitles += found
        label = "字幕位置（樣式 1，主要字幕）" if style["style"] == 1 else f"另一種字幕樣式（樣式 {style['style']}）"
        notes.append(f"{label}：{describe_row(style['row'])}；{len(found)} 句"
                     f"（程式依這部影片自己找出，不預設）；重新截圖後補回 {style['recovered']} 張畫面")
    subtitles.sort(key=lambda item: (item["start"], item["style"]))
    if not subtitles:
        print("錯誤：畫面上沒有讀到字幕", file=sys.stderr)
        return 1
    gaps = find_gaps(subtitles, duration)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(title, args.video_id, url or args.url or "", subtitles,
                                        gaps, video.name, notes), encoding="utf-8")
    weak = sum(1 for line in subtitles if line["weak"])
    print(f"完成：畫面字幕 {len(subtitles)} 句（沒把握 {weak} 句），沒讀到字幕的時段 {len(gaps)} 段 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
