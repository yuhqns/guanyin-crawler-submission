# 影片處理流程說明

這份說明只講「影片」這條線（網頁部分見 `README.md`）。流程分三步：先分流、再依字幕狀況走不同路線，最後都產出可以直接使用的文字與截圖。

```
urls.csv ──► 影片分流器 ──┬─ 上傳者有提供字幕 ─► 取字幕 → 挑重點 → 重點截圖
 (video_triage)           ├─ 只有自動字幕     ─► 抓自動字幕 ＋ 照「沒字幕」的做法處理 → 兩邊比較
                          ├─ 完全沒字幕       ─► FunASR 兩個模型 ＋ 畫面字幕 OCR 校對 → 人工確認網頁
                          └─ 不是 YouTube     ─► 當網頁處理（交給網頁分類與爬蟲）
```

## 0. 環境

| 需要 | 用在哪 | 安裝 |
|---|---|---|
| Python 3.10+ | 全部 | — |
| yt-dlp | 分流、取字幕（只讀資訊，不下載影片） | `brew install yt-dlp` |
| FFmpeg | 截圖、轉音訊 | `brew install ffmpeg` |
| FunASR 環境 | 沒字幕路線的語音辨識 | 另開虛擬環境，`pip install -r requirements-funasr.txt`；第一次執行會下載約 3 GB 模型 |
| macOS | 畫面字幕 OCR（Apple Vision） | 只能在 Mac 執行；Windows 不能跑這一步 |

截圖與辨識需要影片檔，一律放在 `local_videos/`，檔名用 YouTube 影片 ID（例如 `uRHrog9kbvA.mp4`）。這個資料夾與 `*.mp4` 已被 `.gitignore` 排除，不會上傳。請只下載有權處理的影片。

## 1. 影片分流器

```bash
python -m crawler.video_triage                      # 讀 urls.csv，寫 output/video_triage.csv
python -m crawler.video_triage --add 編號=網址        # 另外加入不在 csv 裡的影片（可重複）
```

只讀影片資訊，不下載。輸出每個網址一列：類型（影片／網頁）、子類型、處理路線、理由、影片 ID、標題、時長、字幕語言。

| 子類型 | 處理路線（route 欄） |
|---|---|
| 有字幕（上傳者提供） | `video_with_subtitles` |
| 只有自動字幕 | `auto_caption_compare` |
| 沒有字幕 | `funasr_ocr_review` |
| 無法判斷（連不上、被擋） | `重試或人工確認`；不會誤標成「沒有字幕」 |
| 網頁 | `web` |

編號規則和爬蟲相同（`page001`…），所以可以直接對應 `data/records.jsonl` 與 `output/` 底下的檔案。

## 2. 看進度與下一步

```bash
python -m crawler.video_pipeline status
```

列出每支影片每個步驟有沒有完成，以及第一個還沒做的步驟和它的指令。它只盤點檔案，不會自己執行任何處理。

## 3. 路線 A：有字幕

```bash
python -m crawler.subtitle_highlights fetch --id page027        # 取上傳者字幕、切段
# 讀 output/videos/page027/page027_subtitle_chunks.md，挑重點，寫成 page027_highlights.json
python -m crawler.subtitle_highlights render --id page027 --video local_videos/uRHrog9kbvA.mp4
```

- 字幕只用上傳者提供的；有多種語言時中文優先（繁體在前），沒有中文時不亂挑，請用 `--lang` 指定。
- **重點由人或 AI 讀字幕後挑**，程式只檢查格式（編號要存在、不可重複、要有標題）。`highlights.json` 格式：

```json
{"video_id": "…", "note": "誰挑的、依據什麼",
 "highlights": [{"cue": "c012", "title": "重點標題", "summary": "一兩句說明"}]}
```

- 截圖取每個重點那句字幕時間的中間點。影片比字幕短時會報錯，避免拿錯影片。
- 輸出：`output/videos/<編號>/<編號>_highlights.md` 與 `highlights/*.jpg`。不給 `--video` 也能輸出文字版，會註明沒有截圖。

## 4. 路線 B：完全沒字幕

```bash
python -m crawler.subtitle_ocr --video-id <影片ID> --video local_videos/<影片ID>.mp4      # 畫面字幕 OCR（Mac）
<FunASR 環境的 python> -m crawler.transcribe_funasr --video-id <影片ID> --audio local_videos/<影片ID>.mp4
python -m crawler.review_page build --video-id <影片ID> --raw <…_funasr_glossary_raw.json> --ocr <…_ocr.md> --video local_videos/<影片ID>.mp4
# 在網頁選好後按「下載我的選擇」
python -m crawler.review_page apply --video-id <影片ID> --raw <…raw.json> --ocr <…_ocr.md> --decisions <下載的 json>
```

- 兩個語音模型（Paraformer-zh、SenseVoice-Small）各辨識一次；不一樣的地方用畫面字幕 OCR 驗證。
- 專有名詞放 `data/glossary.md`，辨識時會套用「已確認／畫面證實」的詞。
- 確認網頁會列出：兩個模型不同的地方、兩個模型一致但畫面不同的地方、畫面字幕沒把握的地方。有畫面字幕證據時以畫面為準；畫面明顯讀錯時才改用語意判斷。
- 套用順序：人工選的 ＞ AI 建議 ＞ 原本判定。AI 建議用過的句子會標「（含 AI 建議，未經人確認）」。
- 確認網頁內嵌影片截圖，`*_review.html` 已被 `.gitignore` 排除。

## 5. 路線 C：只有自動字幕

```bash
python -m crawler.subtitle_highlights fetch --id <編號> --auto     # 抓自動字幕（影片原聲那一軌，-orig）
# 另外照路線 B 跑完 OCR 與 FunASR
python -m crawler.caption_compare --id <編號>                      # 輸出 <編號>_auto_vs_asr.md
```

兩邊都是機器辨識，比較結果只標出「值得再看」的地方，不判定誰對。這條路線目前只有單元測試與真實自動字幕檔的解析測試，還沒有真實的中文「只有自動字幕」影片完整跑過。

## 6. 測試

```bash
python -m unittest discover -s tests
```

不連網（用假資料與 ffmpeg 現做的測試短片）。

## 7. 已知限制

- 畫面字幕 OCR（Apple Vision）有時漏掉「的」，背景的舊文件或地圖文字也可能干擾；所以才需要多一道確認。
- 片尾上方的宣傳字卡、字幕空窗期不一定讀得到。
- 只驗證過少數影片，換影片請抽查結果。
- 嵌在網頁裡的影片（不是 YouTube 網址）要靠網頁爬蟲先發現，這裡不會自動找。
