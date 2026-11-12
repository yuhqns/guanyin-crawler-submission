"""把影片畫面上「壓在畫面裡的字幕」讀成帶時間點的文字（macOS 的 Apple Vision）。

流程：
1. 先每秒切 1 張畫面下半部（不預設字幕在哪裡），用 Apple Vision 讀文字
   （crawler/subtitle_ocr.swift），從結果自己找出這部影片的字幕列
   （每句都出現、置中、位置和字高一致的那一列）。
2. 依找到的位置重新截圖：每秒切 2 張，只留字幕那一條，再讀一次。
3. 依位置濾掉台標、人名條、畫面裡的雜字，只留字幕列裡的文字。
4. 字幕列裡有字、但不在正中間的畫面（例如字幕和「資料來源」擠在同一列，被讀成一整行），
   依「字幕是置中的」這個性質重新裁切後再讀一次，不直接放棄。
5. 連續幾張相同的字幕合併成一句，並用多張畫面投票決定文字。
6. 和語音辨識的逐字稿（<id>_turbo.md）比對：兩邊不一樣的句子列在最後給人確認。
   畫面實在讀不到的地方（片頭、片尾沒有壓字幕等），才拿語音辨識當候選補上。

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
CENTER_STRICT = 0.03    # 字幕是置中的：中心點離正中央不超過這麼多才直接採用
ROW_BOTTOM_TOLERANCE = 0.03     # 字幕下緣和這部影片的字幕列差多少以內算同一列
ROW_HEIGHT_RANGE = (0.5, 1.8)   # 字高相對於這部影片字幕常見字高的範圍
MIN_ROW_EVIDENCE = 10   # 至少要有幾行置中的長文字，才敢說找到了字幕列
RECAPTURE_MARGIN = 0.01  # 重新裁切時，字幕左右各多留的空間
SAME_SUBTITLE = 0.70    # 兩張畫面的文字相似到這個程度就算同一句
DROPPED_CHAR = "的"     # 實測 Apple Vision 常把這個字整個漏掉（畫面上明明有）
PARTICLES = "啊啦嘛喔呢吧"  # 比對時忽略的語尾助詞
SWIFT_SOURCE = Path(__file__).with_name("subtitle_ocr.swift")
LINE_PATTERN = re.compile(r"- \*\*\[(\d+):(\d+):(\d+)\]\*\* (.*)")


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


def find_subtitle_row(lines: dict[int, list[dict[str, Any]]]) -> dict[str, float] | None:
    """Find where this video's subtitles sit: the row that many long, centred lines share."""
    core = [line for found in lines.values() for line in found
            if len(_norm(line["text"])) >= 5 and abs(_center(line) - 0.5) <= CENTER_STRICT]
    if len(core) < MIN_ROW_EVIDENCE:
        return None
    mode = Counter(round(line["bottom"], 2) for line in core).most_common(1)[0][0]
    same = [line for line in core if abs(line["bottom"] - mode) <= ROW_BOTTOM_TOLERANCE]
    return {"bottom": statistics.median(line["bottom"] for line in same),
            "height": statistics.median(line["height"] for line in same)}


def _in_row(line: dict[str, Any], row: dict[str, float]) -> bool:
    return (abs(line["bottom"] - row["bottom"]) <= ROW_BOTTOM_TOLERANCE
            and row["height"] * ROW_HEIGHT_RANGE[0] <= line["height"] <= row["height"] * ROW_HEIGHT_RANGE[1])


def pick_lines(lines: dict[int, list[dict[str, Any]]], row: dict[str, float]
               ) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    """Per frame: the centred line in the subtitle row, or (if none) a wide off-centre one to re-capture."""
    accepted: dict[int, dict[str, Any]] = {}
    flagged: dict[int, dict[str, Any]] = {}
    for index, found in lines.items():
        best, off = None, None
        for line in found:
            if not _in_row(line, row):
                continue
            width = line["right"] - line["left"]
            if abs(_center(line) - 0.5) <= CENTER_STRICT:
                if best is None or width > best["width"]:
                    best = {"text": line["text"], "conf": line["conf"], "width": width}
            elif line["left"] < 0.5 < line["right"]:    # 橫跨正中央：多半是字幕和旁邊的字被讀成一行
                if off is None or width > off["right"] - off["left"]:
                    off = line
        if best:
            accepted[index] = best
        elif off:
            flagged[index] = off
    return accepted, flagged


