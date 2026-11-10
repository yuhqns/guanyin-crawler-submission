"""Transcribe a local audio/video file with Whisper large-v3-turbo via MercuryAPI.

Standard library (urllib) plus ffmpeg only, so nothing extra needs installing.
Segments use the same shape as crawler/transcribe.py:
``{"start_seconds", "end_seconds", "timestamp", "text"}``.

Usage (from the project folder):
    python -m crawler.transcribe_api --video-id AWP_v5I0zYA --audio path/to/audio.wav

The API key is read from the MERCURY_API_KEY environment variable, or from a
``.env`` file in the project folder (see ``.env.example``). It is never printed.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BASE_URL = os.environ.get("MERCURY_BASE_URL", "https://air.cgu.edu.tw/mercuryapi/v1").rstrip("/")
MODEL = "whisper-large-v3-turbo"
MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # the service limit is 64 MB
CHUNK_SECONDS = 30
# Short, domain-specific hint (same terms as crawler/transcribe.py). Keep it short:
# a long prompt can bias the decoder toward repeating prompt words.
DEFAULT_PROMPT = "桃園市、觀音區、石門水庫、草漯、移民新村。"
# Statuses that mean "this server does not accept verbose_json", not "bad key".
FALLBACK_STATUSES = {400, 404, 415, 422}
# Verification pass (gap fill + second pass). Thresholds come from a test on AWP_v5I0zYA.
GAP_SECONDS = 8        # a silence this long between two segments is re-transcribed on its own
GAP_PAD = 3            # seconds of context kept on each side of a gap
WINDOW_SECONDS = 60    # window length of the plain-text second pass
LOW_LOGPROB = -1.0     # avg_logprob below this is treated as low confidence
MAX_COMPRESSION = 2.4  # compression_ratio above this usually means a repetition loop
ROOT = Path(__file__).resolve().parent.parent


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


def _timestamp(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"


def load_api_key() -> str:
    key = os.environ.get("MERCURY_API_KEY", "").strip()
    if key:
        return key
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):]
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == "MERCURY_API_KEY":
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    raise RuntimeError("找不到 MERCURY_API_KEY：請在 .env 填入金鑰（可參考 .env.example）")


def _multipart(fields: list[tuple[str, str]], audio: Path) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="audio{audio.suffix.lower() or ".wav"}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    parts.append(audio.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _request(audio: Path, fields: list[tuple[str, str]], key: str, timeout: int = 600) -> dict[str, Any]:
    body, content_type = _multipart(fields, audio)
    req = urllib.request.Request(
        f"{BASE_URL}/audio/transcriptions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": content_type,
            "User-Agent": "guanyin-crawler",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise ApiError(exc.code, detail) from exc


def _segments_from_verbose(data: dict[str, Any], offset: float = 0.0) -> list[dict[str, Any]]:
    result = []
    for item in data.get("segments") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = float(item.get("start") or 0) + offset
        end = float(item.get("end") or 0) + offset
        segment = {"start_seconds": start, "end_seconds": max(end, start),
                   "timestamp": _timestamp(start), "text": text}
        for name in ("avg_logprob", "compression_ratio"):
            if isinstance(item.get(name), (int, float)):
                segment[name] = float(item[name])
        result.append(segment)
    return result


def _prompt_field(prompt: str) -> list[tuple[str, str]]:
    return [("prompt", prompt)] if prompt else []


def _transcribe_chunked(audio: Path, key: str, work_dir: Path, prompt: str = "") -> list[dict[str, Any]]:
    """Fallback: cut into fixed windows and label each with its window start."""
    pattern = work_dir / "chunk_%04d.wav"
    command = [
        "ffmpeg", "-y", "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
        "-reset_timestamps", "1", str(pattern),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffmpeg；請先安裝 ffmpeg") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:]
        raise RuntimeError(f"ffmpeg 切段失敗：{' '.join(detail)}") from exc
    result = []
    for index, chunk in enumerate(sorted(work_dir.glob("chunk_*.wav"))):
        data = _request(
            chunk,
            [("model", MODEL), ("language", "zh"), ("response_format", "json")] + _prompt_field(prompt),
            key,
        )
        text = str(data.get("text") or "").strip()
        if text:
            start = index * CHUNK_SECONDS
            result.append({"start_seconds": float(start), "end_seconds": float(start + CHUNK_SECONDS),
                           "timestamp": _timestamp(start), "text": text})
    return result


def transcribe_audio(audio: Path, key: str, prompt: str = "",
                     force_chunked: bool = False) -> tuple[list[dict[str, Any]], str]:
    """Return ``(segments, timestamp_mode)``; mode is ``api_segments`` or ``chunked_30s``."""
    if not audio.is_file():
        raise FileNotFoundError(f"找不到音訊檔：{audio}")
    if not force_chunked and audio.stat().st_size <= MAX_UPLOAD_BYTES:
        try:
            data = _request(
                audio,
                [("model", MODEL), ("language", "zh"), ("response_format", "verbose_json"),
                 ("timestamp_granularities[]", "segment")] + _prompt_field(prompt),
                key,
            )
            segments = _segments_from_verbose(data)
            if segments:
                return segments, "api_segments"
        except ApiError as exc:
            if exc.status not in FALLBACK_STATUSES:
                raise
    with tempfile.TemporaryDirectory(prefix="guanyin_turbo_") as tmp:
        return _transcribe_chunked(audio, key, Path(tmp), prompt), "chunked_30s"


def _clip(audio: Path, start: float, seconds: float, out: Path) -> Path:
    command = ["ffmpeg", "-y", "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}", "-i", str(audio),
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffmpeg；請先安裝 ffmpeg") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:]
        raise RuntimeError(f"ffmpeg 切段失敗：{' '.join(detail)}") from exc
    return out


def _duration(audio: Path) -> float:
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(audio)],
            check=True, capture_output=True, text=True)
        return float(done.stdout.strip())
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError("無法讀取音訊長度（需要 ffprobe，安裝 ffmpeg 時會一併安裝）") from exc


def _norm(text: str) -> str:
    """Text with spaces and punctuation removed, for comparing two passes."""
    return re.sub(r"[\W_]+", "", text)


def _is_low_confidence(segment: dict[str, Any]) -> bool:
    return (segment.get("avg_logprob", 0.0) < LOW_LOGPROB
            or segment.get("compression_ratio", 0.0) > MAX_COMPRESSION)


def fill_gaps(audio: Path, segments: list[dict[str, Any]], key: str, work_dir: Path,
              duration: float) -> tuple[list[dict[str, Any]], int]:
    """Re-transcribe every stretch longer than GAP_SECONDS that has no text.

    A whole-file request sometimes skips half a minute of speech; sending that
    stretch on its own usually recovers it. Returns ``(segments, added)``.
    """
    ordered = sorted(segments, key=lambda s: s["start_seconds"])
    edges = [0.0] + [s["end_seconds"] for s in ordered]
    starts = [s["start_seconds"] for s in ordered] + [duration]
    added: list[dict[str, Any]] = []
    for index, (gap_start, gap_end) in enumerate(zip(edges, starts)):
        if gap_end - gap_start < GAP_SECONDS:
            continue
        clip_start = max(0.0, gap_start - GAP_PAD)
        clip_end = min(duration, gap_end + GAP_PAD)
        clip = _clip(audio, clip_start, clip_end - clip_start, work_dir / f"gap_{index:04d}.wav")
        data = _request(
            clip,
            [("model", MODEL), ("language", "zh"), ("response_format", "verbose_json"),
             ("timestamp_granularities[]", "segment")],
            key,
        )
        known = {_norm(s["text"]) for s in ordered}
        for segment in _segments_from_verbose(data, clip_start):
            inside = gap_start - 0.5 <= segment["start_seconds"] < gap_end - 0.5
            # Music-only stretches make the model invent text; those score low.
            if inside and not _is_low_confidence(segment) and _norm(segment["text"]) not in known:
                segment["source"] = "gap_fill"
                added.append(segment)
    merged = sorted(ordered + added, key=lambda s: s["start_seconds"])
    return merged, len(added)


def compare_second_pass(audio: Path, segments: list[dict[str, Any]], key: str, work_dir: Path,
                        duration: float) -> int:
    """Transcribe again in ~WINDOW_SECONDS windows and record where the text differs.

    The two passes make different mistakes, so a difference marks a line worth a
    human listen. Each differing segment gets ``segment["alt"]`` (second-pass text,
    possibly empty). Segments are edited in place; returns the number of windows.
    """
    cuts = [0.0]
    for segment in segments:
        if segment["start_seconds"] >= cuts[-1] + WINDOW_SECONDS:
            cuts.append(max(0.0, segment["start_seconds"] - 0.3))
    cuts.append(duration)
    for index, (start, end) in enumerate(zip(cuts, cuts[1:])):
        inside = [s for s in segments if start <= s["start_seconds"] < end]
        if not inside or end - start < 1:
            continue
        clip = _clip(audio, start, end - start, work_dir / f"pass2_{index:04d}.wav")
        data = _request(clip, [("model", MODEL), ("language", "zh"), ("response_format", "json"),
                               ("temperature", "0")], key)
        second = _norm(str(data.get("text") or ""))
        first_parts = [_norm(s["text"]) for s in inside]
        first = "".join(first_parts)
        # position in first-pass text -> position in second-pass text
        mapping = [0] * (len(first) + 1)
        for _tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, first, second, autojunk=False).get_opcodes():
            for i in range(i1, i2):
                mapping[i] = min(j1 + (i - i1), j2)
            mapping[i2] = j2
        position = 0
        for segment, part in zip(inside, first_parts):
            alt = second[mapping[position]:mapping[position + len(part)]]
            position += len(part)
            # A sentence-final particle heard or not heard is not worth a human listen.
            if re.sub(r"[啊啦嘛喔呢吧]", "", alt) != re.sub(r"[啊啦嘛喔呢吧]", "", part):
                segment["alt"] = alt
    return len(cuts) - 1


def _lookup_record(video_id: str) -> str:
    records = ROOT / "data" / "records.jsonl"
    if records.is_file():
        for line in records.open(encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(record.get("video_id") or "") == video_id:
                return str(record.get("id") or video_id)
    return video_id


def _read_meta(record_id: str) -> tuple[str, str]:
    video_md = ROOT / "output" / "videos" / record_id / "video.md"
    title, url = record_id, ""
    if video_md.is_file():
        for line in video_md.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("# ") and title == record_id:
                title = line[2:].strip()
            match = re.match(r"- 原始網址：(.+)", line)
            if match:
                url = match.group(1).strip()
    return title, url


def render_markdown(title: str, video_id: str, url: str, segments: list[dict[str, Any]],
                    mode: str, audio_name: str, prompt: str = "",
                    verified: bool = False, gaps_filled: int = 0) -> str:
    mode_note = ("API 回傳的段落時間" if mode == "api_segments"
                 else f"每 {CHUNK_SECONDS} 秒一段，時間點為各段起點（較粗略）")
    review = [s for s in segments if "alt" in s or _is_low_confidence(s)] if verified else []
    verify_note = (f"已做（補回漏掉的句子 {gaps_filled} 句；待人工確認 {len(review)} 句）"
                   if verified else "未做")
    lines = [
        f"# {title}", "",
        f"- YouTube Video ID：{video_id}",
        f"- 原始網址：{url or '未知'}",
        f"- 辨識方式：Whisper {MODEL}（MercuryAPI）",
        "- 語言：zh",
        f"- 時間點來源：{mode_note}",
        f"- 音訊檔：{audio_name}",
        f"- 專有名詞提示：{prompt or '無'}",
        f"- 補洞與兩遍比對：{verify_note}",
        f"- 文字片段數量：{len(segments)}", "",
        "## 字幕與逐字稿", "",
        "### Whisper large-v3-turbo 逐字稿",
    ]
    lines += [f"- **[{s['timestamp']}]** {s['text']}" for s in segments]
    if review:
        lines += ["", "### 待人工確認的句子", "",
                  "同一段聲音辨識兩遍，結果不同或信心偏低的句子。兩種寫法都可能是錯的，請以影片聲音為準。", ""]
        for s in review:
            if "alt" not in s:
                note = "信心偏低"
            elif s["alt"]:
                note = f"第二遍聽成：{s['alt']}"
            else:
                note = "第二遍沒有這一句"
            lines.append(f"- **[{s['timestamp']}]** {s['text']} ｜ {note}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="用 Whisper large-v3-turbo（MercuryAPI）產生帶時間點的逐字稿")
    parser.add_argument("--video-id", required=True, help="YouTube Video ID，例如 AWP_v5I0zYA")
    parser.add_argument("--audio", required=True, type=Path, help="音訊或影片檔路徑（wav/mp3/m4a/flac/ogg）")
    parser.add_argument("--out", type=Path, help="輸出 markdown 路徑（預設 output/videos/<id>/<id>_turbo.md）")
    parser.add_argument("--prompt", default="",
                        help=f"專有名詞提示，預設不使用。實測整段影片送出時會讓文字大量遺漏；可試的詞彙：{DEFAULT_PROMPT}")
    parser.add_argument("--chunked", action="store_true",
                        help=f"強制每 {CHUNK_SECONDS} 秒切一段再逐段辨識（時間點較粗略，但不易整段遺漏）")
    parser.add_argument("--no-verify", action="store_true",
                        help="不做補洞與第二遍比對（較快，但可能漏句，也不會列出待人工確認的句子）")
    args = parser.parse_args()

    try:
        key = load_api_key()
    except RuntimeError as exc:
        print(f"辨識失敗：{exc}", file=sys.stderr)
        return 1
    record_id = _lookup_record(args.video_id)
    out = args.out or ROOT / "output" / "videos" / record_id / f"{record_id}_turbo.md"
    title, url = _read_meta(record_id)

    print(f"開始辨識 {args.video_id}（{args.audio.name}），上傳到 {BASE_URL} ...")
    verified, gaps_filled = False, 0
    try:
        segments, mode = transcribe_audio(args.audio, key, args.prompt, args.chunked)
        if segments and mode == "api_segments" and not args.no_verify:
            duration = _duration(args.audio)
            with tempfile.TemporaryDirectory(prefix="guanyin_verify_") as tmp:
                print("補洞：重新辨識沒有文字的空白區間 ...")
                segments, gaps_filled = fill_gaps(args.audio, segments, key, Path(tmp), duration)
                print(f"第二遍：每約 {WINDOW_SECONDS} 秒一段重新辨識並比對 ...")
                compare_second_pass(args.audio, segments, key, Path(tmp), duration)
            verified = True
    except (ApiError, RuntimeError, FileNotFoundError, urllib.error.URLError) as exc:
        print(f"辨識失敗：{exc}", file=sys.stderr)
        return 1
    if not segments:
        print("辨識失敗：服務沒有回傳任何文字", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_markdown(title, args.video_id, url, segments, mode, args.audio.name, args.prompt,
                                   verified, gaps_filled), encoding="utf-8")
    review = sum(1 for s in segments if "alt" in s or _is_low_confidence(s)) if verified else 0
    print(f"完成：{len(segments)} 段，時間點模式 {mode}，已寫入 {out}")
    if verified:
        print(f"補回漏掉的句子 {gaps_filled} 句；待人工確認 {review} 句（列在檔案最後）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
