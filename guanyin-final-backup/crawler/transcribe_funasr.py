"""沒有字幕的影片：用 FunASR 的兩個模型各轉一遍語音，找出兩邊不同的地方，再用畫面字幕驗證。

流程：
1. 用 fsmn-vad 把音檔切成一段一段（每段不超過 12 秒）。兩個模型吃同樣的段落，所以結果天然對得上。
2. 每一段各給 Paraformer-zh（有標點）和 SenseVoice-Small 辨識，兩邊的輸出都從簡體轉成繁體（OpenCC s2tw）。
3. 逐段比對，兩個模型不一樣的地方就是「差異處」。數字寫法（二十九／29）和語尾助詞不算差異。
4. 差異處拿去和畫面字幕比（<id>_ocr.md，由 crawler/subtitle_ocr.py 產生）：
   哪一邊和畫面上的字比較像就採用哪一邊；畫面沒有字幕、或兩邊都對不上，就留下 Paraformer 的寫法並標「待確認」。
5. 輸出 <id>_funasr.md，另存 <id>_funasr_raw.json（兩個模型的原始輸出與每個差異的判定，方便之後重新分析）。

這支程式要在裝了 FunASR 的獨立環境執行（不是專案本來的 Python）：
    ~/.venvs/funasr/bin/python -m crawler.transcribe_funasr --video-id AWP_v5I0zYA --audio path/to/audio.wav

只跑一小段測試：加 --from-seconds 86 --seconds 90 --out /tmp/test_funasr.md
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import math
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

from .transcribe_api import ROOT, _lookup_record, _read_meta, _timestamp

MODEL_HUB = "hf"              # 魔搭（ModelScope）在這裡實測很慢，HuggingFace 快很多
PARAFORMER = "paraformer-zh"
SENSEVOICE = "FunAudioLLM/SenseVoiceSmall"
SAMPLE_RATE = 16000
MAX_CHUNK = 12.0              # 每一段最長幾秒
MAX_GAP = 0.8                 # 兩小段之間停頓不超過這麼多秒就併成一段
OCR_MAX_SECONDS = 6.0         # 畫面字幕只知道開始時間，最多當它顯示這麼久
OCR_WINDOW_PAD = 1.5          # 找對應的畫面字幕時，前後多看幾秒
CONTEXT_TOKENS = 3            # 比對畫面時，差異處前後各帶幾個字當線索
MIN_MATCH = 0.6               # 和畫面字幕相似度低於這個，視為「兩邊都對不上」
TIE_MARGIN = 0.05             # 兩邊相似度差距小於這個，視為「畫面分不出來」
PARTICLES = "啊啦嘛喔呢吧呀哦哎"  # 語尾助詞，比對時忽略
NUMERALS = "零〇一二兩三四五六七八九十百千"
TOKEN_RE = re.compile(rf"[{NUMERALS}]+|\d+|[^\W_]")
TAG_RE = re.compile(r"<\|[^|>]*\|>")
OCR_LINE = re.compile(r"- \*\*\[(\d+):(\d+):(\d+)\]\*\* (.*)")
DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
UNITS = {"十": 10, "百": 100, "千": 1000}

VERDICT_TEXT = {"para": "畫面支持 Paraformer", "sense": "畫面支持 SenseVoice",
                "neither": "兩邊都對不上畫面", "tie": "畫面分不出來", "none": "這段畫面沒有字幕"}
UNRESOLVED = {"neither", "tie", "none"}

DEFAULT_GLOSSARY = ROOT / "data" / "glossary.md"
GLOSSARY_USED = {"已確認", "畫面證實"}   # 這兩種狀態的詞才會套用；「待確認」只列出
MIN_ALIAS = 2                           # 「常被聽成」的寫法至少幾個字才套用，太短會誤換到正常句子


def find_record_id(video_id: str) -> str:
    """Folder name of this video. Falls back to matching the URL, because records.jsonl
    (after the 2026-11-17 re-crawl) no longer carries a video_id field for every record."""
    record_id = _lookup_record(video_id)
    if record_id != video_id:
        return record_id
    records = ROOT / "data" / "records.jsonl"
    if records.is_file():
        for line in records.open(encoding="utf-8"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if video_id in f"{record.get('final_url') or ''} {record.get('source_url') or ''}":
                return str(record.get("id") or video_id)
    return video_id


def _run(command: list[str], what: str) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{what}失敗：找不到 {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-3:]
        raise RuntimeError(f"{what}失敗：{' / '.join(detail)}") from exc


def prepare_audio(source: Path, work_dir: Path, start: float, length: float | None) -> Path:
    """Convert to 16 kHz mono wav (optionally just one section) in the temporary folder."""
    target = work_dir / "audio.wav"
    command = ["ffmpeg", "-v", "error", "-y"]
    if start:
        command += ["-ss", str(start)]
    if length:
        command += ["-t", str(length)]
    command += ["-i", str(source), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(target)]
    _run(command, "轉換音檔（需要 ffmpeg）")
    return target


def read_wav(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != SAMPLE_RATE or handle.getnchannels() != 1:
            raise RuntimeError("音檔必須是 16 kHz 單聲道")
        frames = handle.readframes(handle.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype("float32") / 32768.0


def read_glossary(path: Path) -> list[dict[str, Any]]:
    """Rows of the glossary table (data/glossary.md): term, wrong spellings heard, category, status, source."""
    entries: list[dict[str, Any]] = []
    if not path.is_file():
        return entries
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] == "正確寫法" or set(cells[0]) <= set("-: "):
            continue
        entries.append({"term": cells[0], "aliases": [a.strip() for a in re.split(r"[、,，]", cells[1]) if a.strip()],
                        "category": cells[2], "status": cells[3], "source": cells[4]})
    return entries


def usable_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry["status"] in GLOSSARY_USED]


def apply_aliases(text: str, entries: list[dict[str, Any]]) -> tuple[str, list[tuple[str, str]]]:
    """Replace spellings the glossary knows to be mis-hearings (longest first). Returns the text and what changed."""
    pairs = sorted(((alias, entry["term"]) for entry in usable_entries(entries)
                    for alias in entry["aliases"] if len(alias) >= MIN_ALIAS), key=lambda pair: -len(pair[0]))
    fixes: list[tuple[str, str]] = []
    for alias, term in pairs:
        if alias in text:
            fixes += [(alias, term)] * text.count(alias)
            text = text.replace(alias, term)
    return text, fixes


def apply_glossary(chunks: list[dict[str, Any]], entries: list[dict[str, Any]]) -> int:
    """Correct both models' text with the glossary. Keeps the uncorrected text in <model>_before."""
    total = 0
    for chunk in chunks:
        chunk["fixes"] = []
        for key in ("para", "sense"):
            fixed, fixes = apply_aliases(chunk[key], entries)
            if fixes:
                chunk[key + "_before"] = chunk[key]
                chunk[key] = fixed
                chunk["fixes"] += [{"model": key, "heard": heard, "term": term} for heard, term in fixes]
                total += len(fixes)
    return total