def recapture(flagged: dict[int, dict[str, Any]], frames_dir: Path, work_dir: Path,
              binary: Path, row: dict[str, float], band_top: float) -> dict[int, dict[str, Any]]:
    """Cut those frames again around where the subtitle must be, and read them again.

    Subtitles are centred, so if the left edge is known the right edge is its mirror image;
    anything else on the row (a source credit on the right, say) falls outside the new crop.
    """
    if not flagged:
        return {}
    out_dir = work_dir / "recapture"
    out_dir.mkdir(exist_ok=True)
    for index, line in flagged.items():
        x = max(0.0, line["left"] - RECAPTURE_MARGIN)
        width = 1.0 - 2 * x
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(frames_dir / f"f_{index:05d}.jpg"),
              "-vf", f"crop=iw*{width:.4f}:ih:iw*{x:.4f}:0", "-q:v", "2", str(out_dir / f"f_{index:05d}.jpg")],
             "重新裁切字幕位置（需要 ffmpeg）")
    accepted, _ = pick_lines(parse_lines(run_vision(binary, out_dir), band_top, BAND_HEIGHT), row)
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
            "source": "ocr",
        })
    return subtitles


def read_whisper(path: Path) -> list[dict[str, Any]]:
    """Read the '- **[HH:MM:SS]** text' lines of a transcript made by transcribe_api."""
    segments: list[dict[str, Any]] = []
    if not path.is_file():
        return segments
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("### 待人工確認"):
            break
        match = LINE_PATTERN.match(line)
        if match:
            hours, minutes, seconds, text = match.groups()
            segments.append({"start": int(hours) * 3600 + int(minutes) * 60 + int(seconds), "text": text.strip()})
    return segments


def _restore_dropped(text: str, heard: str) -> str:
    """Put back the characters Vision is known to drop, where speech recognition heard them."""
    out, restored = [], False
    matcher = difflib.SequenceMatcher(None, text, heard, autojunk=False)
    codes = matcher.get_opcodes()
    for position, (tag, a1, a2, b1, b2) in enumerate(codes):
        out.append(text[a1:a2])
        inside = 0 < position < len(codes) - 1     # 只補句子中間的，句首句尾可能是對齊誤差
        if tag == "insert" and heard[b1:b2] == DROPPED_CHAR and inside:
            out.append(DROPPED_CHAR)
            restored = True
    return "".join(out) if restored else text


def _loose(text: str) -> str:
    return "".join(ch for ch in _norm(text) if ch not in PARTICLES)


