# 桃園市觀音區地方知識爬蟲

本專案以 Python 建立可重複執行的觀音區地方知識爬蟲。程式從 `urls.csv` 讀取指定來源，擷取一般網頁與 YouTube 資訊，並依設定進行有限的相關連結延伸探索。爬取後由 `postprocess.py` 整理文章、圖片、影片資料、結構化索引及報表。

## 1. 功能概覽

### 一般網頁
- 擷取標題、網站名稱、作者、日期、正文及頁面連結。
- 支援一般 `img src`、常見 Lazy Loading 屬性及 `srcset`／`data-srcset`。
- 擷取圖片候選網址，後處理時下載、去重並記錄圖片來源、替代文字、圖說與下載狀態。
- 清理常見導覽列、頁首頁尾、Cookie、訂閱、分享、推薦、留言、廣告等頁面雜訊。

### 圖片篩選
圖片下載及篩選由根目錄的 `postprocess.py` 呼叫 `crawler/image_filter.py` 執行。現有判斷包含：
- URL、`alt`、`title`、`class`、`id`、`role` 等中繼資料關鍵字。
- 廣告／訂閱等中繼資料關鍵字。
- 圖片尺寸及寬高比例。
- SVG 原始標記中的名稱／裝飾線索及小尺寸圖示判斷。
- 可選 OCR：對符合橫幅尺寸條件的點陣圖片辨識文字，搭配廣告關鍵字排除。

OCR 需要另外安裝 Python 套件 `pytesseract`、Tesseract OCR Windows 程式，以及繁體中文語言資料 `chi_tra`。若 OCR 環境未安裝完整，程式仍可執行其他圖片篩選，但不會取得 OCR 文字。OCR 目前不是對所有照片逐張辨識；它只會對符合尺寸／比例條件的圖片啟動，以控制處理時間。

### YouTube
每部影片分別保留：
1. YouTube 平台可取得的字幕。
2. `faster-whisper` 對音訊進行語音辨識的逐字稿。

即使平台字幕已存在，主要爬蟲流程仍會另外執行 Whisper。平台字幕與 Whisper 結果分開保存，後處理時分段呈現在同一部影片的 `video.md`。

代表畫面只從使用者提供、且有權處理的本機影片檔擷取；僅有音訊檔不能產生影片畫格。沒有可用本機影片時，程式會記錄未能擷取畫面的原因，不以 YouTube 縮圖冒充代表畫面。

### 影片分流與兩條處理路線
除了上面主爬蟲內建的流程，影片另有一組獨立工具，先分流再依字幕狀況處理（完整說明見 [`README_video.md`](README_video.md)）：
- **影片分流器**：`python -m crawler.video_triage`，只讀影片資訊、不下載，輸出 `output/video_triage.csv`（上傳者字幕／只有自動字幕／沒有字幕／無法判斷／網頁）。
- **有字幕**：取上傳者字幕 → 挑重點 → 在重點時間點截圖，輸出 `<編號>_highlights.md`。
- **沒有字幕**：FunASR 兩個模型（Paraformer-zh、SenseVoice-Small）語音辨識，用 Apple Vision 畫面字幕 OCR 驗證兩者不同之處，不確定的地方產生網頁讓人工確認。
- **只有自動字幕**：抓下自動字幕，並照沒有字幕的做法處理，最後兩邊比較。
- `python -m crawler.video_pipeline status` 可以看每支影片走到哪一步、下一步該執行什麼。

## 2. 環境需求

- Windows 10／11
- Python 3.10 或更新版本
- FFmpeg（YouTube 音訊轉換及本機影片畫格擷取需要）
- 一般爬蟲套件：見 `requirements.txt`
- Whisper 套件：見 `requirements-transcription.txt`
- OCR（選用）：Tesseract OCR、繁體中文 `chi_tra` 語言資料及 Python 套件 `pytesseract`

先確認 FFmpeg 可在終端機執行：

```cmd
ffmpeg -version
```

### 2.1 安裝 OCR（選用）

1. 安裝 Tesseract OCR for Windows。可使用 UB Mannheim 提供的 Windows 安裝程式。
2. 安裝時確認包含繁體中文語言資料 `chi_tra`；若未安裝，需將對應的 `chi_tra.traineddata` 放入 Tesseract 的 `tessdata` 資料夾。
3. 將 Tesseract 安裝目錄加入 Windows `PATH`，常見位置為 `C:\Program Files\Tesseract-OCR`。
4. 在本專案使用的 Python 環境安裝：

```cmd
python -m pip install pytesseract
```

5. 驗證：

```cmd
tesseract --list-langs
python -c "import shutil,pytesseract; print(shutil.which('tesseract')); print(pytesseract.get_tesseract_version())"
```