def load_models(device: str, para_model: str = PARAFORMER) -> dict[str, Any]:
    try:
        from funasr import AutoModel
        from opencc import OpenCC
    except ImportError as exc:
        raise RuntimeError("找不到 funasr 或 opencc：請用裝了 FunASR 的環境執行（~/.venvs/funasr/bin/python）") from exc
    logging.disable(logging.INFO)
    common = {"device": device, "hub": MODEL_HUB, "disable_update": True}
    para = AutoModel(model=para_model, punc_model="ct-punc", **common)
    return {
        "vad": AutoModel(model="fsmn-vad", **common),
        "para": para,
        "sense": AutoModel(model=SENSEVOICE, **common),
        "opencc": OpenCC("s2tw"),
        "to_simplified": OpenCC("tw2s").convert,     # Paraformer 的字表是簡體，熱詞要先轉簡體
        "hotword_capable": "seaco" in type(para.model).__name__.lower(),
    }


def detect_segments(vad: Any, audio) -> list[tuple[float, float]]:
    result = vad.generate(input=audio, disable_pbar=True)
    spans = result[0]["value"] if result and result[0].get("value") else []
    return [(begin / 1000.0, end / 1000.0) for begin, end in spans if end > begin]


def build_chunks(segments: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Join neighbouring speech segments into chunks of at most MAX_CHUNK seconds."""
    pieces: list[tuple[float, float]] = []
    for begin, end in segments:
        count = max(1, math.ceil((end - begin) / MAX_CHUNK))
        step = (end - begin) / count
        pieces += [(begin + k * step, begin + (k + 1) * step) for k in range(count)]
    chunks: list[list[float]] = []
    for begin, end in pieces:
        if chunks and begin - chunks[-1][1] <= MAX_GAP and end - chunks[-1][0] <= MAX_CHUNK:
            chunks[-1][1] = end
        else:
            chunks.append([begin, end])
    return [(begin, end) for begin, end in chunks]


def _first_text(result: Any) -> str:
    if result and isinstance(result, list) and isinstance(result[0], dict):
        return str(result[0].get("text") or "")
    return ""


def recognise(models: dict[str, Any], audio, chunks: list[tuple[float, float]],
              hotwords: str = "") -> list[dict[str, Any]]:
    out = []
    extra = {"hotword": hotwords} if hotwords and models["hotword_capable"] else {}
    for index, (begin, end) in enumerate(chunks, 1):
        clip = audio[int(begin * SAMPLE_RATE):int(end * SAMPLE_RATE) + 1]
        para = _first_text(models["para"].generate(input=clip, disable_pbar=True, **extra))
        sense = TAG_RE.sub("", _first_text(models["sense"].generate(
            input=clip, language="zh", use_itn=True, disable_pbar=True))).strip()
        convert = models["opencc"].convert
        out.append({"start": begin, "end": end, "para_raw": para, "sense_raw": sense,
                    "para": convert(para).strip(), "sense": convert(sense).strip()})
        if index % 10 == 0:
            print(f"  已辨識 {index}/{len(chunks)} 段")
    return out


def _cn_to_arabic(run: str) -> str:
    if all(ch in DIGITS for ch in run):
        return "".join(str(DIGITS[ch]) for ch in run)
    total = current = 0
    for ch in run:
        if ch in DIGITS:
            current = DIGITS[ch]
        else:
            total += (current or 1) * UNITS[ch]
            current = 0
    return str(total + current)


def tokens(text: str) -> list[tuple[str, int, int]]:
    """(comparable value, start, end) per character; number words become one Arabic-digit token."""
    out = []
    for match in TOKEN_RE.finditer(text):
        piece = match.group(0)
        if piece[0] in NUMERALS:
            value = _cn_to_arabic(piece)
        elif piece in PARTICLES:
            continue
        else:
            value = piece.lower()
        out.append((value, match.start(), match.end()))
    return out


def find_differences(a_text: str, b_text: str) -> list[dict[str, Any]]:
    """Places where the two texts differ, as spans of the original strings."""
    ta, tb = tokens(a_text), tokens(b_text)
    matcher = difflib.SequenceMatcher(None, [t[0] for t in ta], [t[0] for t in tb], autojunk=False)
    ops: list[list[int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if ops and i1 - ops[-1][1] <= 1 and j1 - ops[-1][3] <= 1:   # 中間只隔一個字的差異併成一個
            ops[-1][1], ops[-1][3] = i2, j2
        else:
            ops.append([i1, i2, j1, j2])
    found = []
    for i1, i2, j1, j2 in ops:
        a_span = (ta[i1][1], ta[i2 - 1][2]) if i1 < i2 else ((ta[i1][1] if i1 < len(ta) else len(a_text)),) * 2
        b_span = (tb[j1][1], tb[j2 - 1][2]) if j1 < j2 else ((tb[j1][1] if j1 < len(tb) else len(b_text)),) * 2
        left = [t[0] for t in ta[max(0, i1 - CONTEXT_TOKENS):i1]]
        right = [t[0] for t in ta[i2:i2 + CONTEXT_TOKENS]]
        found.append({
            "a_span": a_span, "b_span": b_span,
            "a": a_text[a_span[0]:a_span[1]], "b": b_text[b_span[0]:b_span[1]],
            "cand_a": left + [t[0] for t in ta[i1:i2]] + right,
            "cand_b": left + [t[0] for t in tb[j1:j2]] + right,
        })
    return found


def read_ocr(path: Path) -> list[dict[str, Any]]:
    """On-screen subtitles from <id>_ocr.md (main section only; lines the OCR itself marks 待確認 are left out,
    as are lines from old OCR files that were filled in from speech)."""
    if not path.is_file():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if raw.startswith("### "):
            break
        raw = re.sub(r"\s*｜樣式\d+\s*$", "", raw)       # 另一種字幕樣式的句子：去掉樣式標記，一樣當畫面字幕用
        match = OCR_LINE.match(raw)
        if match and not raw.rstrip().endswith(("（語音候選）", "（語音補）", "（待確認）")):
            hours, minutes, seconds, text = match.groups()
            lines.append({"start": int(hours) * 3600 + int(minutes) * 60 + int(seconds), "text": text.strip()})
    for position, line in enumerate(lines):
        following = lines[position + 1]["start"] if position + 1 < len(lines) else line["start"] + OCR_MAX_SECONDS
        line["end"] = min(following, line["start"] + OCR_MAX_SECONDS)
    return lines


def _best_match(candidate: list[str], screen: list[str]) -> tuple[float, int, int]:
    """Best similarity of the candidate to any stretch of the screen text: (ratio, first token, last token + 1)."""
    if not candidate or not screen:
        return 0.0, 0, 0
    best, where, size = 0.0, (0, 0), len(candidate)
    for width in range(max(1, size - 1), size + 2):
        for begin in range(max(1, len(screen) - width + 1)):
            ratio = difflib.SequenceMatcher(None, candidate, screen[begin:begin + width], autojunk=False).ratio()
            if ratio > best:
                best, where = ratio, (begin, min(begin + width, len(screen)))
    return best, where[0], where[1]


def verify_with_screen(differences: list[dict[str, Any]], begin: float, end: float,
                       ocr: list[dict[str, Any]]) -> str:
    """Judge each difference against the subtitles on screen during this chunk. Returns the screen text used."""
    near = [line for line in ocr if line["start"] < end + OCR_WINDOW_PAD and line["end"] > begin - OCR_WINDOW_PAD]
    screen_text = " ".join(line["text"] for line in near)
    screen_tokens = tokens(screen_text)
    screen = [t[0] for t in screen_tokens]
    for diff in differences:
        if not screen:
            diff["verdict"] = "none"
            continue
        score_a, first_a, last_a = _best_match(diff["cand_a"], screen)
        score_b, first_b, last_b = _best_match(diff["cand_b"], screen)
        diff["score_a"], diff["score_b"] = round(score_a, 2), round(score_b, 2)
        if max(score_a, score_b) < MIN_MATCH:
            diff["verdict"] = "neither"
        elif abs(score_a - score_b) < TIE_MARGIN:
            diff["verdict"] = "tie"
        else:
            diff["verdict"] = "para" if score_a > score_b else "sense"
        # 畫面上和比較像的那個寫法對應的字（原樣取回，方便人直接看）
        first, last = (first_a, last_a) if score_a >= score_b else (first_b, last_b)
        diff["screen_text"] = screen_text[screen_tokens[first][1]:screen_tokens[last - 1][2]] if last > first else ""
        diff["exact"] = max(score_a, score_b) >= 0.999
    return screen_text


def apply_choices(base: str, differences: list[dict[str, Any]]) -> str:
    """Start from Paraformer's text (it has punctuation); swap in SenseVoice where the screen backs it."""
    text = base
    for diff in sorted(differences, key=lambda d: d["a_span"][0], reverse=True):
        if diff.get("verdict") == "sense":
            text = text[:diff["a_span"][0]] + diff["b"] + text[diff["a_span"][1]:]
    return text


def analyse(chunks: list[dict[str, Any]], ocr: list[dict[str, Any]], offset: float) -> None:
    for chunk in chunks:
        chunk["differences"] = find_differences(chunk["para"], chunk["sense"])
        chunk["screen"] = ""
        if chunk["differences"]:
            if ocr:
                chunk["screen"] = verify_with_screen(
                    chunk["differences"], chunk["start"] + offset, chunk["end"] + offset, ocr)
            else:
                for diff in chunk["differences"]:
                    diff["verdict"] = "none"
        chunk["final"] = apply_choices(chunk["para"], chunk["differences"])
        # 沒決定，或決定了但畫面字幕和採用的寫法不完全一樣（可能兩邊都錯），都要人看
        chunk["unresolved"] = any(d["verdict"] in UNRESOLVED or not d.get("exact") for d in chunk["differences"])


def render_markdown(title: str, video_id: str, url: str, chunks: list[dict[str, Any]], audio_name: str,
                    ocr_name: str, offset: float, notes: list[str]) -> str:
    def stamp(seconds: float) -> str:
        return _timestamp(seconds + offset)

    diffs = [(c, d) for c in chunks for d in c["differences"]]
    count = {key: sum(1 for _, d in diffs if d["verdict"] == key) for key in VERDICT_TEXT}
    differing_chunks = sum(1 for c in chunks if c["differences"])
    out = [f"# {title}", "",
           f"- YouTube Video ID：{video_id}",
           f"- 原始網址：{url}",
           "- 辨識方式：FunASR 兩個模型（Paraformer-zh、SenseVoice-Small），本機 CPU 執行",
           "- 簡轉繁：OpenCC s2tw（只換字形，不換詞彙）",
           *[f"- {note}" for note in notes],
           f"- 時間點來源：語音切段的起點（每段不超過 {int(MAX_CHUNK)} 秒）",
           f"- 音訊檔：{audio_name}",
           f"- 畫面字幕驗證：{'已做（' + ocr_name + '）' if ocr_name else '未做（找不到畫面字幕檔）'}",
           f"- 語音切段：{len(chunks)} 段；兩個模型不同的段落：{differing_chunks} 段，差異處共 {len(diffs)} 個",
           "- 差異處判定：" + "；".join(f"{VERDICT_TEXT[k]} {count[k]}" for k in VERDICT_TEXT),
           f"- 其中畫面字幕和採用的寫法完全一樣：{sum(1 for _, d in diffs if d['verdict'] in ('para', 'sense') and d.get('exact'))} 個；"
           f"需要人確認：{sum(1 for _, d in diffs if d['verdict'] in UNRESOLVED or not d.get('exact'))} 個"
           f"（已涵蓋沒決定的，以及決定了但畫面不完全一樣的）",
           "", "## 字幕與逐字稿", ""]
    for chunk in chunks:
        if not chunk["final"].strip():
            continue
        mark = "（待確認）" if chunk["unresolved"] else ""
        out.append(f"- **[{stamp(chunk['start'])}]** {chunk['final']}{mark}")
    def line(chunk: dict[str, Any], diff: dict[str, Any], decided: bool) -> str:
        text = f"- **[{stamp(chunk['start'])}]** Paraformer：{diff['a'] or '（無）'} ｜ SenseVoice：{diff['b'] or '（無）'}"
        if diff["verdict"] == "none":
            return text + " ｜ 這段畫面沒有字幕，沒有東西可驗證"
        text += f"（相似度 {diff['score_a']} 比 {diff['score_b']}）"
        if decided:
            text += f" → 採用 {'Paraformer' if diff['verdict'] == 'para' else 'SenseVoice'}"
        else:
            text += f" ｜ {VERDICT_TEXT[diff['verdict']]}"
        return text + f" ｜ 畫面字幕寫的是：{diff.get('screen_text') or '（找不到對應處）'}"

    decided = [(c, d) for c, d in diffs if d["verdict"] in ("para", "sense")]
    exact = [(c, d) for c, d in decided if d.get("exact")]
    partial = [(c, d) for c, d in decided if not d.get("exact")]
    undecided = [(c, d) for c, d in diffs if d["verdict"] in UNRESOLVED]
    if undecided:
        out += ["", "### 兩個模型不同、畫面也無法決定的地方（請人確認）", "",
                "正文暫時用 Paraformer 的寫法。「這段畫面沒有字幕」代表沒有東西可以驗證，不代表 Paraformer 是對的。", ""]
        out += [line(c, d, False) for c, d in undecided]
    if partial:
        out += ["", "### 畫面選了其中一邊，但畫面字幕和它不完全一樣（兩邊都可能錯，請人確認）", "",
                "「相似度」是這個寫法（連同前後 3 個字）和畫面字幕最像的程度，1 代表完全一樣。"
                "畫面字幕本身偶爾也會讀錯，所以看到的差異不一定是模型錯。", ""]
        out += [line(c, d, True) for c, d in partial]
    if exact:
        out += ["", "### 畫面字幕和其中一邊完全一樣（可以放心採用）", ""]
        out += [line(c, d, True) for c, d in exact]
    fixes = [(c, f) for c in chunks for f in c.get("fixes", [])]
    if fixes:
        out += ["", "### 詞庫自動更正的地方", "",
                "這些寫法在詞庫裡被記錄為「常被聽成」，已換成正確寫法（更正發生在比對兩個模型之前）。"
                "若換錯，請到詞庫把該列「常被聽成」的寫法刪掉。", ""]
        out += [f"- **[{stamp(c['start'])}]** {'Paraformer' if f['model'] == 'para' else 'SenseVoice'}："
                f"{f['heard']} → {f['term']}" for c, f in fixes]
    return "\n".join(out) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="用 FunASR 兩個模型轉語音，差異處用畫面字幕驗證")
    parser.add_argument("--video-id", required=True, help="YouTube Video ID，例如 AWP_v5I0zYA")
    parser.add_argument("--audio", required=True, type=Path, help="音檔或影片檔路徑")
    parser.add_argument("--ocr", type=Path, help="畫面字幕檔（預設 output/videos/<id>/<id>_ocr.md）")
    parser.add_argument("--out", type=Path, help="輸出 markdown 路徑（預設 output/videos/<id>/<id>_funasr.md）")
    parser.add_argument("--glossary", type=Path, default=DEFAULT_GLOSSARY,
                        help="專有名詞詞庫（預設 data/glossary.md；存在就會套用）")
    parser.add_argument("--no-glossary", action="store_true", help="不套用詞庫（第一次跑、當作對照用）")
    parser.add_argument("--para-model", default=PARAFORMER,
                        help="Paraformer 的模型名稱或本機資料夾；要用熱詞需指定支援熱詞的 SeACo 版本")
    parser.add_argument("--device", default="cpu", help="cpu（預設）或 mps；實測 Paraformer 在 mps 會出錯")
    parser.add_argument("--from-seconds", type=float, default=0.0, help="只處理從第幾秒開始的一段（測試用）")
    parser.add_argument("--seconds", type=float, help="只處理這麼多秒（測試用）")
    args = parser.parse_args()

    if not args.audio.is_file():
        print(f"錯誤：找不到音檔：{args.audio}", file=sys.stderr)
        return 1
    record_id = find_record_id(args.video_id)
    title, url = _read_meta(record_id)
    folder = ROOT / "output" / "videos" / record_id
    ocr_path = args.ocr or folder / f"{record_id}_ocr.md"
    entries = [] if args.no_glossary else read_glossary(args.glossary)
    used = usable_entries(entries)
    out_path = args.out or folder / (f"{record_id}_funasr_glossary.md" if used else f"{record_id}_funasr.md")
    raw_path = out_path.with_name(out_path.stem + "_raw.json")

    try:
        with tempfile.TemporaryDirectory(prefix="guanyin_funasr_") as temp:
            wav = prepare_audio(args.audio, Path(temp), args.from_seconds, args.seconds)
            audio = read_wav(wav)
        print("載入模型（第一次會下載約 3 GB）…")
        models = load_models(args.device, args.para_model)
        started = time.time()
        chunks_span = build_chunks(detect_segments(models["vad"], audio))
        print(f"語音切成 {len(chunks_span)} 段，開始辨識…")
        hotwords = " ".join(models["to_simplified"](entry["term"]) for entry in used)
        chunks = recognise(models, audio, chunks_span, hotwords)
        corrected = apply_glossary(chunks, entries) if used else 0
    except RuntimeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    ocr = read_ocr(ocr_path)
    analyse(chunks, ocr, args.from_seconds)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not used:
        notes = ["詞庫：未套用"]
    else:
        pending = sum(1 for entry in entries if entry["status"] not in GLOSSARY_USED)
        notes = [f"詞庫：{args.glossary.name}，共 {len(entries)} 個詞，套用 {len(used)} 個（待確認 {pending} 個不套用）；"
                 f"自動更正 {corrected} 處",
                 "熱詞：" + ("已帶入 Paraformer" if models["hotword_capable"]
                           else "未使用（目前的 Paraformer 版本不支援熱詞，只做「常被聽成」寫法的更正）")]
    out_path.write_text(render_markdown(title, args.video_id, url or "", chunks, args.audio.name,
                                        ocr_path.name if ocr else "", args.from_seconds, notes), encoding="utf-8")
    raw_path.write_text(json.dumps({"video_id": args.video_id, "offset_seconds": args.from_seconds,
                                    "models": {"paraformer": args.para_model, "sensevoice": SENSEVOICE},
                                    "glossary": {"file": str(args.glossary), "terms_used": len(used)},
                                    "chunks": chunks}, ensure_ascii=False, indent=1), encoding="utf-8")
    differing = sum(1 for chunk in chunks if chunk["differences"])
    print(f"完成：{len(chunks)} 段，兩模型不同 {differing} 段，耗時 {time.time() - started:.0f} 秒 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
