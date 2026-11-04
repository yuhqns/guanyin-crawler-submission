# 桃園市觀音區知識庫爬取工具

本專案使用 Python 建立觀音區地方知識資料集，從 `urls.csv` 讀取來源網址，擷取一般網頁與 YouTube 影片資訊，並進行有限的相關連結延伸探索。爬取完成後，可透過 `postprocess.py` 將原始紀錄整理成 Markdown、結構化索引及報表。

YouTube 影片會分別保留兩種文字來源：

1. **YouTube 平台字幕**：透過 `yt-dlp` 取得影片提供的字幕或自動字幕。
2. **Whisper 語音辨識逐字稿**：另外下載影片音訊，使用 `faster-whisper` 辨識。

兩種文字來源會獨立保存，並整合在同一部影片的 `video.md` 中；即使影片已有 YouTube 字幕，爬蟲仍會另外執行 Whisper，不會只在缺少平台字幕時才辨識。

---

## 1. 環境需求

- Python 3.10 或更新版本
- Windows 10／11
- FFmpeg：供影片音訊轉換使用
- yt-dlp：取得 YouTube 影片資訊、字幕及音訊
- faster-whisper：執行 Whisper 語音辨識

請先確認 FFmpeg 已安裝，並且可以在 CMD 執行：

```cmd
ffmpeg -version
```

## 2. 安裝方式

以下指令以 **Windows CMD** 為例，請在專案根目錄執行。

### 2.1 建立並啟用虛擬環境

```cmd
py -m venv .venv
.venv\Scripts\activate
```

### 2.2 安裝一般爬蟲套件

