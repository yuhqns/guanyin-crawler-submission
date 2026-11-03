
from pathlib import Path
from crawler.transcribe import transcribe_local_media

ROOT = Path(__file__).resolve().parent.parent
# 第三部影片
video_path = ROOT / "local_videos" / "AWP_v5I0zYA.mp4"

# 暫存音訊
work_dir = ROOT / "output" / "temp_transcription"

# 執行中文語音辨識
print("開始辨識第三部影片，請稍候...")

segments, language = transcribe_local_media(
    media_path=video_path,
    work_dir=work_dir,
    model_name="small"
)

# 建立逐字稿
output_path = ROOT / "output" / "videos" / "transcripts" / "AWP_v5I0zYA.md"
output_path.parent.mkdir(parents=True, exist_ok=True)

with output_path.open("w", encoding="utf-8") as f:
    f.write("# 歷史那一刻：石門水庫遷村故事\n\n")
    f.write(f"- Video ID：AWP_v5I0zYA\n")
    f.write(f"- 語言：{language}\n")
    f.write("- 辨識方式：faster-whisper small\n")
    f.write(f"- 文字片段數量：{len(segments)}\n\n")
    f.write("## 字幕逐字稿\n\n")

    for segment in segments:
        start = segment["timestamp"]
        end_seconds = int(segment["end_seconds"])
        end = (
            f"{end_seconds // 3600:02d}:"
            f"{(end_seconds % 3600) // 60:02d}:"
            f"{end_seconds % 60:02d}"
        )
        f.write(f"[{start} - {end}] {segment['text']}\n\n")

print("辨識完成！")
print(f"文字片段：{len(segments)}")
print(f"輸出位置：{output_path}")