語言清單應包含 `chi_tra`。若 `pytesseract` 安裝在不同 Python 虛擬環境，執行爬蟲時也必須啟用同一個環境。

## 3. 安裝專案

以下以 Windows CMD 為例，所有指令均在專案根目錄執行。

```cmd
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -r requirements-transcription.txt
```

若要啟用 OCR，再執行：

```cmd
python -m pip install pytesseract
```

## 4. 執行方式

### 4.1 完整流程

```cmd
python crawler\crawler.py
```

預設會讀取 `urls.csv`，執行一般網頁／YouTube 爬取、有限延伸探索，最後自動執行 `postprocess.py`。

### 4.2 常用範例

只測試前三筆指定來源，不做延伸探索：

```cmd
python crawler\crawler.py --limit 3 --no-extend
```

調整延伸探索：

```cmd
python crawler\crawler.py --max-depth 2 --max-extra 60 --per-domain 6 --delay 2
```

只執行爬取，不自動執行後處理：

```cmd
python crawler\crawler.py --no-postprocess
```

停用 YouTube 音訊下載：

```cmd
python crawler\crawler.py --no-download-audio
```

指定本機影片資料夾，供 Whisper 及代表畫面擷取使用：

```cmd
python crawler\crawler.py --media-dir local_videos
```

切換 faster-whisper 模型：

```cmd
python crawler\crawler.py --model large-v3
```

目前 `crawler/transcribe.py` 使用 CPU + INT8。指定 `large-v3` 只會更換模型，不會自動啟用 NVIDIA GPU；GPU 推論需要另行配置相容的 CUDA、cuDNN 與 CTranslate2 環境並修改裝置設定。

### 4.3 單獨重新執行後處理

若 `data/records.jsonl` 已經存在，只想重新整理文章與圖片，不必重新爬取：

```cmd
python postprocess.py --image-limit 3 --frame-count 5
```

`--image-limit` 是**每篇文章最多下載／保留的圖片候選數量**，預設為 `3`。例如每篇最多 6 張：

```cmd
python postprocess.py --image-limit 6 --frame-count 5
```

停用圖片下載：

```cmd
python postprocess.py --no-images
```

指定本機影片資料夾擷取代表畫面：

```cmd
python postprocess.py --media-dir local_videos --frame-count 5
```

注意：`--image-limit` 是 `postprocess.py` 的參數，不是 `crawler.py` 的參數。若透過 `crawler.py` 自動後處理，該入口目前只傳入 `--frame-count`，圖片上限使用 `postprocess.py` 預設值 3。要自行調整圖片上限，請先使用 `--no-postprocess` 完成爬取，再手動執行上述 `postprocess.py --image-limit N`。

### 4.4 影片工具與測試

影片分流、有字幕／沒字幕路線的指令與說明見 [`README_video.md`](README_video.md)。需要的額外環境：`yt-dlp`、FFmpeg；沒字幕路線另需 FunASR 環境（`pip install -r requirements-funasr.txt`，建議另開虛擬環境）與 macOS（畫面字幕 OCR）。

影片工具的單元測試（不連網）：

```bash
python -m unittest discover -s tests
```

## 5. 命令列參數

### `crawler/crawler.py`

| 參數 | 說明 | 預設值 |
|---|---|---|
| `--input` | 輸入網址 CSV | `urls.csv` |
| `--output` | 原始資料輸出資料夾 | `data` |
| `--max-depth` | 延伸探索深度（0～3） | `1` |
| `--max-extra` | 額外延伸頁面總數上限 | `40` |
| `--per-domain` | 每個網域的延伸頁面上限 | `5` |
| `--delay` | 同網域請求間隔秒數 | `1.5` |
| `--timeout` | 單一請求逾時秒數 | `20` |
| `--limit` | 原始網址處理筆數；0 表示全部 | `0` |
| `--media-dir` | 本機影音檔資料夾 | 未指定 |
| `--download-audio` | 啟用 YouTube 音訊下載 | 預設啟用 |
| `--no-download-audio` | 停用音訊下載 | — |
| `--model` | faster-whisper 模型名稱 | `small` |
| `--frame-count` | 每部本機影片擷取畫面數（3、4 或 5） | `5` |
| `--no-postprocess` | 不自動執行後處理 | 未啟用 |
| `--no-extend` | 停用延伸探索 | 未啟用 |

查看完整參數：

```cmd
python crawler\crawler.py --help
```

### `postprocess.py`

