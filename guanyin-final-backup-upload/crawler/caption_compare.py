"""自動字幕 vs 沒字幕路線（FunASR）逐段比較。

給「只有 YouTube 自動字幕」的影片用：兩邊都是機器辨識，這支工具把它們放在同一個時間軸上，
列出不一樣的地方。兩邊不一樣不代表誰對誰錯，只代表這裡值得再看一眼（最後仍以畫面字幕與人工確認為準）。

用法（在專案根目錄；兩個輸入檔先用下面兩個步驟產生）：
    python -m crawler.subtitle_highlights fetch --id <編號> --auto        # 產生 <id>_auto_captions.json
    python -m crawler.transcribe_funasr ...                              # 產生 <id>_funasr_glossary_raw.json
    python -m crawler.caption_compare --id <編號>
→ output/videos/<編號>/<編號>_auto_vs_asr.md

比較前會去掉標點與空白；如果裝了 opencc 會先統一成簡體（避免繁簡不同被當成差異）。
沒有 opencc 時照原樣比較，並在報告裡註明。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path
from typing import Any

from .transcribe_api import ROOT, _read_meta, _timestamp

AGREE_RATIO = 0.85     # 相似度達到這個值就算「一致」
WINDOW_PAD = 0.5       # 比對一段語音時，前後多容納幾秒的自動字幕
MAX_SPANS = 6          # 每一段最多列幾個差異處


def _converter():
    try:
        from opencc import OpenCC
        return OpenCC("t2s").convert
    except Exception:
        return None


def normalize(text: str, convert=None) -> str:
    """去掉標點、空白，統一大小寫；有轉換函式時順便繁轉簡。"""
    if convert:
        text = convert(text)
    return re.sub(r"[\W_]+", "", text).lower()


def auto_text_for(cues: list[dict[str, Any]], start: float, end: float, pad: float = WINDOW_PAD) -> str:
    """時間落在 [start-pad, end+pad] 的自動字幕（以每句的中點判斷），依序接起來。"""
    picked = [c["text"] for c in cues if start - pad <= (c["start"] + c["end"]) / 2 <= end + pad]
    return " ".join(picked)


def diff_spans(a: str, b: str, limit: int = MAX_SPANS) -> list[tuple[str, str]]:
    """兩段（已正規化的）文字不一樣的地方：[(a 的寫法, b 的寫法)]。"""
    spans = []
    for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            spans.append((a[a1:a2], b[b1:b2]))
    return spans[:limit]


def compare(chunks: list[dict[str, Any]], cues: list[dict[str, Any]], convert=None,
            agree_ratio: float = AGREE_RATIO) -> list[dict[str, Any]]:
    rows = []
    for chunk in chunks:
        asr_text = chunk.get("final") or chunk.get("para") or ""
        auto_text = auto_text_for(cues, chunk["start"], chunk["end"])
        a, b = normalize(asr_text, convert), normalize(auto_text, convert)
        if not b:
            flag, ratio = "auto_missing", 0.0
        elif not a:
            flag, ratio = "asr_missing", 0.0
        else:
            ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
            flag = "agree" if ratio >= agree_ratio else "differ"
        rows.append({"start": chunk["start"], "end": chunk["end"], "asr": asr_text, "auto": auto_text,
                     "ratio": round(ratio, 3), "flag": flag,
                     "spans": diff_spans(a, b) if flag == "differ" else []})
    return rows


def render_markdown(title: str, video_id: str, language: str, rows: list[dict[str, Any]], converted: bool) -> str:
    counts = {k: sum(1 for r in rows if r["flag"] == k) for k in ("agree", "differ", "auto_missing", "asr_missing")}
    mean = sum(r["ratio"] for r in rows if r["flag"] in ("agree", "differ")) / max(1, counts["agree"] + counts["differ"])
    lines = [f"# {title}｜自動字幕與 FunASR 比較", "",
             f"- YouTube Video ID：{video_id}",
             f"- 自動字幕語言：{language}",
             f"- 語音段數：{len(rows)}；一致 {counts['agree']}、不一樣 {counts['differ']}、"
             f"自動字幕沒有對應 {counts['auto_missing']}、FunASR 沒有文字 {counts['asr_missing']}",
             f"- 有對照的段落平均相似度：{mean:.2f}（相似度 ≥ {AGREE_RATIO} 算一致）",
             f"- 繁簡統一：{'已用 opencc 轉成簡體再比' if converted else '沒有安裝 opencc，照原樣比（繁簡不同可能被當成差異）'}",
             "- 注意：兩邊都是機器辨識，不一樣只代表值得再看，不代表哪一邊錯。「FunASR」欄是沒字幕路線的最終判定。", ""]
    shown = [r for r in rows if r["flag"] != "agree"]
    if not shown:
        return "\n".join(lines + ["沒有不一樣的段落。", ""])
    lines += ["## 不一樣的段落", ""]
    for r in shown:
        label = {"differ": f"相似度 {r['ratio']:.2f}", "auto_missing": "自動字幕沒有這一段",
                 "asr_missing": "FunASR 沒有文字"}[r["flag"]]
        lines += [f"### [{_timestamp(r['start'])} – {_timestamp(r['end'])}]　{label}", "",
                  f"- FunASR：{r['asr'] or '（無）'}", f"- 自動字幕：{r['auto'] or '（無）'}"]
        for mine, theirs in r["spans"]:
            lines.append(f"  - 差異：FunASR「{mine or '（無）'}」／自動字幕「{theirs or '（無）'}」")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="比較 YouTube 自動字幕與 FunASR 辨識結果。")
    ap.add_argument("--id", required=True, help="爬蟲編號（或影片 ID），例如 page031")
    ap.add_argument("--raw", type=Path, help="FunASR 的 raw.json；預設找 <id>_funasr_glossary_raw.json 再找 <id>_funasr_raw.json")
    ap.add_argument("--auto", type=Path, help="自動字幕檔；預設 <id>_auto_captions.json")
    ap.add_argument("--out", type=Path, help="輸出路徑；預設 <id>_auto_vs_asr.md")
    ap.add_argument("--agree-ratio", type=float, default=AGREE_RATIO)
    args = ap.parse_args()

    folder = ROOT / "output" / "videos" / args.id
    raw_path = args.raw or next((p for p in (folder / f"{args.id}_funasr_glossary_raw.json",
                                             folder / f"{args.id}_funasr_raw.json") if p.is_file()), None)
    auto_path = args.auto or folder / f"{args.id}_auto_captions.json"
    if not raw_path or not Path(raw_path).is_file():
        raise SystemExit(f"找不到 FunASR 結果（{folder}/{args.id}_funasr_glossary_raw.json）；請先跑沒字幕路線")
    if not auto_path.is_file():
        raise SystemExit(f"找不到自動字幕檔 {auto_path}；請先執行 subtitle_highlights fetch --id {args.id} --auto")
    raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    auto = json.loads(auto_path.read_text(encoding="utf-8"))
    convert = _converter()
    rows = compare(raw["chunks"], auto["cues"], convert, args.agree_ratio)
    title, _ = _read_meta(args.id)
    out = args.out or folder / f"{args.id}_auto_vs_asr.md"
    out.write_text(render_markdown(title, auto.get("video_id", ""), auto.get("language", ""), rows, bool(convert)),
                   encoding="utf-8")
    differ = sum(1 for r in rows if r["flag"] != "agree")
    print(f"{args.id}：{len(rows)} 段，需要再看 {differ} 段 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
