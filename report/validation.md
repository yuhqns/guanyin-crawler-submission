# 任務一到六驗收對照

本頁將作業文件中的要求對照目前程式與成果，方便現場展示。

## 任務 1

- `urls.csv` 的 32 筆網址會去除完全重複值後逐筆處理；32 筆指定來源都出現在 `data/crawl_status.csv`。
- 一般網頁與 YouTube 會分流處理，保存標題、原始網址、網站／頻道名稱、正文與連結。
- 觀音相關連結以 `max-depth`、`max-extra`、`per-domain` 限制，實際保存 39 筆一層延伸頁面。
- `success`、`partial`、`inaccessible`、`failed` 及具體原因會同步保存到爬取報表。

## 任務 2

- 清理器會移除語意上的導覽、頁首、頁尾、側欄、script/style、彈窗與表單，以及 class/id 所標示的選單、麵包屑、Cookie、訂閱、分享、推薦、留言、分頁與廣告版位。
- 保留標題、正文、條列、表格、圖片圖說、作者與發表日期；作者和日期會寫入索引欄位。
- `report/screenshots/` 有百科、桃園市政府、甘泉寺官方網站三種原始 HTML／清理文字對照，以及三張 `raw_*.png` 瀏覽器原始畫面。

## 任務 3

- 支援 `src`、`data-src`、`data-original`、`data-lazy-src`、`data-image`、`data-url` 與 `srcset`／`data-srcset`。
- 圖片保存原始網址、文章 ID、替代文字、圖說、雜湊與下載狀態；logo、banner、icon、追蹤像素及分享圖片會排除。
- 同一網址或同一 SHA-256 不會重複保存；沒有可保存圖片時保留空陣列或失敗原因。

## 任務 4

- 3 支影片均保存標題、網址、頻道、長度、字幕狀態與縮圖來源。
- 2 支影片保存逐段字幕與時間戳；無字幕影片記錄原因。
- 已嘗試公開播放器畫格擷取，但 YouTube 回傳錯誤 153；未下載影片或繞過限制，三個 `video_*_frames/README.md` 均保留原因，縮圖不冒充代表畫格。

## 任務 5

- `output/index.jsonl` 共 71 筆，具備 `id`、`type`、`title`、`source_url`、`source_name`、`content`、`images`、`links`、`timestamps`、`crawl_status`、`crawled_at`，並附主題、關鍵字、作者、日期、深度與來源類型。
- Markdown 文章可追到原始網址、清理正文、圖片檔與圖片原始網址。

## 任務 6

- 以文字相似度產生 `duplicate_group`；目前偵測到 1 組高度相似資料。
- 每筆資料依自然景觀與生態、歷史文化與信仰、地方產業與農漁業、觀光景點與活動等主題分類，並產生 3–5 個關鍵字。
- `data/fact_checks.csv` 有 3 筆地方知識查核，保留聲稱、評估、來源與查核狀態；傳說內容維持「相傳」措辭，不將 AI 推測寫成已確認事實。
