# 桃園市觀音區知識庫爬取工具

本專案是一套可重複執行的 Python 多媒體爬蟲。程式讀取 `urls.csv`，辨識一般網頁與 YouTube 來源，擷取主要文字、標題、來源資訊、圖片候選及超連結，並依設定進行有限的延伸探索。爬蟲完成後會自動執行 `postprocess.py`，整理 Markdown、結構化索引及報表。

## 1. 環境需求

- Python 3.10 或更新版本
- Windows PowerShell（以下指令以 Windows 為例）
- 若要執行影片音訊轉檔與 Whisper：另需安裝 FFmpeg，並確認 `ffmpeg -version` 可正常執行

建議在專案根目錄建立虛擬環境：

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
```

若需要無字幕影片的 Whisper 語音辨識，再安裝：

```powershell
py -m pip install -r requirements-transcription.txt
```

## 2. 執行方式

在專案根目錄執行：

```powershell
py -m crawler.crawler
```

若要指定本機影音資料夾、Whisper 模型及代表畫面張數：

```powershell
py -m crawler.crawler --media-dir local_videos --model small --frame-count 5
```

`local_videos` 為選用資料夾。若要擷取影片代表畫面，請將有權處理的本機影片放入該資料夾，並以對應的 YouTube video ID 開頭命名。ZIP 壓縮檔不包含影片原始檔。

爬蟲預設會在完成後自動執行根目錄的 `postprocess.py`。若只想執行爬蟲、不執行後處理：

```powershell
py -m crawler.crawler --no-postprocess
```

## 3. 常用參數

```powershell
py -m crawler.crawler --max-depth 2 --max-extra 60 --per-domain 6 --delay 2
py -m crawler.crawler --no-extend
py -m crawler.crawler --limit 3 --no-extend
py -m crawler.crawler --no-postprocess
py -m crawler.crawler --no-download-audio
```

- `--max-depth`：延伸探索深度，預設 1。
- `--max-extra`：延伸頁面總數上限，預設 40。
- `--per-domain`：每個網域的延伸頁面上限，預設 5。
- `--delay`：同網域請求間隔秒數，預設 1.5。
- `--limit`：限制本次處理的原始網址數量，適合小規模測試。
- `--no-extend`：停用延伸探索。
- `--no-postprocess`：停用爬取完成後的自動後處理。
- `--no-download-audio`：停用無字幕影片的音訊下載備援。

## 4. YouTube 字幕與 Whisper 處理流程

影片文字處理依序採用以下方式：

1. 優先取得 YouTube 可讀字幕。
2. 若沒有可讀字幕，先尋找 `--media-dir` 指定資料夾中以 YouTube video ID 開頭的本機影音檔。
3. 若沒有本機檔案，且未使用 `--no-download-audio`，則使用 `yt-dlp` 嘗試下載音訊。
4. 使用 FFmpeg 將音訊轉成 16 kHz、單聲道 WAV。
5. 使用 `faster-whisper` 產生逐字稿與時間戳記。

音訊快取位置：

- 下載音訊：`data/audio_work/downloads/`
- 轉換後音訊：`data/audio_work/audio/`

預設 Whisper 模型可由 `--model` 指定，例如 `small`。下載或辨識失敗時，程式會記錄原因並繼續處理其他來源。

請只對有權下載及處理的媒體使用音訊下載功能。程式不應用來繞過登入、付費牆、驗證碼或其他存取限制。

## 5. 主要程式說明

- `crawler/crawler.py`：主要執行入口，負責讀取網址、一般網頁爬取、延伸探索，以及 YouTube 資訊、字幕與音訊處理。
- `crawler/transcribe.py`：Whisper 語音辨識功能，由主要爬蟲流程呼叫。
- `postprocess.py`：整理原始紀錄，產生文章／影片 Markdown、圖片下載結果、代表畫面、索引及報表。
- `capture_comparisons.py`：獨立產生網站清理前後對照素材。
- `crawler/transcribe_extra.py`：獨立語音辨識輔助腳本，不屬於一般批次執行流程。
- `crawler/old_crawler.py`：舊版爬蟲程式，保留作為參考，不是目前執行入口。

`old_postprocess.py` 已從目前整理版本移除；目前使用的後處理程式為根目錄的 `postprocess.py`。

## 6. 主要輸出資料

- `data/records.jsonl`：爬蟲原始紀錄，一行一筆，包含來源類型、階層資訊、標題、正文、圖片候選及連結。
- `data/crawl_status.csv`：原始來源與延伸網址的處理狀態、HTTP 狀態碼及原因。
- `data/audio_work/`：音訊下載及轉換快取。
- `output/articles/`：一般網頁 Markdown。
- `output/videos/`：影片資訊與字幕 Markdown。
- `output/videos/transcripts/`：語音辨識逐字稿。
- `output/images/`：成功下載的文章圖片。
- `output/index.jsonl`：後處理後的結構化索引，包含主題、關鍵字及圖片下載狀態等欄位。
- `output/crawl_report.csv`：依輸出 ID 對應的爬取報表。
- `report/`：作業報告、驗證紀錄及清理前後對照素材。

## 7. 資料清理與限制

正文擷取會優先使用文章或主要內容區塊，並嘗試排除常見導覽列、頁首頁尾、Cookie／同意提示、訂閱、分享、推薦、留言、彈窗及程式碼等雜訊。圖片處理會讀取一般及常見 Lazy Loading 屬性，嘗試排除 Logo、Banner、追蹤像素等圖片，並記錄來源網址、圖說及下載狀態。

不同網站的版型與存取限制可能影響正文清理及圖片下載結果，重要成果仍應人工抽查。

影片代表畫面需要可處理的本機影片檔；只有下載音訊不能產生影片畫格。若沒有可用影片檔，程式會在對應的 `*_frames/README.md` 記錄原因，不會以 YouTube 縮圖冒充代表畫面。

## 8. 來源清單注意事項

目前專案的 `urls.csv` 含 32 筆來源列；作業說明列出 31 個指定網址。清單中甘泉寺維基百科網址曾有一筆缺少結尾右括號，另有一筆完整網址。程式會依 CSV 原值處理，只有完全相同的網址才會去重。

正式繳交前，請依教師提供的原始清單確認應保留 31 或 32 筆，並修正錯誤網址。

## 9. 報告與重跑注意事項

- `RESULTS.md`：執行成果摘要。
- `report/report.md`：作業任務對應、執行統計、清理策略與限制。
- `report/validation.md`：驗證紀錄。
- `data/fact_checks.csv`：地方知識來源查核摘要。
- `PROJECT_CLEANUP.md`：本次專案整理紀錄。

每次重新執行爬蟲，輸出統計可能改變。正式報告中的筆數應以該次實際產生的資料為準，並同步更新 `RESULTS.md` 與 `report/report.md`。重新執行前，建議先備份現有 `output/` 與 `data/`。
