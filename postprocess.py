#!/usr/bin/env python3
"""Export crawled records as a searchable knowledge-base project."""
import argparse, csv, hashlib, json, re, time
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DATA, OUT, REPORT = ROOT / "data", ROOT / "output", ROOT / "report"
UA = "GuanyinKnowledgeCrawler/1.0 (public-source research)"
BAD = re.compile(r"(logo|banner|avatar|tracking|pixel|icon|sprite|share|facebook|advert|qrcode|qr-code)", re.I)
NOISE_LINE = re.compile(
    r"^(?:::|首頁|主選單|選單|跳到主要內容區塊|跳至內容|移至側邊欄|隱藏|登入|註冊|"
    r"Facebook|關於我們|聯絡我們|隱私權政策|Cookie|同意|訂閱|推薦文章|相關文章|"
    r"最新消息|重要資訊|線上解籤|線上點燈|VR尋燈導覽|了解更多|回到頂端|"
    r"農遊好所在|農遊熱搜|發燒話題|農遊行事曆|農遊月報|社群精選|人氣票選|"
    r"農遊魅力|四季農遊推薦|農遊行程|精選專欄|熱門標籤|主題玩法|農遊吃喝)$",
    re.I,
)
EXTS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif", "image/svg+xml": ".svg"}
TOPICS = {
    "自然景觀與生態": ("草漯沙丘", "藻礁", "海岸", "沙丘", "濱海", "燈塔", "濕地", "生態", "林道"),
    "歷史文化與信仰": ("甘泉寺", "石觀音", "古厝", "歷史", "文化", "信仰", "受信所", "客家", "石滬", "牽罟"),
    "地方產業與農漁業": ("蓮花", "蓮藕", "農業", "農場", "漁港", "漁業", "農產", "鋼構", "發電廠", "工業", "蜂蜜"),
    "觀光景點與活動": ("旅遊", "景點", "觀光", "一日遊", "活動", "遊程", "休閒區", "季", "餐廳", "農遊"),
}
KEYWORDS = ("觀音區", "桃園市", "草漯沙丘", "藻礁", "白沙岬燈塔", "甘泉寺", "石觀音", "崙坪文化地景園區", "蓮花園休閒農業區", "蓮花季", "客家文化", "石滬", "海岸景觀", "大潭發電廠", "觀音坑溪", "大堀溪", "永安漁港", "地方產業", "歷史文化", "生態保育", "農業旅遊")

def access_status(status, reason, http_status=''):
    """Normalize access failures, including reports produced by older runs."""
    if status not in ('failed', 'inaccessible'):
        return status, reason
    detail = str(reason or '')
    if status == 'inaccessible' and not detail.startswith('無法存取：'):
        return status, f'無法存取：{detail}' if detail else '無法存取：未提供錯誤原因'
    if status == 'inaccessible':
        return status, detail
    try:
        code = int(http_status) if http_status else 0
    except (TypeError, ValueError):
        code = 0
    if code >= 400 and code != 404 or re.search(
        r'HTTP\s+(401|403|408|429|5\d\d)|CERTIFICATE_VERIFY_FAILED|SSL:|timed? ?out|timeout|'
        r'getaddrinfo failed|Name or service not known|Connection(?:Error|Reset|Refused)|拒絕',
        detail, re.I):
        return 'inaccessible', f'無法存取：{detail}' if detail else '無法存取：未提供錯誤原因'
    if code == 404 or re.search(r'HTTP\s+404', detail, re.I):
        return 'failed', f'找不到資源：{detail}' if not detail.startswith('找不到資源：') else detail
    return status, detail

def jlines(path):
    if not path.exists(): return []
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]

def classify(title, content):
    s = f"{title} {content[:12000]}"
    topics = [k for k, terms in TOPICS.items() if any(t in s for t in terms)] or ["地方知識"]
    words = [t for t in KEYWORDS if t in s][:5]
    for term in ("地方旅遊", "觀光資訊", "在地資訊"):
        if len(words) >= 3: break
        if term not in words: words.append(term)
    return topics[:3], words[:5]

def clean_legacy_content(text):
    """Remove obvious standalone site chrome from records made by older runs."""
    if not text: return text
    kept=[]; previous=None
    for raw in text.splitlines():
        line=raw.strip()
        if not line or NOISE_LINE.match(line):
            continue
        if line == previous:
            continue
        kept.append(line); previous=line
    return '\n'.join(kept).strip()

def duplicate_groups(rows):
    parent = list(range(len(rows)))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a
    texts = [re.sub(r"[\W_]+", "", f"{r['title']} {r['content']}")[:3500] for r in rows]
    for i in range(len(rows)):
        if len(texts[i]) < 280: continue
        for k in range(i+1, len(rows)):
            if len(texts[k]) >= 280 and SequenceMatcher(None,texts[i],texts[k]).ratio() >= .88:
                a,b=find(i),find(k)
                if a!=b: parent[b]=a
    found=defaultdict(list)
    for i in range(len(rows)): found[find(i)].append(i)
    return [g for g in found.values() if len(g)>1]