```cmd
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 2.3 安裝 Whisper 語音辨識套件

```cmd
python -m pip install -r requirements-transcription.txt
```

`requirements-transcription.txt` 會安裝 `faster-whisper` 與 `av`。本專案的 YouTube 雙來源流程需要這組套件，並非只有在沒有平台字幕時才需要安裝。

## 3. 執行爬蟲

在專案根目錄執行：

```cmd
python crawler\crawler.py
```

預設設定如下：

| 項目 | 預設值 |
|---|---|
| 輸入網址清單 | `urls.csv` |
| 原始資料輸出 | `data` |
| 延伸探索深度 | `1` |
| 額外延伸頁面上限 | `40` |
| 每個網域延伸頁面上限 | `5` |
| 同網域請求間隔 | `1.5` 秒 |
| 單一請求逾時 | `20` 秒 |
| 原始網址筆數限制 | `0`（全部處理） |
| Whisper 模型 | `small` |
| YouTube 音訊下載 | 啟用 |
| 爬取完成後後處理 | 啟用 |

### 3.1 常用執行範例

完整執行全部來源，使用預設 Small 模型：

```cmd
python crawler\crawler.py
```

指定延伸探索深度及頁面上限：

```cmd
python crawler\crawler.py --max-depth 2 --max-extra 60 --per-domain 6 --delay 2
```

只處理前三筆原始網址（測試用途）：

```cmd
python crawler\crawler.py --limit 3 --no-extend
```

停用延伸探索：

```cmd
python crawler\crawler.py --no-extend
```

只執行爬蟲，不自動啟動後處理：

```cmd
python crawler\crawler.py --no-postprocess
```

停用 YouTube 音訊下載：

```cmd
python crawler\crawler.py --no-download-audio
```

停用音訊下載後，仍可透過 `--media-dir` 使用指定資料夾內已有的本機影音檔進行 Whisper 辨識。

### 3.2 Whisper 模型切換

目前預設模型為 `small`。如需改用 Large，可在執行時指定：

```cmd
python crawler\crawler.py --model large-v3
```

若要使用預設模型，則不必加上 `--model`：

```cmd
python crawler\crawler.py
```

第一次使用某個模型時，`faster-whisper` 可能需要下載模型檔；下載完成後通常會使用本機快取。Large 模型需要較多記憶體及運算時間，實際速度會依硬體與音訊長度而異。

目前 `crawler/transcribe.py` 的模型執行設定為 **CPU + INT8**。單純指定 `--model large-v3` 只會切換模型，不會自動改成 GPU 推論。若要使用 NVIDIA GPU，還需要另外設定並確認 CUDA、cuDNN 與 CTranslate2 環境相容，並修改程式中的裝置設定。

## 4. YouTube 雙來源字幕與逐字稿流程

每一部 YouTube 影片會分別執行以下工作：

### 4.1 YouTube 平台字幕

1. 使用 `yt-dlp` 取得影片資訊及字幕軌。
2. 優先依程式選取的語言順序尋找可讀字幕。
3. 解析字幕時間戳記及文字。
4. 將平台字幕保存在原始紀錄的 `content` 與 `transcript_segments` 欄位。

若沒有取得可讀字幕，會在紀錄中標示平台字幕不可用或相關備註。

### 4.2 Whisper 語音辨識

1. 不論是否已取得平台字幕，都會另外執行 Whisper 流程。
2. 優先尋找 `--media-dir` 指定資料夾中，以 YouTube Video ID 開頭的本機影音檔。
3. 若找不到本機影音檔，且未指定 `--no-download-audio`，則使用 `yt-dlp` 下載音訊。
4. 使用 FFmpeg 將音訊轉成 16 kHz、單聲道 WAV。
5. 使用 `faster-whisper` 指定模型進行語音辨識。
6. 將 Whisper 逐字稿及時間戳記獨立保存於 `transcription` 與 `transcription_segments` 欄位。

平台字幕與 Whisper 辨識結果不會互相覆蓋，後處理時會一起寫入同一部影片的 `video.md`，並以不同標題區分。

### 4.3 音訊快取位置

預設輸出資料夾為 `data` 時：

- yt-dlp 下載音訊：`data/audio_work/downloads/`
- FFmpeg 轉換後 WAV：`data/audio_work/audio/`

音訊下載或語音辨識失敗時，程式會記錄辨識狀態及原因；該影片可能仍保有成功取得的平台字幕。

### 4.4 本機影音檔與代表畫面

若要使用本機影片進行辨識或擷取代表畫面，可指定：

```cmd
python crawler\crawler.py --media-dir local_videos
```

請將有權處理的本機影片放入 `local_videos`，檔名需以對應的 YouTube Video ID 開頭。

代表畫面擷取需要本機影片檔。只有下載音訊並不能產生影片畫格；若沒有可用影片檔，後處理會在該影片資料夾的 `*_frames/README.md` 記錄原因，不會以 YouTube 縮圖代替代表畫面。

## 5. 命令列參數

| 參數 | 說明 | 預設值 |
|---|---|---|
| `--input` | 輸入網址 CSV | `urls.csv` |
| `--output` | 原始紀錄輸出資料夾 | `data` |
| `--max-depth` | 延伸探索深度，可選 0～3 | `1` |
| `--max-extra` | 額外延伸頁面總數上限 | `40` |
| `--per-domain` | 每個網域的延伸頁面上限 | `5` |
| `--delay` | 同網域請求間隔秒數 | `1.5` |
| `--timeout` | 單一請求逾時秒數 | `20` |
| `--limit` | 原始網址處理筆數；0 表示全部 | `0` |
| `--media-dir` | 本機影音檔資料夾 | 未指定 |
| `--download-audio` | 啟用 YouTube 音訊下載 | 啟用 |
| `--no-download-audio` | 停用音訊下載 | — |
| `--model` | faster-whisper 模型名稱 | `small` |
| `--frame-count` | 每部本機影片擷取代表畫面數量，可選 3、4、5 | `5` |
| `--no-postprocess` | 不自動執行後處理 | 未啟用 |
| `--no-extend` | 停用延伸探索 | 未啟用 |

查看完整參數：

```cmd
python crawler\crawler.py --help
```

## 6. 專案結構

```text
guanyin-final-backup/
├─ crawler/
│  ├─ crawler.py                 # 主要爬蟲入口
│  ├─ transcribe.py              # faster-whisper 音訊轉錄功能
│  ├─ transcribe_extra.py        # 獨立語音辨識輔助腳本
│  └─ old_crawler.py             # 舊版爬蟲，供參考
├─ data/
│  ├─ records.jsonl              # 爬蟲原始紀錄
│  ├─ crawl_status.csv           # 爬取狀態
│  ├─ fact_checks.csv            # 地方知識來源查核摘要
│  └─ audio_work/
│     ├─ downloads/              # 下載的音訊
│     └─ audio/                  # 轉換後 WAV
├─ output/
│  ├─ articles/                  # 一般網頁 Markdown
│  ├─ videos/                    # 影片資料夾及 video.md
│  ├─ images/                    # 下載的文章圖片
│  ├─ index.jsonl                # 結構化索引
│  └─ crawl_report.csv           # 爬取報表
├─ report/                       # 作業報告及驗證資料
├─ urls.csv                      # 原始網址清單
├─ requirements.txt              # 一般爬蟲套件
├─ requirements-transcription.txt# Whisper 相關套件
├─ postprocess.py                # 後處理及輸出整理
├─ capture_comparisons.py        # 網頁清理前後對照素材
├─ RESULTS.md                    # 執行成果摘要
├─ PROJECT_CLEANUP.md            # 專案整理紀錄
└─ README.md
```

## 7. 主要輸出資料說明

| 路徑 | 內容 |
|---|---|
| `data/records.jsonl` | 爬蟲原始紀錄，一行一筆，包含來源、標題、正文、平台字幕及 Whisper 欄位等 |
| `data/crawl_status.csv` | 原始來源與延伸網址的處理狀態、HTTP 狀態碼及原因 |
| `data/fact_checks.csv` | 地方知識來源查核摘要 |
| `data/audio_work/downloads/` | yt-dlp 下載的 YouTube 音訊 |
| `data/audio_work/audio/` | FFmpeg 轉換後供 Whisper 使用的 WAV |
| `output/articles/` | 一般網頁 Markdown |
| `output/videos/<ID>/video.md` | 單部影片的資訊、YouTube 平台字幕及 Whisper 逐字稿 |
| `output/videos/<ID>/` | 影片相關縮圖及其他輸出素材 |
| `output/images/` | 成功下載的文章圖片 |
| `output/index.jsonl` | 後處理後的結構化索引 |
| `output/crawl_report.csv` | 依輸出 ID 對應的爬取報表 |
| `report/` | 作業報告、驗證紀錄及清理前後對照素材 |

影片的 `video.md` 會將兩種文字來源分開呈現：

- `YouTube 平台字幕`
- `爬蟲流程中的 Whisper 逐字稿`

若其中一種來源無法取得，仍會保留另一種成功取得的結果，並在紀錄中留下可用性或錯誤備註。

## 8. 重新執行與資料覆蓋注意事項

目前爬蟲沒有提供讀取舊進度並接續執行的 `--resume`／`--continue` 參數。

每次啟動爬蟲時，程式會刪除指定 `--output` 資料夾中的：

- `records.jsonl`
- `crawl_status.csv`

接著重新建立本次執行的紀錄。因此，使用預設輸出資料夾 `data` 重新執行時，上述兩個檔案會被覆蓋。若要保留舊紀錄，請先自行備份，或指定新的 `--output` 資料夾。

**注意後處理路徑：** 目前 `postprocess.py` 固定讀取專案根目錄的 `data/records.jsonl`，並將整理結果寫入根目錄的 `output/`。因此若使用自訂 `--output` 資料夾，請同時加上 `--no-postprocess`，避免後處理讀取到預設 `data` 中的舊紀錄。自訂輸出資料夾的後處理需另行安排。

## 9. 資料清理與使用限制

一般網頁正文擷取會優先使用文章或主要內容區塊，並嘗試排除常見導覽列、頁首頁尾、Cookie／同意提示、訂閱、分享、推薦、留言、彈窗及程式碼等雜訊。圖片處理會讀取一般及常見 Lazy Loading 屬性，嘗試排除 Logo、Banner、追蹤像素等圖片，並記錄來源網址、圖說及下載狀態。

不同網站的版型、網路狀況與存取限制可能影響正文清理、字幕取得及圖片下載結果，重要成果仍應人工抽查。

請只對有權下載及處理的媒體使用音訊下載功能。程式不應用來繞過登入、付費牆、驗證碼或其他存取限制。

## 10. 來源清單與成果文件

- `urls.csv`：原始網址清單。專案目前版本含 32 筆來源列；作業說明列出 31 個指定網址。正式使用前，請依教師提供的原始清單確認應保留的筆數及網址正確性。
- `RESULTS.md`：執行成果摘要。
- `report/report.md`：作業任務對應、執行統計、清理策略與限制。
- `report/validation.md`：驗證紀錄。
- `PROJECT_CLEANUP.md`：專案整理紀錄。

每次重新執行爬蟲後，輸出統計可能改變。正式報告中的筆數應以該次實際產生的資料為準，並同步檢查及更新成果文件。