| 參數 | 說明 | 預設值 |
|---|---|---|
| `--image-limit` | 每篇文章圖片上限 | `3` |
| `--no-images` | 不下載圖片 | 未啟用 |
| `--media-dir` | 本機影片資料夾 | 未指定 |
| `--frame-count` | 每部本機影片擷取畫面數（3、4 或 5） | `5` |
| `--ffmpeg` | FFmpeg 執行檔名稱或路徑 | `ffmpeg` |
| `--ffprobe` | FFprobe 執行檔名稱或路徑 | `ffprobe` |

查看完整參數：

```cmd
python postprocess.py --help
```

## 6. YouTube 處理流程

### 6.1 平台字幕
1. 使用 `yt-dlp` 取得影片資訊及可公開取得的字幕軌。
2. 依程式設定的語言順序選取可讀字幕。
3. 保存字幕文字及時間戳記。

### 6.2 Whisper 語音辨識
1. 優先尋找 `--media-dir` 中檔名以 YouTube Video ID 開頭的本機影音檔。
2. 若沒有本機檔案且音訊下載功能啟用，使用 `yt-dlp` 下載音訊。
3. 使用 FFmpeg 轉成 16 kHz、單聲道 WAV。
4. 使用 `faster-whisper` 執行辨識，預設模型 `small`、CPU + INT8。
5. 將逐字稿與時間戳記獨立保存。

預設快取位置：

- 下載音訊：`data/audio_work/downloads/`
- 轉換後 WAV：`data/audio_work/audio/`

### 6.3 其他獨立工具
- `crawler/subtitle_ocr.py`：使用 Apple Vision 對影片畫面中的字幕進行 OCR，不依賴語音辨識（作為語音辨識的驗證來源）；能辨識多種字幕樣式（例如左下小字與底部大字），讀不準的句子會標「待確認」。需在支援 Apple Vision 的 macOS 環境執行，並非 Windows 主要爬取流程的必要步驟。
- `crawler/transcribe_funasr.py`：沒有字幕的影片用 FunASR 兩個模型各辨識一次，兩邊不同處以畫面字幕 OCR 驗證，並套用 `data/glossary.md` 的專有名詞。套件見 `requirements-funasr.txt`。
- `crawler/review_page.py`：把需要人工確認的地方做成單一網頁（附影片截圖、兩個模型與畫面字幕的讀法），選完後產生確認版逐字稿。`*_review.html` 內含影片截圖，已被 `.gitignore` 排除。
- `crawler/video_triage.py`：影片分流器，判斷每個網址的類型與字幕狀況，輸出 `output/video_triage.csv`。
- `crawler/subtitle_highlights.py`：有字幕影片取字幕、切段、依重點截圖；也可用 `--auto` 抓自動字幕。
- `crawler/caption_compare.py`：只有自動字幕的影片，比較自動字幕與 FunASR 的差異。
- `crawler/video_pipeline.py`：影片總入口，`status` 盤點各影片進度與下一步指令。
- `crawler/transcribe_api.py`：使用 MercuryAPI 的 Whisper large-v3-turbo 進行獨立音訊轉錄。需要設定 `MERCURY_API_KEY`，不屬於主要爬蟲預設流程。
- `.env.example` 提供環境變數格式範例。請複製為 `.env` 並自行填入金鑰；不要將 `.env` 或金鑰提交到版本控制。

## 7. 專案結構

```text
guanyin-final-backup/
├─ crawler/
│  ├─ __init__.py
│  ├─ crawler.py                 # 主要爬蟲入口
│  ├─ image_filter.py            # 圖片中繼資料、尺寸及 OCR 篩選
│  ├─ subtitle_ocr.py             # Apple Vision 影片字幕 OCR 工具
│  ├─ subtitle_ocr.swift          # Apple Vision Swift 輔助程式
│  ├─ transcribe.py               # faster-whisper 本機語音辨識
│  ├─ transcribe_api.py           # MercuryAPI 獨立語音辨識工具
│  ├─ transcribe_extra.py         # 獨立語音辨識輔助腳本
│  ├─ transcribe_funasr.py        # 沒字幕影片：FunASR 兩模型辨識＋畫面字幕驗證
│  ├─ review_page.py              # 人工確認網頁與確認版逐字稿
│  ├─ video_triage.py             # 影片分流器
│  ├─ subtitle_highlights.py      # 有字幕影片：取字幕、重點、截圖
│  ├─ caption_compare.py          # 自動字幕與 FunASR 比較
│  └─ video_pipeline.py           # 影片總入口（進度與下一步）
├─ tests/                         # 影片工具的單元測試
├─ data/
│  ├─ records.jsonl               # 爬蟲原始紀錄
│  ├─ crawl_status.csv            # 網址處理狀態
│  ├─ fact_checks.csv              # 地方知識查核資料
│  ├─ glossary.md                  # 專有名詞詞庫（FunASR 辨識用）
│  └─ audio_work/                  # YouTube 音訊快取
├─ output/
│  ├─ articles/                    # 一般網頁 Markdown
│  ├─ videos/                      # 影片資訊、逐字稿及畫面
│  ├─ images/                      # 下載後保留的圖片
│  ├─ index.jsonl                  # 結構化索引
│  ├─ crawl_report.csv             # 爬取與影片畫面報表
│  └─ image_filter_report.csv      # 圖片篩選決策與 OCR 文字
├─ report/                         # 作業報告、驗證及截圖
├─ urls.csv                         # 指定來源網址
├─ postprocess.py                  # 後處理、圖片下載與索引輸出
├─ capture_comparisons.py          # 網頁清理前後對照素材
├─ requirements.txt
├─ requirements-transcription.txt
├─ requirements-funasr.txt         # 沒字幕影片的 FunASR 套件（建議另開虛擬環境）
├─ README_video.md                 # 影片處理流程說明
├─ local_videos/                   # 本機影片（已被 .gitignore 排除，不會上傳）
├─ .env.example
├─ RESULTS.md
├─ PROJECT_CLEANUP.md
└─ README.md
```

