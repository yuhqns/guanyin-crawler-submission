# 清理對照圖說明

- `raw_01_wikipedia.png`：維基百科目前可取得的瀏覽器畫面截圖。
- `raw_02_government.png`、`raw_03_official_temple.png`：若執行環境有可用的 Chromium／Edge，`capture_comparisons.py` 會自動產生的瀏覽器畫面截圖。
- `01_wikipedia.png`：維基百科原始 HTML 文字與清理後正文的並排對照。
- `02_government.png`：桃園市政府官方首頁的原始 HTML 文字與清理後正文對照。
- `03_official_temple.png`：甘泉寺官方網站的原始 HTML 文字與清理後正文對照。

三張編號對照圖的左側是原始 HTML 文字擷取，右側是套用與爬蟲相同規則後的清理正文。`raw_*` 檔案才是瀏覽器畫面截圖；本次三個網站均已保存原始瀏覽器畫面。清理規則會保留文章或頁面核心資訊，排除導覽、社群分享、Cookie、推薦、彈窗、頁尾與程式碼雜訊。
