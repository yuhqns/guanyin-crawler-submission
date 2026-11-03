# 專案整理紀錄

## 本次整理範圍

本次僅依指定需求移除舊版後處理程式 `old_postprocess.py`，並更新專案說明文件與套件需求檔。其他程式、資料、輸出成果及資料夾均不在本次刪除範圍內。

## 移除項目

- `old_postprocess.py`：舊版後處理程式。現行後處理入口為專案根目錄的 `postprocess.py`。

## 保留項目

- `crawler/crawler.py`：目前主要爬蟲執行入口。
- `crawler/transcribe.py`：主要流程使用的 Whisper 語音辨識模組。
- `crawler/transcribe_extra.py`：獨立語音辨識輔助腳本。
- `crawler/old_crawler.py`：舊版爬蟲參考程式，未在本次移除。
- `postprocess.py`：目前使用的後處理程式。
- `capture_comparisons.py`：網站清理前後對照素材產生工具。
- `urls.csv`、`data/`、`output/`、`report/` 及其他原有資料。

## 文件更新

- `README.md`：更新安裝、執行方式、主要程式用途、輸出資料、YouTube 音訊下載與 Whisper 流程，以及執行限制。
- `requirements.txt`：列出核心爬蟲與後處理所需套件。
- `requirements-transcription.txt`：列出 Whisper 語音辨識額外套件，並固定 PyAV 為 `18.1.0`。

## 目前執行方式

在專案根目錄執行：

```powershell
py -m crawler.crawler
```

爬蟲完成後會依預設流程自動執行 `postprocess.py`。如需停用自動後處理，可使用：

```powershell
py -m crawler.crawler --no-postprocess
```

## 注意事項與尚待確認

- 本次整理不代表已重新執行全部網路爬取流程。
- 影片代表畫面仍需要可合法處理的本機影片檔；只有音訊檔無法產生影片畫格。
- 目前 `urls.csv` 有 32 筆來源列，而作業說明列出 31 個指定網址，正式繳交前需對照教師原始清單確認。
- 重新執行爬蟲前，建議備份 `data/` 與 `output/`，並在執行後同步更新成果統計文件。