def compare_with_whisper(subtitles: list[dict[str, Any]], whisper: list[dict[str, Any]],
                         duration: float) -> list[dict[str, Any]]:
    """Mark where speech recognition heard something else; fill what the picture missed.

    Returns the combined list (on-screen subtitles plus speech-only lines), sorted by time.
    """
    if not whisper:
        return subtitles
    ocr_text, ocr_spans = "", []
    for subtitle in subtitles:
        piece = _norm(subtitle["text"])
        ocr_spans.append((len(ocr_text), len(ocr_text) + len(piece)))
        ocr_text += piece
    heard_text, heard_spans = "", []
    for segment in whisper:
        piece = _norm(segment["text"])
        heard_spans.append((len(heard_text), len(heard_text) + len(piece)))
        heard_text += piece

    to_heard: dict[int, int] = {}
    matched_heard: set[int] = set()
    matcher = difflib.SequenceMatcher(None, ocr_text, heard_text, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            to_heard[block.a + offset] = block.b + offset
            matched_heard.add(block.b + offset)

    ranges: list[tuple[int, int] | None] = []
    for begin, end in ocr_spans:
        hits = [to_heard[i] for i in range(begin, end) if i in to_heard]
        ranges.append((min(hits), max(hits) + 1) if hits else None)

    for position, subtitle in enumerate(subtitles):
        begin, end = ocr_spans[position]
        current = ranges[position]
        if current is None:
            subtitle["heard"] = ""      # 語音那邊找不到對應的句子
            continue
        low, high = current
        length = end - begin
        # 句首或句尾沒對上的字，往相鄰句子的邊界延伸，才看得到語音完整聽成什麼
        if begin not in to_heard:
            previous = next((r for r in reversed(ranges[:position]) if r), None)
            floor = previous[1] if previous else 0
            low = max(floor, low - length) if floor <= low else low
        if end - 1 not in to_heard:
            following = next((r for r in ranges[position + 1:] if r), None)
            ceiling = following[0] if following else len(heard_text)
            high = min(ceiling, high + length) if ceiling >= high else high
        heard = heard_text[low:high]
        subtitle["text"] = _restore_dropped(subtitle["text"], heard)
        if _loose(heard) != _loose(subtitle["text"]):
            subtitle["heard"] = heard

    # 畫面讀得沒把握、語音也沒有這一句：多半是背景裡的雜字，不放進正文
    for subtitle in subtitles:
        subtitle["skipped"] = bool(subtitle["weak"] and subtitle.get("heard") == "")

    combined = list(subtitles)
    for position, segment in enumerate(whisper):
        begin, end = heard_spans[position]
        if end == begin:
            continue
        matched = sum(1 for i in range(begin, end) if i in matched_heard) / (end - begin)
        start = segment["start"]
        stop = whisper[position + 1]["start"] if position + 1 < len(whisper) else duration
        on_screen = any(s["start"] < stop and s["end"] > start - 1 for s in subtitles)
        if matched < 0.5 and not on_screen:
            combined.append({"start": float(start), "end": float(stop), "text": segment["text"],
                             "source": "speech", "weak": True})
    combined.sort(key=lambda item: item["start"])
    return combined


def render_markdown(title: str, video_id: str, url: str, lines: list[dict[str, Any]],
                    video_name: str, compared: bool, notes: list[str]) -> str:
    skipped = [line for line in lines if line.get("skipped")]
    lines = [line for line in lines if not line.get("skipped")]
    on_screen = [line for line in lines if line["source"] == "ocr"]
    speech_only = [line for line in lines if line["source"] == "speech"]
    weak = [line for line in on_screen if line["weak"]]
    differ = [line for line in on_screen if "heard" in line and not line["weak"]]
    out = [f"# {title}", "",
           f"- YouTube Video ID：{video_id}",
           f"- 原始網址：{url}",
           "- 辨識方式：畫面字幕辨識（Apple Vision，繁體中文）",
           *[f"- {note}" for note in notes],
           f"- 時間點來源：字幕出現在畫面上的時間（每秒取 {FPS} 張畫面）",
           f"- 影片檔：{video_name}（只用來切畫面，不保留）",
           f"- 與語音辨識比對：{'已做' if compared else '未做（找不到語音逐字稿）'}",
           f"- 畫面字幕句數：{len(on_screen)}；畫面沒讀到、以語音辨識當候選補上：{len(speech_only)} 句",
           f"- 待人工確認：優先 {len(weak) + len(speech_only)} 句；字幕與語音不同 {len(differ)} 句",
           "", "## 字幕與逐字稿", ""]
    for line in lines:
        mark = "（語音候選）" if line["source"] == "speech" else ""
        out.append(f"- **[{_timestamp(line['start'])}]** {line['text']}{mark}")

    def describe(line: dict[str, Any]) -> str:
        if "heard" not in line:
            return ""
        return f" ｜ 語音聽成：{line['heard']}" if line["heard"] else " ｜ 語音沒有這一句"

    if weak or speech_only:
        out += ["", "### 請優先確認的句子", "",
                "畫面讀得不夠有把握（背景太雜、只閃過一張畫面），或畫面沒讀到、只能拿語音辨識當候選的句子。", ""]
        for line in lines:
            if line["source"] == "speech":
                out.append(f"- **[{_timestamp(line['start'])}]** {line['text']} ｜ 畫面沒讀到，這句只是語音辨識的候選")
            elif line["weak"]:
                out.append(f"- **[{_timestamp(line['start'])}]** {line['text']}{describe(line)} ｜ 畫面讀得不夠有把握")
    if differ:
        out += ["", "### 字幕與語音不同的句子（已採用畫面字幕）", "",
                "畫面字幕讀得穩定，但語音辨識聽成別的字。多半是語音聽錯，少數可能是畫面讀錯。", ""]
        for line in differ:
            out.append(f"- **[{_timestamp(line['start'])}]** {line['text']}{describe(line)}")
    if skipped:
        out += ["", "### 沒有放進正文的畫面文字", "",
                "畫面讀得沒把握、語音也沒有對應的句子，判斷是背景裡的雜字（文件、招牌）。若其實是字幕請補回。", ""]
        for line in skipped:
            out.append(f"- **[{_timestamp(line['start'])}]** {line['text']}")
    return "\n".join(out) + "\n"


def _video_duration(video: Path) -> float:
    result = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=nw=1:nk=1", str(video)], "讀取影片長度（需要 ffprobe）")
    try:
        return float(result.stdout.decode().strip())
    except ValueError as exc:
        raise RuntimeError("讀取影片長度失敗") from exc


def read_subtitle_frames(video: Path, work_dir: Path) -> tuple[dict[int, dict[str, Any]], dict[str, float], int]:
    """Find where the subtitles are, cut the pictures there, read them, re-capture the off-centre ones."""
    binary = build_vision(work_dir)
    print("先找字幕在畫面的哪裡…")
    search_dir = work_dir / "search"
    extract_frames(video, search_dir, SEARCH_FPS, SEARCH_TOP, SEARCH_HEIGHT)
    row = find_subtitle_row(parse_lines(run_vision(binary, search_dir), SEARCH_TOP, SEARCH_HEIGHT))
    if row is None:
        raise RuntimeError("找不到字幕列：這部影片可能沒有壓字幕，或字幕不是置中的")
    shutil.rmtree(search_dir)
    band_top = min(max(0.0, row["bottom"] - row["height"] - BAND_ABOVE_ROW), 1.0 - BAND_HEIGHT)

    frames_dir = work_dir / "frames"
    print(f"字幕大約在畫面高度 {row['bottom'] * 100:.0f}%，重新截圖…")
    count = extract_frames(video, frames_dir, FPS, band_top, BAND_HEIGHT)
    print(f"辨識 {count} 張畫面的字幕（13 分鐘的影片約需 3–5 分鐘）…")
    lines = parse_lines(run_vision(binary, frames_dir), band_top, BAND_HEIGHT)
    row = find_subtitle_row(lines) or row
    accepted, flagged = pick_lines(lines, row)
    recovered = recapture(flagged, frames_dir, work_dir, binary, row, band_top)
    print(f"字幕不在正中間而重新裁切：{len(flagged)} 張，補回 {len(recovered)} 張")
    accepted.update(recovered)
    return accepted, row, len(recovered)


def main() -> int:
    parser = argparse.ArgumentParser(description="用 Apple Vision 把影片畫面上的字幕讀成帶時間點的文字")
    parser.add_argument("--video-id", required=True, help="YouTube Video ID，例如 AWP_v5I0zYA")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--video", type=Path, help="已經在電腦裡的影片檔路徑")
    source.add_argument("--url", help="影片網址；只下載畫面到暫存資料夾，用完即刪")
    parser.add_argument("--height", type=int, default=720, help="下載的畫質上限（預設 720，實測 1080 沒有比較準）")
    parser.add_argument("--whisper", type=Path, help="語音逐字稿路徑（預設 output/videos/<id>/<id>_turbo.md）")
    parser.add_argument("--out", type=Path, help="輸出 markdown 路徑（預設 output/videos/<id>/<id>_ocr.md）")
    args = parser.parse_args()

    record_id = _lookup_record(args.video_id)
    title, url = _read_meta(record_id)
    folder = ROOT / "output" / "videos" / record_id
    whisper_path = args.whisper or folder / f"{record_id}_turbo.md"
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
            frames, row, recovered = read_subtitle_frames(video, work_dir)
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    subtitles = merge_subtitles(frames)
    if not subtitles:
        print("錯誤：畫面上沒有讀到字幕", file=sys.stderr)
        return 1
    whisper = read_whisper(whisper_path)
    lines = compare_with_whisper(subtitles, whisper, duration)
    notes = [f"字幕位置：畫面高度約 {row['bottom'] * 100:.0f}%（程式依這部影片自己找出，不預設）",
             f"字幕不在正中間、重新截圖後補回：{recovered} 張畫面"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(title, args.video_id, url or args.url or "", lines,
                                        video.name, bool(whisper), notes), encoding="utf-8")
    speech_only = sum(1 for line in lines if line["source"] == "speech")
    shown = sum(1 for line in lines if line["source"] == "ocr" and not line.get("skipped"))
    print(f"完成：畫面字幕 {shown} 句，語音候選 {speech_only} 句 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
