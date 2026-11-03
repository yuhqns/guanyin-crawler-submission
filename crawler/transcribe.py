"""Optional local-audio transcription for videos without readable captions.

The crawler never imports faster-whisper until transcription is explicitly
requested.  This keeps the normal metadata/caption crawl lightweight and
avoids downloading a model as part of a regular run.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def _timestamp(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"


def transcribe_local_media(
    media_path: Path,
    work_dir: Path,
    model_name: str = "large-v3",
) -> tuple[list[dict[str, Any]], str]:
    """Convert *media_path* to 16 kHz mono WAV and transcribe it with Whisper.

    ``ffmpeg`` and ``faster-whisper`` are intentionally runtime dependencies:
    callers only need them when they opt into this fallback.  The returned
    segment shape matches the crawler's subtitle segments so downstream output
    can render both sources consistently.
    """
    if not media_path.is_file():
        raise FileNotFoundError(f"找不到影音檔：{media_path}")
    work_dir.mkdir(parents=True, exist_ok=True)
    wav_path = work_dir / f"{media_path.stem}.16k-mono.wav"
    command = [
        "ffmpeg", "-y", "-i", str(media_path), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(wav_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError("找不到 ffmpeg；請先安裝 ffmpeg，或不要啟用語音轉錄") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()[-1:]
        raise RuntimeError(f"ffmpeg 轉換失敗：{' '.join(detail)}") from exc

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("缺少 faster-whisper；請安裝 requirements-transcription.txt") from exc

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    # Keep the prompt short and domain-specific. A long prompt can bias the
    # decoder toward repeatedly emitting prompt-related words.
    prompt = "桃園市、觀音區、石門水庫、草漯、移民新村。"
    segments, info = model.transcribe(
        str(wav_path),
        language="zh",
        beam_size=5,
        best_of=5,
        temperature=[0.0, 0.2, 0.4, 0.6, 0.8],
        compression_ratio_threshold=2.4,
        log_prob_threshold=-1.0,
        no_speech_threshold=0.6,
        condition_on_previous_text=False,
        repetition_penalty=1.12,
        no_repeat_ngram_size=3,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 500,
            "speech_pad_ms": 200,
        },
        initial_prompt=prompt,
        word_timestamps=False,
    )
    result = []
    for segment in segments:
        text = str(segment.text or "").strip()
        if not text:
            continue
        start = float(segment.start)
        result.append({
            "start_seconds": start,
            "end_seconds": float(segment.end),
            "timestamp": _timestamp(start),
            "text": text,
        })
    return result, str(getattr(info, "language", "zh") or "zh")
