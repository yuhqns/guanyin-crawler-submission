"""人工確認頁：把「需要人看」的地方做成一個網頁，附字幕截圖，讓人直接點選。

需要人看的地方有兩種：
1. 兩個語音模型（Paraformer、SenseVoice）寫得不一樣，而畫面字幕無法決定（或選了其中一邊但畫面不完全一樣）。
2. 畫面字幕辨識（OCR）自己標了「讀得沒把握」的句子。

每一個地方會顯示：影片當時的字幕截圖、兩個模型各自寫了什麼（不同處標色）、畫面字幕讀到什麼，
讓人選「Paraformer／SenseVoice／畫面字幕／自己輸入／先跳過」。選完按「下載我的選擇」，
再用 apply 把選擇套回逐字稿。

用法（系統的 python3 就能跑，只需要 ffmpeg）：
    python -m crawler.review_page build --video-id AWP_v5I0zYA \\
        --raw output/videos/page030/page030_funasr_glossary_raw.json \\
        --ocr output/videos/page030/page030_ocr.md \\
        --video local_videos/AWP_v5I0zYA.mp4 \\
        --suggestions output/videos/page030/page030_ai_suggestions.json
    python -m crawler.review_page apply --video-id AWP_v5I0zYA \\
        --raw output/videos/page030/page030_funasr_glossary_raw.json \\
        --ocr output/videos/page030/page030_ocr.md \\
        --decisions ~/Downloads/AWP_v5I0zYA_decisions.json \\
        --suggestions output/videos/page030/page030_ai_suggestions.json
採用順序：人工選的 > AI 建議（*_ai_suggestions.json，AI 在對話中逐點用語意判斷後寫的）> 原本的判定。
--decisions 和 --suggestions 都可以省略。

網頁是單一檔案（截圖直接嵌在裡面），在瀏覽器雙擊開啟即可，不用網路。
截圖來自影片，請不要把網頁檔提交到 git（.gitignore 已排除 *_review.html）。
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .transcribe_api import ROOT, _read_meta, _timestamp
from .transcribe_funasr import OCR_WINDOW_PAD, UNRESOLVED, _best_match, find_record_id, read_ocr, tokens

FRAME_WIDTH = 560            # 截圖寬度（像素）
BAND_HEIGHT = 0.30           # 截取畫面底部這麼高的一條（字幕所在）
DIFF_OFFSETS = (-1.0, 0.0, 1.0)   # 語音差異處：預估時間點前後各取一張
OCR_OFFSETS = (0.25, 1.25)        # 畫面字幕沒把握的句子：出現後取兩張
ATTACH_WINDOW = 3.0          # 畫面沒把握的句子離某個語音差異處這麼近（秒），就併進那一張卡
AGREE_MAX_TOKENS = 4         # 語音與畫面字幕的差異超過這麼多字，多半是對不齊而不是真的不同
AGREE_MAX_LENGTH_GAP = 1     # 換字時兩邊字數最多差幾個字
SAME_SOUND_GROUPS = ("的地得", "他她它祂", "臺台", "裡裏", "為爲", "係系")   # 同音或異體字，不算語音聽錯
MIN_SPAN_MATCH = 0.4         # 在模型輸出裡找不到比這個更像的段落，就說附近沒找到
WEAK_LINE = re.compile(r"- \*\*\[(\d+):(\d+):(\d+)\]\*\* (.*?) ｜ 讀到的版本：(.*)")
ROW_PERCENT = re.compile(r"畫面高度約 (\d+)%")
REASON_LABEL = {
    "none": "這段畫面沒有字幕可對照",
    "tie": "畫面分不出來（兩邊一樣像）",
    "neither": "兩邊都對不上畫面字幕",
    "para": "畫面偏向 Paraformer，但不完全一樣",
    "sense": "畫面偏向 SenseVoice，但不完全一樣",
    "agree": "兩個模型寫得一樣，但畫面字幕不一樣",
}


# ---------------------------------------------------------------- 收集要確認的項目

def read_weak_lines(ocr_path: Path) -> tuple[list[dict[str, Any]], float]:
    """OCR 自己標「讀得沒把握」的句子，以及字幕列大約在畫面高度的哪裡（0–1）。"""
    text = ocr_path.read_text(encoding="utf-8-sig")
    percent = ROW_PERCENT.search(text)
    row = int(percent.group(1)) / 100 if percent else 0.88
    weak, inside = [], False
    for line in text.splitlines():
        if line.startswith("### 讀得沒把握"):
            inside = True
            continue
        if inside and line.startswith("### "):
            break
        match = WEAK_LINE.match(line) if inside else None
        if match:
            hours, minutes, seconds, shown, versions = match.groups()
            weak.append({"t": int(hours) * 3600 + int(minutes) * 60 + int(seconds), "text": shown.strip(),
                         "variants": [v.strip() for v in versions.split("／") if v.strip()]})
    return weak, row


def _needs_review(diff: dict[str, Any]) -> bool:
    return diff.get("verdict") in UNRESOLVED or not diff.get("exact")


def _model_span(candidate: list[str], chunks: list[dict[str, Any]], key: str,
                begin: float, end: float) -> tuple[float, str]:
    """在這段時間附近的模型輸出裡，找最像這句畫面字幕的那一段文字。"""
    best, found = 0.0, ""
    for chunk in chunks:
        if chunk["start"] > end or chunk["end"] < begin:
            continue
        parts = tokens(chunk[key])
        ratio, first, last = _best_match(candidate, [p[0] for p in parts])
        if ratio > best and last > first:
            best, found = ratio, chunk[key][parts[first][1]:parts[last - 1][2]]
    return best, found


def _screen_window(ocr: list[dict[str, Any]], begin: float, end: float) -> tuple[str, list[tuple[int, int, float]]]:
    """這一段語音前後畫面上的字幕，接成一個字串；同時記下每一句在字串裡的位置和出現時間。"""
    text, spans = "", []
    for line in ocr:
        if line["start"] < end + OCR_WINDOW_PAD and line["end"] > begin - OCR_WINDOW_PAD:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(line["text"]), float(line["start"])))
            text += line["text"]
    return text, spans


def _token_range(parts: list[tuple[str, int, int]], span: list[int]) -> tuple[int, int]:
    first = next((i for i, t in enumerate(parts) if t[2] > span[0]), len(parts))
    last = next((i for i, t in enumerate(parts) if t[1] >= span[1]), len(parts))
    return first, max(first, last)


def _screen_counterpart(equal: dict[int, int], model_parts: list[tuple[str, int, int]],
                        screen_parts: list[tuple[str, int, int]], screen_text: str, first: int, last: int) -> str | None:
    """模型寫的第 first 到 last 個字，在畫面字幕上對應的是什麼字；前後都對不上錨點就回 None。"""
    before = next((i for i in range(first - 1, -1, -1) if i in equal), None)
    after = next((i for i in range(last, len(model_parts)) if i in equal), None)
    if before is None or after is None:
        return None
    start, stop = equal[before] + 1, equal[after]
    if stop < start or stop - start > AGREE_MAX_TOKENS:
        return None
    if stop == start:
        return ""
    return screen_text[screen_parts[start][1]:screen_parts[stop - 1][2]].strip()


def collect_agree_items(raw: dict[str, Any], ocr: list[dict[str, Any]],
                        attach: dict[int, list[tuple[int, str | None]]] | None = None) -> list[dict[str, Any]]:
    """兩個模型一致、但畫面字幕不一樣的地方；順便幫每個語音差異處找出畫面字幕對應的字。"""
    offset = float(raw.get("offset_seconds", 0.0))
    items: list[dict[str, Any]] = []
    for ci, chunk in enumerate(raw["chunks"]):
        screen_text, spans = _screen_window(ocr, chunk["start"] + offset, chunk["end"] + offset)
        if not screen_text or not chunk["para"].strip():
            continue
        para, sense = tokens(chunk["para"]), tokens(chunk["sense"])
        screen = tokens(screen_text)
        values = [t[0] for t in para]
        codes = difflib.SequenceMatcher(None, values, [t[0] for t in screen], autojunk=False).get_opcodes()
        equal = {i1 + k: j1 + k for tag, i1, i2, j1, j2 in codes if tag == "equal" for k in range(i2 - i1)}
        to_sense = {i1 + k: j1 + k for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, values, [t[0] for t in sense], autojunk=False).get_opcodes() if tag == "equal" for k in range(i2 - i1)}
        if attach is not None:
            for di, diff in enumerate(chunk["differences"]):
                first, last = _token_range(para, diff["a_span"])
                attach.setdefault(ci, []).append(
                    (di, _screen_counterpart(equal, para, screen, screen_text, first, last)))
        diff_spans = [d["a_span"] for d in chunk["differences"]]
        for k, (tag, i1, i2, j1, j2) in enumerate(codes):
            if tag == "equal":
                continue
            before = k > 0 and codes[k - 1][0] == "equal"
            after = k + 1 < len(codes) and codes[k + 1][0] == "equal"
            if not ((tag == "replace" and (before or after)) or (before and after)):
                continue                                  # 在頭尾的單純多字少字，多半只是對齊的邊界
            if i2 - i1 > AGREE_MAX_TOKENS or j2 - j1 > AGREE_MAX_TOKENS:
                continue
            if j1 == j2:
                continue                                  # 畫面字幕比口語少字：字幕是編輯過的，不是語音聽錯
            if i1 < i2 and abs((i2 - i1) - (j2 - j1)) > AGREE_MAX_LENGTH_GAP:
                continue                                  # 換字但兩邊字數差很多，多半是對齊錯位
            if i1 < i2 and any(set(values[i1:i2]) | {t[0] for t in screen[j1:j2]} <= set(group)
                               for group in SAME_SOUND_GROUPS):
                continue
            a_span = [para[i1][1], para[i2 - 1][2]] if i1 < i2 else [para[i1][1] if i1 < len(para) else len(chunk["para"])] * 2
            if any(a_span[0] < d[1] and d[0] < a_span[1] or a_span == d for d in diff_spans):
                continue                                  # 這裡兩個模型本來就不同，已經是語音差異卡
            said = chunk["para"][a_span[0]:a_span[1]]
            shown = screen_text[screen[j1][1]:screen[j2 - 1][2]].strip() if j1 < j2 else ""
            if tag == "delete" and "".join(values[i1:i2]) == "的":
                continue                                  # 畫面字幕漏掉「的」是 Apple Vision 的老毛病
            if i1 < i2:
                b_span = [sense[to_sense[i1]][1], sense[to_sense[i2 - 1]][2]] if i1 in to_sense and i2 - 1 in to_sense else a_span
            else:
                b_span = [sense[to_sense[i1]][1]] * 2 if i1 in to_sense else [len(chunk["sense"])] * 2
            where = screen[j1][1] if j1 < len(screen) else len(screen_text)
            line_at = next(((s[2], screen_text[s[0]:s[1]]) for s in spans if s[0] <= where <= s[1]), (chunk["start"] + offset, ""))
            items.append({
                "id": f"a{ci}-{len(items)}", "kind": "agree", "t": line_at[0] + 0.5, "line": line_at[1],
                "chunk": ci, "start": chunk["start"] + offset, "end": chunk["end"] + offset,
                "para": chunk["para"], "sense": chunk["sense"], "a_span": a_span, "b_span": b_span,
                "a": said, "ocr": shown, "verdict": "agree", "reason": REASON_LABEL["agree"], "ocr_notes": [],
            })
    return items


def collect_items(raw: dict[str, Any], weak: list[dict[str, Any]],
                  ocr: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    offset = float(raw.get("offset_seconds", 0.0))
    chunks = raw["chunks"]
    items: list[dict[str, Any]] = []
    attach: dict[int, list[tuple[int, str | None]]] = {}
    agree = collect_agree_items(raw, ocr, attach) if ocr else []
    counterpart = {(ci, di): text for ci, pairs in attach.items() for di, text in pairs}
    for ci, chunk in enumerate(chunks):
        length = max(len(chunk["para"]), 1)
        for di, diff in enumerate(chunk["differences"]):
            if not _needs_review(diff):
                continue
            middle = (diff["a_span"][0] + diff["a_span"][1]) / 2 / length
            when = chunk["start"] + offset + middle * (chunk["end"] - chunk["start"])
            items.append({
                "id": f"d{ci}-{di}", "kind": "diff", "t": round(when, 2),
                "chunk": ci, "start": chunk["start"] + offset, "end": chunk["end"] + offset,
                "para": chunk["para"], "sense": chunk["sense"],
                "a_span": diff["a_span"], "b_span": diff["b_span"], "a": diff["a"], "b": diff["b"],
                "verdict": diff["verdict"], "reason": REASON_LABEL.get(diff["verdict"], ""),
                "screen": diff.get("screen_text", ""), "ocr_notes": [],
                "ocr": counterpart.get((ci, di)),
            })
    items += agree
    diff_items = [item for item in items if item["kind"] in ("diff", "agree")]
    for line in weak:
        near = [item for item in diff_items if abs(item["t"] - line["t"]) <= ATTACH_WINDOW
                and item["start"] - 1 <= line["t"] <= item["end"] + 1]
        if near:
            min(near, key=lambda item: abs(item["t"] - line["t"]))["ocr_notes"].append(
                {"t": line["t"], "text": line["text"], "variants": line["variants"]})
            continue
        candidate = [t[0] for t in tokens(line["text"])]
        para_ratio, para_text = _model_span(candidate, chunks, "para", line["t"] - offset - 1, line["t"] - offset + 3)
        sense_ratio, sense_text = _model_span(candidate, chunks, "sense", line["t"] - offset - 1, line["t"] - offset + 3)
        items.append({
            "id": f"o{line['t']}-{len(items)}", "kind": "ocr", "t": float(line["t"]), "text": line["text"],
            "variants": line["variants"],
            "para_text": para_text if para_ratio >= MIN_SPAN_MATCH else "",
            "sense_text": sense_text if sense_ratio >= MIN_SPAN_MATCH else "",
        })
    items.sort(key=lambda item: item["t"])
    return items


def load_suggestions(path: Path | None) -> dict[str, dict[str, Any]]:
    """AI 逐點判斷後寫的建議檔：{編號: {pick: para|sense|ocr|custom|none, text, why, check}}。"""
    if not path or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("suggestions", {})


def item_meta(item: dict[str, Any]) -> dict[str, Any]:
    """套用選擇時需要的資料（網頁下載選擇檔時也會帶同樣的內容）。"""
    if item["kind"] == "diff":
        return {"kind": "diff", "t": item["t"], "a": item["a"], "b": item["b"], "ocr": item["ocr"]}
    if item["kind"] == "agree":
        return {"kind": "agree", "t": item["t"], "chunk": item["chunk"], "a_span": item["a_span"],
                "a": item["a"], "ocr": item["ocr"]}
    return {"kind": "ocr", "t": item["t"], "text": item["text"], "para": item["para_text"], "sense": item["sense_text"]}


# ---------------------------------------------------------------- 截圖

def grab_frame(video: Path, seconds: float, band_top: float) -> str:
    """影片某一秒的字幕列截圖，回傳可直接放進網頁的 data URI。"""
    command = ["ffmpeg", "-v", "error", "-ss", f"{max(seconds, 0):.2f}", "-i", str(video), "-frames:v", "1",
               "-vf", f"crop=iw:ih*{BAND_HEIGHT}:0:ih*{band_top:.3f},scale={FRAME_WIDTH}:-1",
               "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "-"]
    result = subprocess.run(command, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return ""
    return "data:image/jpeg;base64," + base64.b64encode(result.stdout).decode("ascii")


def attach_frames(items: list[dict[str, Any]], video: Path, band_top: float) -> None:
    jobs = []
    for item in items:
        if item["kind"] in ("diff", "agree"):
            low, high = item["start"], max(item["start"], item["end"] - 0.1)
            times = [min(max(item["t"] + delta, low), high) for delta in DIFF_OFFSETS]
        else:
            times = [item["t"] + delta for delta in OCR_OFFSETS]
        item["frames"] = [{"t": t, "src": ""} for t in sorted(set(round(t, 1) for t in times))]
        jobs += item["frames"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        sources = list(pool.map(lambda frame: grab_frame(video, frame["t"], band_top), jobs))
    for frame, source in zip(jobs, sources):
        frame["src"] = source


# ---------------------------------------------------------------- 網頁

PAGE = r"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>字幕人工確認</title>
<style>
:root { --bg:#f6f5f1; --card:#ffffff; --ink:#1f1f1c; --mute:#6b6a63; --line:#dcdad2; --accent:#1b5e9b;
        --mark:#ffe08a; --markink:#3a2c00; --ok:#1f7a45; --warn:#9a5b00; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#171715; --card:#222220; --ink:#ecebe6; --mute:#a3a29a; --line:#3a3a36; --accent:#6db3f2;
  --mark:#6b5200; --markink:#fff1c2; --ok:#5fd08f; --warn:#f0b24f; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:16px/1.6 -apple-system,"PingFang TC","Noto Sans TC",sans-serif; }
header { position:sticky; top:0; z-index:5; background:var(--bg); border-bottom:1px solid var(--line);
         padding:10px 16px; }
.wrap { max-width:880px; margin:0 auto; }
h1 { font-size:18px; margin:0 0 4px; }
.bar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; font-size:14px; color:var(--mute); }
.meter { flex:1 1 160px; height:8px; background:var(--line); border-radius:4px; overflow:hidden; min-width:120px; }
.meter i { display:block; height:100%; background:var(--ok); width:0; }
button { font:inherit; padding:6px 14px; border-radius:8px; border:1px solid var(--line);
         background:var(--card); color:var(--ink); cursor:pointer; }
button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
main { padding:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin:0 auto 16px;
        max-width:880px; }
.card.done { border-left:5px solid var(--ok); }
.head { display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; align-items:baseline; }
.time { font-weight:700; }
.why { font-size:13px; color:var(--warn); }
.frames { display:flex; flex-direction:column; gap:6px; margin:10px 0; }
.frames figure { margin:0; }
.frames img { width:100%; border-radius:6px; display:block; background:#000; }
.frames figcaption { font-size:12px; color:var(--mute); }
.line { margin:4px 0; }
.line b { display:inline-block; min-width:6.5em; color:var(--mute); font-weight:600; }
mark { background:var(--mark); color:var(--markink); padding:0 2px; border-radius:3px; }
.note { font-size:14px; color:var(--mute); margin:6px 0; }
.choices { display:flex; flex-direction:column; gap:6px; margin-top:10px; }
.choices label { display:flex; gap:10px; align-items:center; padding:8px 10px; border:1px solid var(--line);
                 border-radius:8px; cursor:pointer; }
.choices label:has(input:checked) { border-color:var(--accent); background:color-mix(in srgb, var(--accent) 12%, transparent); }
.choices input[type=text] { flex:1; font:inherit; padding:4px 8px; border:1px solid var(--line); border-radius:6px;
                            background:var(--bg); color:var(--ink); min-width:0; }
.ai { font-size:14px; margin:8px 0 0; padding:8px 10px; border-radius:8px; border:1px dashed var(--accent); }
.ai.check { border-color:var(--warn); }
.hidden { display:none; }
</style>
</head>
<body>
<header><div class="wrap">
  <h1 id="title"></h1>
  <div class="bar">
    <span id="progress"></span><div class="meter"><i id="meter"></i></div>
    <button id="only">只看還沒選的</button>
    <button id="useai">套用 AI 建議到還沒選的</button>
    <button id="save" class="primary">下載我的選擇</button>
  </div>
</div></header>
<main id="list"></main>
<script>
const DATA = __DATA__;
const KEY = "review_" + DATA.video_id;
let answers = {};
try { answers = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { answers = {}; }
let onlyOpen = false;

function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function stamp(sec) { sec = Math.max(0, Math.floor(sec)); const m = Math.floor(sec / 60), s = sec % 60;
  return String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0"); }
function marked(text, span) {
  const a = span[0], b = span[1];
  const mid = a === b ? "<mark>▲（這裡沒有字）</mark>" : "<mark>" + esc(text.slice(a, b)) + "</mark>";
  return esc(text.slice(0, a)) + mid + esc(text.slice(b));
}
function save() { try { localStorage.setItem(KEY, JSON.stringify(answers)); } catch (e) {} }
function isDone(item) {
  const a = answers[item.id];
  return !!a && (a.choice !== "custom" || (a.text || "").trim() !== "");
}
function shown(text) { return text ? "「" + esc(text) + "」" : "（沒有這個字）"; }
function options(item) {
  if (item.kind === "agree") {
    return [["para", "語音（兩個模型一致）：" + shown(item.a)], ["ocr", "畫面字幕：" + shown(item.ocr)]];
  }
  if (item.kind === "diff") {
    const out = [["para", "Paraformer：" + shown(item.a)], ["sense", "SenseVoice：" + shown(item.b)]];
    if (item.ocr !== null && item.ocr !== undefined) out.push(["ocr", "畫面字幕：" + shown(item.ocr)]);
    return out;
  }
  const out = [["ocr", "畫面字幕讀到：「" + esc(item.text) + "」"]];
  if (item.para_text && item.para_text !== item.text) out.push(["para", "Paraformer 聽到：「" + esc(item.para_text) + "」"]);
  if (item.sense_text && item.sense_text !== item.text && item.sense_text !== item.para_text)
    out.push(["sense", "SenseVoice 聽到：「" + esc(item.sense_text) + "」"]);
  return out;
}
function aiLabel(item) {
  const pick = item.ai.pick;
  if (pick === "custom") return "改成「" + item.ai.text + "」";
  const found = options(item).find(o => o[0] === pick);
  return found ? found[1].replace(/<[^>]+>/g, "") : pick;
}
function render() {
  const list = document.getElementById("list");
  list.innerHTML = "";
  let done = 0;
  DATA.items.forEach((item, index) => {
    const finished = isDone(item);
    if (finished) done++;
    const card = document.createElement("section");
    card.className = "card" + (finished ? " done" : "") + (onlyOpen && finished ? " hidden" : "");
    let html = '<div class="head"><span class="time">#' + (index + 1) + "　" + stamp(item.t) + '</span>'
      + '<span class="why">' + (item.kind !== "ocr" ? esc(item.reason) : "畫面字幕讀得沒把握") + "</span></div>";
    html += '<div class="frames">' + item.frames.map(f =>
      f.src ? '<figure><img loading="lazy" src="' + f.src + '" alt="字幕截圖"><figcaption>影片 ' + stamp(f.t) + " 秒處的字幕列</figcaption></figure>"
            : '<figcaption>（這個時間點截圖失敗）</figcaption>').join("") + "</div>";
    if (item.kind === "agree") {
      html += '<div class="line"><b>Paraformer</b>' + marked(item.para, item.a_span) + "</div>";
      html += '<div class="line"><b>SenseVoice</b>' + marked(item.sense, item.b_span) + "</div>";
      html += '<div class="note">畫面字幕（' + stamp(item.t) + '）整句：' + esc(item.line) + "</div>";
    } else if (item.kind === "diff") {
      html += '<div class="line"><b>Paraformer</b>' + marked(item.para, item.a_span) + "</div>";
      html += '<div class="line"><b>SenseVoice</b>' + marked(item.sense, item.b_span) + "</div>";
      if (item.screen) html += '<div class="note">畫面字幕（程式讀的，可能有錯）最像的地方：' + esc(item.screen) + "</div>";
      item.ocr_notes.forEach(n => {
        html += '<div class="note">畫面字幕在 ' + stamp(n.t) + ' 讀得沒把握，讀到：' + esc(n.variants.join(" ／ ")) + "</div>"; });
    } else if (item.variants.length > 1) {
      html += '<div class="note">各張畫面讀到的版本：' + esc(item.variants.join(" ／ ")) + "</div>";
    }
    if (item.ai && item.ai.pick !== "none") {
      html += '<div class="ai' + (item.ai.check ? " check" : "") + '">AI 建議：' + esc(aiLabel(item)) + "｜" + esc(item.ai.why)
        + (item.ai.check ? "（建議你再看一眼截圖）" : "") + "</div>";
    } else if (item.ai) {
      html += '<div class="ai">AI 沒有建議：' + esc(item.ai.why) + "</div>";
    }
    html += '<div class="choices">';
    options(item).forEach(([value, label]) => {
      const checked = answers[item.id] && answers[item.id].choice === value ? " checked" : "";
      html += '<label><input type="radio" name="' + item.id + '" value="' + value + '"' + checked + "> <span>" + label + "</span></label>";
    });
    const custom = answers[item.id] && answers[item.id].choice === "custom";
    html += '<label><input type="radio" name="' + item.id + '" value="custom"' + (custom ? " checked" : "") + '> <span>都不對，應該是：</span>'
      + '<input type="text" value="' + esc(custom ? (answers[item.id].text || "") : "") + '" placeholder="直接輸入正確的字"></label>';
    const skipped = answers[item.id] && answers[item.id].choice === "skip";
    html += '<label><input type="radio" name="' + item.id + '" value="skip"' + (skipped ? " checked" : "") + "> <span>看不清楚，先跳過</span></label>";
    html += "</div>";
    card.innerHTML = html;
    card.querySelectorAll('input[type=radio]').forEach(r => r.addEventListener("change", () => {
      answers[item.id] = { choice: r.value, text: r.value === "custom" ? card.querySelector('input[type=text]').value : "" };
      save(); refresh(); }));
    const box = card.querySelector('input[type=text]');
    box.addEventListener("input", () => {
      answers[item.id] = { choice: "custom", text: box.value };
      card.querySelector('input[value=custom]').checked = true; save();
      card.classList.toggle("done", box.value.trim() !== ""); setProgress(DATA.items.filter(isDone).length); });
    list.appendChild(card);
  });
  setProgress(done);
}
function setProgress(done) {
  const total = DATA.items.length;
  document.getElementById("progress").textContent = "已選 " + done + " ／ 共 " + total + " 個";
  document.getElementById("meter").style.width = (total ? 100 * done / total : 0) + "%";
}
function refresh() { const y = window.scrollY; render(); window.scrollTo(0, y); }
document.getElementById("useai").addEventListener("click", () => {
  let n = 0;
  DATA.items.forEach(item => {
    if (!answers[item.id] && item.ai && item.ai.pick !== "none") {
      answers[item.id] = { choice: item.ai.pick, text: item.ai.pick === "custom" ? item.ai.text : "", ai: true }; n++; }
  });
  save(); refresh(); alert("已把 AI 建議套到 " + n + " 個還沒選的地方；你可以逐個改。");
});
document.getElementById("title").textContent = "字幕人工確認：" + DATA.title;
document.getElementById("only").addEventListener("click", e => {
  onlyOpen = !onlyOpen; e.target.textContent = onlyOpen ? "顯示全部" : "只看還沒選的"; render(); });
document.getElementById("save").addEventListener("click", () => {
  const meta = {};
  DATA.items.forEach(item => { meta[item.id] = item.kind === "diff"
    ? { kind: "diff", t: item.t, a: item.a, b: item.b, ocr: item.ocr }
    : item.kind === "agree" ? { kind: "agree", t: item.t, chunk: item.chunk, a_span: item.a_span, a: item.a, ocr: item.ocr }
    : { kind: "ocr", t: item.t, text: item.text, para: item.para_text, sense: item.sense_text }; });
  const body = JSON.stringify({ video_id: DATA.video_id, answers: answers, items: meta }, null, 1);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([body], { type: "application/json" }));
  link.download = DATA.video_id + "_decisions.json";
  document.body.appendChild(link); link.click(); link.remove();
});
render();
</script>
</body>
</html>
"""