## 8. 主要輸出資料

| 路徑 | 說明 |
|---|---|
| `data/records.jsonl` | 原始爬取資料，一行一筆 |
| `data/crawl_status.csv` | 原始來源與延伸頁面的狀態、HTTP 狀態碼及原因 |
| `data/fact_checks.csv` | 地方知識查核摘要 |
| `data/audio_work/downloads/` | yt-dlp 下載的音訊 |
| `data/audio_work/audio/` | FFmpeg 轉換後的 WAV |
| `output/articles/` | 一般網頁 Markdown |
| `output/videos/<record_id>/video.md` | 影片資訊、平台字幕及 Whisper 逐字稿 |
| `output/videos/` | 影片代表畫面及擷取說明 |
| `output/images/` | 實際保存的圖片檔 |
| `output/index.jsonl` | 後處理後的結構化索引 |
| `output/crawl_report.csv` | 網頁／影片爬取狀態及畫面擷取狀態 |
| `output/image_filter_report.csv` | 每張圖片的保留／排除判斷、原因及 OCR 文字 |
| `report/` | 作業報告、驗證紀錄及清理前後對照素材 |

## 9. 重新執行與覆蓋注意事項

- 主要爬蟲目前沒有 `--resume`／`--continue` 接續進度參數。
- 每次執行爬蟲時，指定的原始資料輸出資料夾內既有 `records.jsonl` 與 `crawl_status.csv` 會被重建；使用預設 `data` 時請先備份這兩個檔案及相關音訊快取。
- `postprocess.py` 會重建其管理的文章 Markdown、影片 Markdown／畫面資料、圖片輸出、`output/index.jsonl` 及報表。若要保留目前輸出，執行前請先備份 `output/`。
- `postprocess.py` 固定讀取專案根目錄 `data/records.jsonl` 與 `data/crawl_status.csv`，並寫入根目錄 `output/`。若 crawler 使用自訂 `--output`，請加上 `--no-postprocess`；自訂資料夾的資料不能直接由目前的後處理入口自動處理。
- 單獨調整圖片數量時，只需重新執行 `postprocess.py --image-limit N`，不必重新爬取網頁。

## 10. 網路、資料及使用限制

網站版型、網路狀況、TLS 憑證、HTTP 403／404、登入要求及平台政策都可能影響爬取結果。程式會保存狀態及可取得的錯誤原因，不會將未取得內容的頁面標記為成功，也不應用來繞過登入、付費牆、驗證碼或其他存取限制。

圖片篩選採保守策略：明確符合 Logo、網站介面或廣告線索的圖片會被排除；不會只因圖片缺少文章關鍵字就排除，以降低誤刪景點、建築、地圖及紀錄照片的風險。自動篩選仍可能有誤判，請抽查 `output/image_filter_report.csv` 及索引中的圖片紀錄。

## 11. 來源清單與成果文件

- `urls.csv` 目前有 32 筆資料列，去除完全相同的網址後為 31 個唯一來源；正式使用前仍應與教師提供的原始清單核對。
- `RESULTS.md` 記錄本 ZIP 所附資料的執行成果快照；重新爬取後，數量可能改變。
- `report/report.md` 為作業實作報告。
- `report/validation.md` 為任務驗收對照。
- `PROJECT_CLEANUP.md` 為專案文件與程式整理紀錄。

正式報告的統計數字應以同一次執行所產生的 `data/`、`output/` 報表為準。
