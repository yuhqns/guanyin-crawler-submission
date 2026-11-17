"""Run an additional Whisper pass and write the result directly into video.md."""
import json
import re
from pathlib import Path

from crawler.transcribe import transcribe_local_media

ROOT = Path(__file__).resolve().parent.parent

# 要額外辨識的 YouTube Video ID 與合法取得的本機影片檔。
VIDEO_ID = "AWP_v5I0zYA"
VIDEO_PATH = ROOT / "local_videos" / f"{VIDEO_ID}.mp4"
MODEL_NAME = "small"

EXTRA_START = "<!-- EXTRA_WHISPER_TRANSCRIPT_START -->"
EXTRA_END = "<!-- EXTRA_WHISPER_TRANSCRIPT_END -->"

if not VIDEO_PATH.is_file():
    raise FileNotFoundError(f"找不到本機影片：{VIDEO_PATH}")

# 由 records.jsonl 將 YouTube Video ID 對應到爬蟲使用的 page ID。
record_id = VIDEO_ID
records_path = ROOT / "data" / "records.jsonl"
if records_path.is_file():
    with records_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(record.get("video_id") or "") == VIDEO_ID:
                record_id = str(record.get("id") or VIDEO_ID)
                break

print(f"開始辨識影片 {VIDEO_ID}，請稍候...")
work_dir = ROOT / "output" / "temp_transcription"
segments, language = transcribe_local_media(
    media_path=VIDEO_PATH,
    work_dir=work_dir,
    model_name=MODEL_NAME,
)

video_dir = ROOT / "output" / "videos" / record_id
video_dir.mkdir(parents=True, exist_ok=True)
video_md = video_dir / "video.md"

# Preserve all existing video.md content and replace only the managed extra-Whisper section.
if video_md.is_file():
    content = video_md.read_text(encoding="utf-8-sig").rstrip()
else:
    content = f"# {record_id}\n"

start = content.find(EXTRA_START)
end = content.find(EXTRA_END)
if start >= 0 and end >= 0 and end > start:
    content = (content[:start].rstrip() + content[end + len(EXTRA_END):]).rstrip()

transcript_lines = [
    "### 額外 Whisper 逐字稿（獨立轉錄）",
    EXTRA_START,
    f"- Video ID：{VIDEO_ID}",
    f"- 語言：{language}",
    f"- 辨識方式：faster-whisper {MODEL_NAME}",
    f"- 文字片段數量：{len(segments)}",
    "",
    "#### 字幕逐字稿",
    "",
]
for segment in segments:
    start_time = segment["timestamp"]
    end_seconds = int(segment["end_seconds"])
    end_time = (
        f"{end_seconds // 3600:02d}:"
        f"{(end_seconds % 3600) // 60:02d}:"
        f"{end_seconds % 60:02d}"
    )
    transcript_lines.append(f"[{start_time} - {end_time}] {segment['text']}")
    transcript_lines.append("")
transcript_lines.append(EXTRA_END)

# Append exactly one managed section; postprocess.py also preserves this section on rebuild.
new_content = content + "\n\n" + "\n".join(transcript_lines) + "\n"
video_md.write_text(new_content, encoding="utf-8")

print("辨識完成！")
print(f"文字片段：{len(segments)}")
print(f"逐字稿已直接整合至：{video_md}")
