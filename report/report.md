# 桃園市觀音區地方知識爬蟲實作報告

## 1. 專案目的

本專案把指定網址轉換成可重複產生的觀音區地方知識資料。流程由 `urls.csv` 讀取來源，分辨一般網頁與 YouTube，擷取標題、網站／頻道、正文、圖片候選、頁面連結與狀態，再輸出 JSONL、Markdown、圖片檔與 CSV 報表。程式不繞過登入、付費牆、驗證碼或平台存取限制。

## 2. 執行結果

| 項目 | 結果 |
|---|---:|
| 指定來源（CSV） | 32 筆，網址無重複 |
| 指定來源成功／部分／無法存取／找不到資源 | 24／0／6／2 |
| 一層延伸探索 | 39 頁（成功 25、部分 1、無法存取 13） |
| 結構化索引 | 71 筆（`output/index.jsonl`） |
| 有正文內容 | 48 筆 |
| 擷取頁面連結 | 6,353 筆 |
| 圖片檔 | 93 個實體檔；索引中有 205 筆圖片紀錄，成功保存 131 筆，下載失敗 74 筆 |
| YouTube | 3 支；2 支取得公開字幕 |

每個網址（包含失敗網址）都保留在 `data/crawl_status.csv` 與 `output/crawl_report.csv`。HTTP 401／403、伺服器錯誤、TLS 憑證、DNS、連線或逾時等無法讀取情況會標成 `inaccessible`，並記錄具體原因；HTTP 404 會標成 `failed` 並記錄「找不到資源」。失敗不會被標成成功，也沒有以推測內容補寫。

## 3. 對應作業任務

### 任務 1：網址讀取、分類與延伸探索

`crawler.py` 會清理網址片段、去除完全重複值、逐一處理 32 筆 CSV 來源，並以 `--max-depth`、`--max-extra`、`--per-domain` 與 `--delay` 控制延伸探索。延伸頁面記錄 `source_type=extension`、`depth` 與 `discovered_from`，方便回溯來源。

### 任務 2：正文清理

一般網頁優先使用正文抽取器；失敗時使用內建 HTML parser。清理前會排除語意標籤及常見 class/id 版位，包括導覽、頁首、頁尾、側欄、選單、麵包屑、Cookie／同意提示、訂閱、分享、推薦、留言、分頁、彈窗、script/style、表單與廣告區塊，再從清理後的 HTML 擷取正文，避免正文抽取器重新帶回頁面雜訊。標題、正文、條列、表格、作者與發表日期（頁面有提供時）會保留；作者與日期另存於索引欄位。`report/screenshots/01_wikipedia.png`、`02_government.png`、`03_official_temple.png` 提供百科、桃園市政府、官方寺廟三種網站類型的原始文字與清理後文字對照；`raw_01_wikipedia.png`、`raw_02_government.png`、`raw_03_official_temple.png` 是同一批來源的瀏覽器畫面截圖。文字對照圖與瀏覽器畫面分開命名，便於驗收清理前後差異。

### 任務 3：圖片

圖片候選會讀取 `src`、`data-src`、`data-original`、`srcset` 等欄位，並以網址、替代文字、圖說、檔案雜湊去重。會排除 logo、banner、icon、追蹤像素、分享按鈕等常見無關項目，並限制檔案大小與最小尺寸。每筆圖片在 `output/index.jsonl` 保留原始網址、文章 ID、替代文字、圖說、SHA-256 與下載狀態。

### 任務 4：影片文字與代表畫面

YouTube 僅使用可公開取得的影片資訊與字幕，不下載影片本體。兩支影片有逐段字幕與時間戳，輸出在 `output/videos/video_001.md` 與 `video_002.md`；第三支影片及無字幕情況會記錄原因。執行環境可取得影片縮圖，但 YouTube 播放器回傳錯誤 153，沒有取得可驗證的串流畫格，因此三個 `video_*_frames/README.md` 明確標示沒有時間碼畫格；縮圖不冒充代表畫面。

### 任務 5：結構化資料

每筆索引採一致欄位：`id`、`type`、`title`、`source_url`、`source_name`、`content`、`images`、`links`、`timestamps`、`crawl_status`、`crawled_at`，並附 `author`、`published_at`、`topic`、`keywords`、`depth`、`source_type`、解析備註等欄位。文章 Markdown 會以相對路徑連到圖片檔與原始圖片網址；無法取得圖片或畫格時會保留空欄與原因，不以其他檔案冒充。

### 任務 6：去重、分類與查證

`postprocess.py` 使用文字相似度找出高度相似文章並標記 `duplicate_group`，以關鍵詞將資料分為自然景觀與生態、歷史文化與信仰、地方產業與農漁業、觀光景點與活動，並產生每篇 3–5 個關鍵字。三筆地方知識查核結果見 `data/fact_checks.csv`：甘泉寺位置與傳說、蓮花園休閒農業區資訊、白沙岬燈塔景點資訊。報告把「相傳」與官方頁面列出的資訊分開保存，不把 AI 推測當成查證事實。

## 6. 任務驗收摘要

| 任務 | 已完成的可重現功能 | 目前限制與紀錄 |
|---|---|---|
| 1 | 32 筆 CSV 來源全部進入爬取紀錄，含 39 筆一層延伸連結與存取原因 | 題目圖片標示 31 筆，CSV 實際有 32 筆，兩者差異已在 README 與 RESULTS 說明 |
| 2 | 三種網站原始文字／清理文字及三張瀏覽器原始畫面；清理導覽、Cookie、分享、推薦、留言、廣告與程式碼版位 | 不同網站版型仍可能需要人工抽查，原始與清理檔案均保留 |
| 3 | 一般與 lazy-loading 圖片候選、來源、替代文字、圖說、雜湊、下載狀態與去重 | 不能合法或實際下載的圖片保留失敗原因 |
| 4 | 3 支影片均保存標題、網址、頻道、長度與字幕狀態；2 支保存逐段時間戳字幕 | YouTube 播放器回傳錯誤 153，未取得可驗證串流畫格；三個 frames README 明確記錄，未用縮圖冒充 |
| 5 | `output/index.jsonl` 71 筆具一致欄位，Markdown 可追到來源、圖片與連結 | 無法存取內容的欄位保留空值與原因 |
| 6 | 相似文章分組、四類主題、每篇 3–5 個關鍵字、3 筆查核紀錄 | 查核只對保存且可交叉驗證的敘述，不把推測改寫成事實 |

## 4. 執行方式

```powershell
cd guanyin-crawler
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py crawler.py
py postprocess.py --image-limit 3
py capture_comparisons.py
```

小規模檢查可用 `py crawler.py --limit 3 --no-extend`；正式執行會重建 `data/` 與 `output/` 的成果，請保留 `urls.csv` 作為來源清單。

## 5. 限制與可重現性

網站內容會隨時間變動；403、404、TLS 憑證、需要瀏覽器互動或影片沒有公開字幕的來源，會依當次回應記錄狀態。圖片是否能下載也取決於原站回應。這些限制都保留在 CSV 與 JSONL 中，重跑時可辨識差異。