def save_images(rows, limit):
    folder=OUT/'images'; folder.mkdir(parents=True,exist_ok=True)
    hash_paths, url_cache, last={}, {}, {}
    for r in rows:
        kept=[]; selected=0
        for im in r.pop('_candidates',[]):
            url=im.get('url',''); host=urlparse(url).hostname or ''
            if not url or urlparse(url).scheme not in ('http','https') or BAD.search(url+' '+im.get('alt','')+' '+im.get('caption','')): continue
            if url in url_cache:
                kept.append(dict(url_cache[url])); continue
            if selected>=limit: continue
            delay=.2-(time.monotonic()-last.get(host,0))
            if delay>0: time.sleep(delay)
            try:
                req=Request(url,headers={'User-Agent':UA,'Accept':'image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8'})
                with urlopen(req,timeout=18) as resp:
                    typ=resp.headers.get_content_type().lower()
                    if not typ.startswith('image/'): raise ValueError('回應不是圖片：'+typ)
                    data=resp.read(8*1024*1024+1)
                last[host]=time.monotonic()
                if len(data)>8*1024*1024: raise ValueError('圖片超過 8 MB')
                ext=EXTS.get(typ)
                if not ext: raise ValueError('不支援的圖片格式：'+typ)
                if ext != '.svg':
                    try:
                        from io import BytesIO
                        from PIL import Image
                        with Image.open(BytesIO(data)) as image:
                            if image.width < 100 or image.height < 70:
                                raise ValueError(f'圖片尺寸過小：{image.width}x{image.height}')
                    except ImportError:
                        pass
                digest=hashlib.sha256(data).hexdigest()
                duplicate=digest in hash_paths
                if duplicate: file=hash_paths[digest]
                else:
                    name=f"{r['id']}_image_{selected+1:02d}{ext}"
                    (folder/name).write_bytes(data); file='images/'+name; hash_paths[digest]=file
                item={'file':file,'source_url':url,'alt':im.get('alt',''),'caption':im.get('caption',''),'sha256':digest,'download_status':'duplicate' if duplicate else 'saved'}
                url_cache[url]=item; kept.append(item); selected+=1
            except Exception as e:
                last[host]=time.monotonic(); kept.append({'file':'','source_url':url,'alt':im.get('alt',''),'caption':im.get('caption',''),'download_status':'failed','reason':f'{type(e).__name__}: {e}'})
        r['images']=kept

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--image-limit',type=int,default=3); ap.add_argument('--no-images',action='store_true'); args=ap.parse_args()
    raw=jlines(DATA/'records.jsonl')
    with (DATA/'crawl_status.csv').open(encoding='utf-8-sig',newline='') as f: statuses=list(csv.DictReader(f))
    for item in statuses:
        item['status'], item['reason'] = access_status(item.get('status',''), item.get('reason',''), item.get('http_status',''))
    status={r['url']:r for r in statuses}; byurl={r.get('source_url'):r for r in raw}
    with (ROOT/'urls.csv').open(encoding='utf-8-sig',newline='') as f: urls=[x['url'].strip() for x in csv.DictReader(f) if x.get('url','').strip()]
    urls += [s['url'] for s in statuses if s['url'] not in set(urls)]
    urls=list(dict.fromkeys(urls)); OUT.mkdir(exist_ok=True); REPORT.mkdir(exist_ok=True)
    # Rebuild only files owned by this exporter so reruns do not leave stale page IDs.
    for pattern in ('page_*.md',):
        for old in (OUT/'articles').glob(pattern): old.unlink()
    for old in (OUT/'videos').glob('video_*.md'): old.unlink()
    for old in (OUT/'videos').glob('video_*_frames'):
        for f in old.glob('*'):
            if f.is_file(): f.unlink()
        old.rmdir()
    for pattern in ('page_*_image_*','video_*_image_*'):
        for old in (OUT/'images').glob(pattern): old.unlink()
    for x in ('articles','videos','images'): (OUT/x).mkdir(parents=True,exist_ok=True)
    page_n=video_n=0; rows=[]
    for url in urls:
        st=status.get(url,{}); src=byurl.get(url,{})
        parsed=urlparse(url); host=(parsed.hostname or '').lower(); path=parsed.path.lower()
        video=(host=='youtu.be' or (host.endswith('youtube.com') and (path.startswith('/shorts/') or path.startswith('/watch') or path.startswith('/live/') or path.startswith('/embed/'))))
        if video: video_n+=1; ident=f'video_{video_n:03d}'
        else: page_n+=1; ident=f'page_{page_n:03d}'
        title=src.get('title') or st.get('title') or ''; body=src.get('content','')
        topics, keywords=classify(title,body)
        rows.append({'id':ident,'type':'video' if video else 'webpage','title':title,'source_url':url,
          'source_name':src.get('channel') if video else src.get('site_name',''),'content':body,
          'author':src.get('author',''),'published_at':src.get('published_at',''),
          'images':[],'_candidates':src.get('images',[]),'links':src.get('links',[]),
          'timestamps':src.get('transcript_segments',[]),'crawl_status':st.get('status','failed'),
          'crawl_reason':st.get('reason',''),'crawled_at':src.get('fetched_at') or st.get('crawled_at',''),'topic':topics,'keywords':keywords,
          'depth':int(st.get('depth') or 0),'source_type':st.get('source_type','seed'),'final_url':st.get('final_url',''),
          'http_status':st.get('http_status',''),'parser_note':src.get('extraction_note',''),
          'duration_seconds':src.get('duration_seconds'),'caption_available':src.get('caption_available',False),
          'caption_note':src.get('caption_note','')})
    groups=duplicate_groups(rows)
    for n,g in enumerate(groups,1):
        for i in g: rows[i]['duplicate_group']=f'dup_{n:03d}'
    if not args.no_images: save_images(rows,max(0,args.image_limit))
    else:
        for r in rows:
            r['images']=[{'file':'','source_url':x.get('url',''),'alt':x.get('alt',''),'caption':x.get('caption',''),'download_status':'not_downloaded'} for x in r.pop('_candidates',[])[:args.image_limit]]
    index=OUT/'index.jsonl'
    with index.open('w',encoding='utf-8') as f:
        for r in rows: f.write(json.dumps(r,ensure_ascii=False)+'\n')
    for r in rows:
        if r['type']=='webpage':
            f=OUT/'articles'/f"{r['id']}.md"
            lines=[f"# {r['title'] or r['id']}", '',f"- 網站：{r['source_name'] or '未知'}",f"- 原始網址：{r['source_url']}",f"- 狀態：{r['crawl_status']}"]
            if r.get('author'): lines.append(f"- 作者：{r['author']}")
            if r.get('published_at'): lines.append(f"- 發表日期：{r['published_at']}")
            if r.get('crawl_reason'): lines.append(f"- 原因：{r['crawl_reason']}")
            lines += [f"- 主題：{'、'.join(r['topic'])}",f"- 關鍵字：{'、'.join(r['keywords'])}",f"- 擷取時間：{r['crawled_at']}",'','## 清理後正文','',r['content'] or '未取得正文，請查看 crawl_report.csv。','','## 圖片']
            ims=[i for i in r['images'] if i.get('file')]
            lines += [f"- ![{i.get('alt') or '文章圖片'}](../{i['file']}) — [原始圖片來源]({i['source_url']}) {i.get('caption','')}" for i in ims] or ['本次無成功保存的圖片。']
            lines += ['','## 頁面連結']+[f"- [{i.get('text') or i['url']}]({i['url']})" for i in r['links'][:100]]
            if r['parser_note']: lines+=['',f"> 備註：{r['parser_note']}"]
            f.write_text('\n'.join(lines)+'\n',encoding='utf-8')
        else:
            folder=OUT/'videos'/f"{r['id']}_frames"; folder.mkdir(exist_ok=True)
            (folder/'README.md').write_text('本次未擷取影片畫格；保存的圖片若有，為影片縮圖而非時間碼畫格。\n',encoding='utf-8')
            lines=[f"# {r['title'] or r['id']}",'',f"- 頻道：{r['source_name'] or '未知'}",f"- 原始網址：{r['source_url']}",f"- 狀態：{r['crawl_status']}"]
            if r.get('crawl_reason'): lines.append(f"- 原因：{r['crawl_reason']}")
            lines += [f"- 時長（秒）：{r['duration_seconds'] or '未知'}",'','## 字幕逐字稿']
            if r['timestamps']: lines += [f"- **[{s['timestamp']}]** {s['text']}" for s in r['timestamps']]
            elif r['content']: lines += [r['content']]
            else: lines += [r['caption_note'] or '沒有取得可用字幕；未使用語音辨識補寫。']
            lines += ['','## 代表畫面','沒有取得可驗證時間碼的影片畫格；請勿將影片縮圖當成代表畫格。']
            for im in r['images']:
                if im.get('file'): lines.append(f"- [影片縮圖](../{im['file']}) — {im['source_url']}")
            (OUT/'videos'/f"{r['id']}.md").write_text('\n'.join(lines)+'\n',encoding='utf-8')
    fields=['id','type','url','title','status','http_status','reason','depth','crawled_at']
    with (OUT/'crawl_report.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in rows: w.writerow({'id':r['id'],'type':r['type'],'url':r['source_url'],'title':r['title'],'status':r['crawl_status'],'http_status':r['http_status'],'reason':r['crawl_reason'] or r['caption_note'],'depth':r['depth'],'crawled_at':r['crawled_at']})
    print(f"Exported {len(rows)} records into {OUT}")

if __name__=='__main__': main()
