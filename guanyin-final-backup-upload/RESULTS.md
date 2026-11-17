# 擷取結果摘要

本檔案記錄本 ZIP 內所附資料的執行快照；重新執行爬蟲或後處理後，統計數字可能改變。正式報告請以同一次執行產生的 CSV／JSONL 為準。

## 本 ZIP 所附資料快照

- `urls.csv`：32 筆資料列，去除完全相同網址後為 31 個唯一來源。
- `data/crawl_status.csv`：共 71 筆處理紀錄；31 筆指定來源全部成功，40 筆延伸頁面中 39 筆成功、1 筆無法存取。
- `output/index.jsonl`：71 筆結構化紀錄，其中 68 筆一般網頁、3 筆 YouTube 影片。
- `output/crawl_report.csv`：71 筆報表紀錄，70 筆成功、1 筆無法存取。
- `output/image_filter_report.csv`：119 筆圖片篩選判斷，其中 115 筆保留、4 筆排除。
- `data/fact_checks.csv`：3 筆地方知識查核紀錄。

## 狀態說明

`success` 表示該筆來源已成功處理；`partial` 表示只取得部分資料；`inaccessible` 表示網站或網路存取受限；`failed` 表示處理失敗，例如來源不存在。請以 `data/crawl_status.csv` 與 `output/crawl_report.csv` 的當次內容確認具體原因。

## 圖片篩選

圖片篩選報告 `output/image_filter_report.csv` 會記錄來源文章、圖片網址、保留／排除決策、判斷原因及 OCR 文字。圖片索引則保留下載狀態、來源網址、替代文字、圖說及雜湊等資訊。兩份檔案用途不同，統計時不應將篩選判斷筆數直接視為實際圖片檔案數。

## 其他成果文件

- `report/report.md`：作業實作報告。
- `report/validation.md`：任務驗收對照。
- `README.md`：安裝、執行、圖片篩選、OCR、輸出及覆蓋注意事項。
