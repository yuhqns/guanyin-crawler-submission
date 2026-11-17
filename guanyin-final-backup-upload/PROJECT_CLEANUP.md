# 專案整理紀錄

## 本次文件整理範圍

本次依目前專案實際檔案與程式入口，更新 README、成果摘要及驗收文件中的執行方式、模組用途、圖片篩選與 OCR 說明。未修改爬蟲、後處理、圖片篩選或語音辨識程式碼，也未新增專案功能。

## 目前主要程式

- `crawler/crawler.py`：主要爬蟲入口，讀取 `urls.csv`、處理一般網頁與 YouTube，執行有限延伸探索，並預設在結束後啟動後處理。
- `crawler/image_filter.py`：圖片中繼資料、尺寸、SVG 與選用 OCR 篩選。
- `crawler/transcribe.py`：主要 YouTube 流程使用的 faster-whisper 本機音訊辨識。
- `postprocess.py`：讀取 `data/records.jsonl` 與 `data/crawl_status.csv`，下載／篩選圖片、擷取可用本機影片畫面，輸出 Markdown、索引及報表。
- `crawler/subtitle_ocr.py`：Apple Vision 影片畫面字幕 OCR 的獨立工具。
- `crawler/transcribe_api.py`：MercuryAPI Whisper large-v3-turbo 獨立轉錄工具。
- `crawler/transcribe_extra.py`：獨立語音辨識輔助腳本。
- `capture_comparisons.py`：產生網頁清理前後對照素材。

## 執行方式

建立環境並安裝套件：

```cmd
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m pip install -r requirements-transcription.txt
```

完整爬取與後處理：

```cmd
python crawler\crawler.py
```

只重新執行後處理並設定每篇文章圖片上限：

```cmd
python postprocess.py --image-limit 3 --frame-count 5
```

OCR 為選用功能；除安裝 `pytesseract` 外，Windows 亦須安裝 Tesseract OCR，並確認 `chi_tra` 語言資料可用。

## 圖片上限說明

`postprocess.py` 的 `--image-limit` 控制每篇文章最多處理的圖片候選數量，預設為 3。例如：

```cmd
python postprocess.py --image-limit 6
```

目前 `crawler.py` 自動啟動後處理時只傳遞 `--frame-count`，不會轉傳 `--image-limit`。若需自訂圖片上限，請先執行：

```cmd
python crawler\crawler.py --no-postprocess
python postprocess.py --image-limit 6
```

## 主要輸出

- `data/records.jsonl`：原始爬取紀錄。
- `data/crawl_status.csv`：來源處理狀態與原因。
- `output/articles/`、`output/videos/`：文章及影片 Markdown。
- `output/images/`：篩選後保存的圖片。
- `output/index.jsonl`：結構化索引。
- `output/crawl_report.csv`：爬取與影片畫面報表。
- `output/image_filter_report.csv`：圖片篩選決策及 OCR 文字。
- `report/`：作業報告、驗證與截圖。

## 覆蓋與限制

- 重新執行爬蟲會重建指定輸出資料夾中的 `records.jsonl` 與 `crawl_status.csv`。
- 後處理會重建其管理的 `output/` 成果。執行前應先備份需要保留的資料。
- 後處理固定讀取專案根目錄 `data/`，自訂 crawler `--output` 時應搭配 `--no-postprocess`。
- 本次文件更新不代表重新執行網路爬取；實際成功／失敗筆數以當次 CSV 報表為準。