def build(args: argparse.Namespace) -> int:
    record_id = find_record_id(args.video_id)
    title, _ = _read_meta(record_id)
    folder = ROOT / "output" / "videos" / record_id
    out_path = args.out or folder / f"{record_id}_review.html"
    for path, what in ((args.raw, "FunASR 原始結果"), (args.ocr, "畫面字幕檔"), (args.video, "影片檔")):
        if not path.is_file():
            print(f"錯誤：找不到{what}：{path}", file=sys.stderr)
            return 1
    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    weak, row = read_weak_lines(args.ocr)
    items = collect_items(raw, weak, read_ocr(args.ocr))
    suggestions = load_suggestions(args.suggestions)
    for item in items:
        item["ai"] = suggestions.get(item["id"])
    if not items:
        print("沒有需要人確認的地方。")
        return 0
    band_top = min(max(0.0, row + 0.04 - BAND_HEIGHT), 1.0 - BAND_HEIGHT)
    print(f"需要確認 {len(items)} 個地方，截圖中…")
    attach_frames(items, args.video, band_top)
    page = PAGE.replace("__DATA__", json.dumps({"video_id": args.video_id, "title": title, "items": items},
                                               ensure_ascii=False).replace("</", "<\\/"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    failed = sum(1 for item in items for frame in item["frames"] if not frame["src"])
    count = {kind: sum(1 for item in items if item["kind"] == kind) for kind in ("diff", "agree", "ocr")}
    print(f"完成：{count['diff']} 個語音差異處、{count['agree']} 個「語音一致但畫面不同」、"
          f"{count['ocr']} 個畫面沒把握的句子，截圖失敗 {failed} 張 → {out_path}")
    return 0


# ---------------------------------------------------------------- 套用選擇

def _effective_answers(asked: dict[str, Any], answers: dict[str, Any],
                       suggestions: dict[str, dict[str, Any]]) -> dict[str, tuple[dict[str, Any], str]]:
    """每個地方最後採用誰的答案：人 > AI 建議。回傳 {編號: (答案, "人" 或 "AI")}。"""
    out: dict[str, tuple[dict[str, Any], str]] = {}
    for key in asked:
        mine = answers.get(key)
        if mine and not mine.get("ai"):
            out[key] = (mine, "人")
        elif mine and mine.get("ai"):
            out[key] = (mine, "AI")
        elif key in suggestions and suggestions[key].get("pick") not in (None, "none"):
            out[key] = ({"choice": suggestions[key]["pick"], "text": suggestions[key].get("text", "")}, "AI")
    return out


def _word_for(meta: dict[str, Any], answer: dict[str, Any]) -> str | None:
    """依答案決定這個地方的文字；None 代表沒有可用的答案（跳過、或選項不存在）。"""
    choice = answer.get("choice")
    if choice == "custom":
        text = answer.get("text", "").strip()
        # 整格只寫「（沒有這個字）」「(沒有字)」這類備註，意思是這個地方不該有字：刪掉
        if re.fullmatch(r"[（(]\s*(沒有|無|不要)[^）)]*[）)]", text):
            return ""
        return text or None
    if meta["kind"] == "diff":
        return {"para": meta["a"], "sense": meta["b"], "ocr": meta.get("ocr")}.get(choice)
    if meta["kind"] == "agree":
        return {"para": meta["a"], "ocr": meta.get("ocr")}.get(choice)
    return {"ocr": meta.get("text"), "para": meta.get("para"), "sense": meta.get("sense")}.get(choice) or None


def apply(args: argparse.Namespace) -> int:
    record_id = find_record_id(args.video_id)
    title, url = _read_meta(record_id)
    out_path = args.out or ROOT / "output" / "videos" / record_id / f"{record_id}_confirmed.md"
    raw = json.loads(args.raw.read_text(encoding="utf-8"))
    weak, _ = read_weak_lines(args.ocr)
    items = collect_items(raw, weak, read_ocr(args.ocr))
    asked = {item["id"]: item_meta(item) for item in items}
    answers = {}
    if args.decisions:
        answers = json.loads(args.decisions.expanduser().read_text(encoding="utf-8")).get("answers", {})
    chosen = _effective_answers(asked, answers, load_suggestions(args.suggestions))
    offset = float(raw.get("offset_seconds", 0.0))
    lines, changed = [], []
    counts = {"人": 0, "AI": 0, "未決定": 0}
    decided: dict[str, str | None] = {}
    for key, meta in asked.items():
        word, who = (None, "")
        if key in chosen:
            word, who = _word_for(meta, chosen[key][0]), chosen[key][1]
        decided[key] = word
        counts[who if word is not None else "未決定"] += 1
    for ci, chunk in enumerate(raw["chunks"]):
        text, open_item, ai_item = chunk["para"], False, False
        edits: list[tuple[int, int, str]] = []
        for di, diff in enumerate(chunk["differences"]):
            key = f"d{ci}-{di}"
            word = decided.get(key)
            if key in asked:
                if word is None:
                    open_item = True
                    word = diff["b"] if diff.get("verdict") == "sense" else diff["a"]
                else:
                    ai_item = ai_item or chosen[key][1] == "AI"
                    if word != diff["a"]:
                        changed.append((chunk["start"] + offset, diff["a"], diff["b"], word, chosen[key][1]))
            else:
                word = diff["b"] if diff.get("verdict") == "sense" else diff["a"]
            edits.append((diff["a_span"][0], diff["a_span"][1], word))
        for key, meta in asked.items():
            if meta["kind"] != "agree" or meta["chunk"] != ci:
                continue
            word = decided.get(key)
            if word is None:
                open_item = True
                continue
            ai_item = ai_item or chosen[key][1] == "AI"
            edits.append((meta["a_span"][0], meta["a_span"][1], word))
            if word != meta["a"]:
                changed.append((chunk["start"] + offset, meta["a"], None, word, chosen[key][1]))
        last = len(text) + 1
        for begin, end, word in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
            if end > last:
                continue                                   # 兩處更動互相重疊時，只採用前面的
            text = text[:begin] + word + text[end:]
            last = begin
        if text.strip():
            mark = "（待確認）" if open_item else ("（含 AI 建議，未經人確認）" if ai_item else "")
            lines.append(f"- **[{_timestamp(chunk['start'] + offset)}]** {text}{mark}")
    ocr_lines = []
    for key, meta in asked.items():
        if meta["kind"] != "ocr":
            continue
        word = decided.get(key)
        who = chosen[key][1] if key in chosen and word is not None else ""
        shown = word if word is not None else meta["text"]
        tag = "" if who == "人" else ("（AI 建議，未經人確認）" if who == "AI" else "（待確認，沿用畫面字幕原讀）")
        ocr_lines.append(f"- **[{_timestamp(meta['t'])}]** {shown}{tag}")
        if word is not None and word != meta["text"]:
            changed.append((meta["t"], "畫面字幕：" + meta["text"], None, word, who))
    kinds = [meta["kind"] for meta in asked.values()]
    out = [f"# {title}（確認後）", "", f"- YouTube Video ID：{args.video_id}", f"- 原始網址：{url or ''}",
           f"- 採用順序：人工選的 > AI 建議 > 原本的判定。人工 {counts['人']} 個、AI 建議 {counts['AI']} 個、"
           f"沒有答案 {counts['未決定']} 個（保留原本的判定，並標「待確認」）",
           f"- 需要確認的地方共 {len(kinds)} 個（語音差異處 {kinds.count('diff')}、語音一致但畫面不同 {kinds.count('agree')}、"
           f"畫面字幕沒把握 {kinds.count('ocr')}）",
           "", "## 逐字稿", ""] + lines
    if ocr_lines:
        out += ["", "## 畫面字幕裡原本沒把握的句子（確認結果）", ""] + sorted(ocr_lines)
    if changed:
        out += ["", "## 和語音原本寫法不同的地方（建議把常見的加進詞庫 data/glossary.md）", ""]
        out += [(f"- **[{_timestamp(t)}]** 語音「{a}」／SenseVoice「{b}」→「{word}」（{who or '—'}）" if b is not None
                 else f"- **[{_timestamp(t)}]** {a} → 「{word}」（{who or '—'}）")
                for t, a, b, word, who in sorted(changed, key=lambda c: c[0])]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"完成：人工 {counts['人']} 個、AI 建議 {counts['AI']} 個、沒有答案 {counts['未決定']} 個 → {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="把需要人確認的地方做成網頁（含字幕截圖），並把選擇套回逐字稿")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("build", help="產生確認網頁")
    one.add_argument("--video-id", required=True)
    one.add_argument("--raw", required=True, type=Path, help="transcribe_funasr 產生的 *_raw.json")
    one.add_argument("--ocr", required=True, type=Path, help="subtitle_ocr 產生的畫面字幕檔")
    one.add_argument("--video", required=True, type=Path, help="影片檔（只用來截圖）")
    one.add_argument("--suggestions", type=Path, help="AI 建議檔（*_ai_suggestions.json）；有的話卡片會顯示 AI 建議")
    one.add_argument("--out", type=Path, help="網頁輸出路徑（預設 output/videos/<id>/<id>_review.html）")
    two = sub.add_parser("apply", help="把網頁下載的選擇套回逐字稿")
    two.add_argument("--video-id", required=True)
    two.add_argument("--raw", required=True, type=Path)
    two.add_argument("--ocr", required=True, type=Path, help="subtitle_ocr 產生的畫面字幕檔（用來重建每個地方的資料）")
    two.add_argument("--decisions", type=Path, help="網頁「下載我的選擇」得到的 *_decisions.json（沒有就只用 AI 建議）")
    two.add_argument("--suggestions", type=Path, help="AI 建議檔（*_ai_suggestions.json）")
    two.add_argument("--out", type=Path, help="輸出 markdown 路徑（預設 output/videos/<id>/<id>_confirmed.md）")
    args = parser.parse_args()
    return build(args) if args.command == "build" else apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
