#!/usr/bin/env python3
"""Export crawled records as a searchable knowledge-base project."""
import argparse, csv, hashlib, json, re, time, shutil, subprocess
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
            # Older/raw records may contain JSON null for optional image
            # fields.  Normalize those values before string operations.
            url = str(im.get('url') or '').strip()
            alt = str(im.get('alt') or '')
            caption = str(im.get('caption') or '')
            host=urlparse(url).hostname or ''
            if not url or urlparse(url).scheme not in ('http','https') or BAD.search(url+' '+alt+' '+caption): continue
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


VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}


def format_seconds(seconds):
    whole = max(0, int(seconds))
    return f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"


def find_local_video(media_dir, video_id):
    """Find a user-provided video whose filename starts with its YouTube ID."""
    if not media_dir or not media_dir.is_dir() or not video_id:
        return None
    matches = [
        path for path in media_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTS
        and path.stem.startswith(video_id)
    ]
    return sorted(matches)[0] if matches else None


def probe_video_duration(media_path, ffprobe_bin):
    command = [
        ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(media_path),
    ]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    value = result.stdout.strip()
    duration = float(value)
    if duration <= 0:
        raise ValueError("影片長度必須大於 0 秒")
    return duration


def nearest_caption(segments, seconds, max_distance=30):
    """Return the nearest subtitle text when it is close enough to a frame."""
    candidates = []
    for segment in segments or []:
        try:
            start = float(segment.get("start_seconds", 0))
        except (TypeError, ValueError):
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            candidates.append((abs(start - seconds), start, text))
    if not candidates:
        return ""
    distance, _, text = min(candidates, key=lambda item: item[0])
    return text if distance <= max_distance else ""


def extract_local_video_frames(row, media_dir, frame_count, ffmpeg_bin, ffprobe_bin):
    """Extract evenly spaced stills from a user-provided local video only."""
    folder = OUT / "videos" / f"{row['id']}_frames"
    folder.mkdir(parents=True, exist_ok=True)
    video_id = str(row.get("video_id") or "")
    media_path = find_local_video(media_dir, video_id)
    if not media_path:
        note = (
            f"找不到本機影片檔。請將有權處理的影片放入 {media_dir or '指定資料夾'}，"
            f"並以 {video_id or 'YouTube video ID'} 開頭命名。"
        )
        (folder / "README.md").write_text(note + "\n", encoding="utf-8")
        row["video_frames"] = []
        row["frame_extraction_status"] = "missing_local_video"
        row["frame_extraction_note"] = note
        row["local_video_file"] = ""
        return

    if not shutil.which(ffmpeg_bin):
        note = f"找不到 FFmpeg 執行檔：{ffmpeg_bin}。請安裝 FFmpeg 並確認 ffmpeg 已加入 PATH。"
        (folder / "README.md").write_text(note + "\n", encoding="utf-8")
        row["video_frames"] = []
        row["frame_extraction_status"] = "ffmpeg_missing"
        row["frame_extraction_note"] = note
        row["local_video_file"] = media_path.name
        return
    if not shutil.which(ffprobe_bin):
        note = f"找不到 FFprobe 執行檔：{ffprobe_bin}。請確認 FFmpeg 安裝包含 ffprobe 並已加入 PATH。"
        (folder / "README.md").write_text(note + "\n", encoding="utf-8")
        row["video_frames"] = []
        row["frame_extraction_status"] = "ffprobe_missing"
        row["frame_extraction_note"] = note
        row["local_video_file"] = media_path.name
        return

    try:
        # Use the local file's actual duration; a locally trimmed copy may
        # differ from the duration reported by the online video metadata.
        duration = probe_video_duration(media_path, ffprobe_bin)
        row["local_video_duration_seconds"] = round(duration, 3)
        # Avoid the opening and closing title/credit sections. Sample across
        # the usable duration, with 5 frames by default as required by the task.
        fractions = [0.10, 0.30, 0.50, 0.70, 0.90]
        if frame_count != 5:
            fractions = [((i + 1) / (frame_count + 1)) for i in range(frame_count)]
        frames = []
        for index, fraction in enumerate(fractions[:frame_count], start=1):
            seconds = min(max(0.0, duration - 0.25), duration * fraction)
            timestamp = format_seconds(seconds)
            filename = f"frame_{index:02d}_{timestamp.replace(':', '-')}.jpg"
            destination = folder / filename
            command = [
                ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{seconds:.3f}", "-i", str(media_path),
                "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", "-q:v", "2",
                str(destination),
            ]
            subprocess.run(command, check=True, capture_output=True, text=True)
            if not destination.is_file() or destination.stat().st_size == 0:
                raise RuntimeError(f"FFmpeg 未產生畫格：{timestamp}")
            caption = nearest_caption(row.get("timestamps") or row.get("transcription_segments"), seconds)
            description = f"對應字幕：{caption}" if caption else "未找到時間點附近的字幕，請人工確認並補充畫面描述。"
            frames.append({
                "file": f"videos/{row['id']}_frames/{filename}",
                "timestamp": timestamp,
                "seconds": round(seconds, 3),
                "description": description,
                "caption": caption,
                "source_video": media_path.name,
            })
        row["video_frames"] = frames
        row["frame_extraction_status"] = "success" if frames else "no_frames"
        row["frame_extraction_note"] = ""
        row["local_video_file"] = media_path.name
        readme = [f"本機影片：{media_path.name}", f"擷取畫格：{len(frames)} 張", ""]
        readme += [f"- {item['timestamp']}：{item['description']}（{Path(item['file']).name}）" for item in frames]
        (folder / "README.md").write_text("\n".join(readme) + "\n", encoding="utf-8")
    except Exception as exc:
        # Do not leave partial frame files that could be mistaken for a
        # complete extraction when one of the requested timestamps fails.
        for partial in folder.glob("frame_*.jpg"):
            try:
                partial.unlink()
            except OSError:
                pass
        note = f"代表畫面擷取失敗：{type(exc).__name__}: {exc}"
        row["video_frames"] = []
        row["frame_extraction_status"] = "failed"
        row["frame_extraction_note"] = note
        row["local_video_file"] = media_path.name
        (folder / "README.md").write_text(note + "\n", encoding="utf-8")


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--image-limit',type=int,default=3); ap.add_argument('--no-images',action='store_true'); ap.add_argument('--media-dir',type=Path,help='本機合法取得的影片資料夾；檔名需以 YouTube video ID 開頭'); ap.add_argument('--frame-count',type=int,choices=(3,4,5),default=5,help='每部影片擷取的代表畫面數量（3 至 5 張）'); ap.add_argument('--ffmpeg',default='ffmpeg',help='FFmpeg 執行檔名稱或路徑'); ap.add_argument('--ffprobe',default='ffprobe',help='FFprobe 執行檔名稱或路徑'); args=ap.parse_args()
    raw=jlines(DATA/'records.jsonl')
    with (DATA/'crawl_status.csv').open(encoding='utf-8-sig',newline='') as f: statuses=list(csv.DictReader(f))
    for item in statuses:
        item['status'], item['reason'] = access_status(item.get('status',''), item.get('reason',''), item.get('http_status',''))
    status={r['url']:r for r in statuses}; byurl={r.get('source_url'):r for r in raw}
    with (ROOT/'urls.csv').open(encoding='utf-8-sig',newline='') as f: urls=[x['url'].strip() for x in csv.DictReader(f) if x.get('url','').strip()]
    urls += [s['url'] for s in statuses if s['url'] not in set(urls)]
    urls=list(dict.fromkeys(urls)); OUT.mkdir(exist_ok=True); REPORT.mkdir(exist_ok=True)
    # Rebuild only files owned by this exporter so reruns do not leave stale page IDs.
    for old in (OUT/'articles').glob('*.md'):
        old.unlink()
    for old in (OUT/'videos').glob('*.md'):
        old.unlink()
    for old in (OUT/'videos').glob('video_*_frames'):
        for f in old.glob('*'):
            if f.is_file(): f.unlink()
        old.rmdir()
    for pattern in ('page_*_image_*','video_*_image_*'):
        for old in (OUT/'images').glob(pattern): old.unlink()
    for x in ('articles','videos','images'): (OUT/x).mkdir(parents=True,exist_ok=True)
    page_n=video_n=0; rows=[]
    for url in urls:
        st=status.get(url,{})
        src=byurl.get(url,{})
        parsed=urlparse(url); host=(parsed.hostname or '').lower(); path=parsed.path.lower()
        video=(host=='youtu.be' or (host.endswith('youtube.com') and (path.startswith('/shorts/') or path.startswith('/watch') or path.startswith('/live/') or path.startswith('/embed/'))))

        # 優先沿用 crawler.py 的階層式 ID；舊資料才使用傳統編號。
        if video:
            video_n += 1
            fallback_id = f'video_{video_n:03d}'
        else:
            page_n += 1
            fallback_id = f'page_{page_n:03d}'
        ident = src.get('id') or st.get('id') or fallback_id
        parent_id = src.get('parent_id') or st.get('parent_id') or ''

        title=src.get('title') or st.get('title') or ''; body=src.get('content','')
        topics, keywords=classify(title,body)
        rows.append({'id':ident,'parent_id':parent_id,'type':'video' if video else 'webpage','title':title,'source_url':url,
          'source_name':src.get('channel') if video else src.get('site_name',''),'content':body,
          'author':src.get('author',''),'published_at':src.get('published_at',''),
          'images':[],'_candidates':src.get('images',[]),'links':src.get('links',[]),
          'timestamps':src.get('transcript_segments',[]),'crawl_status':st.get('status','failed'),
          'crawl_reason':st.get('reason',''),'crawled_at':src.get('fetched_at') or st.get('crawled_at',''),'topic':topics,'keywords':keywords,
          'depth':int(src.get('depth') or st.get('depth') or 0),
          'source_type':src.get('source_type') or st.get('source_type','seed'),
          'final_url':src.get('final_url') or st.get('final_url',''),
          'http_status':st.get('http_status',''),'parser_note':src.get('extraction_note',''),
          'video_id':src.get('video_id',''),'duration_seconds':src.get('duration_seconds'),'caption_available':src.get('caption_available',False),
          'caption_note':src.get('caption_note',''),
          'transcription':src.get('transcription',''),
          'transcription_segments':src.get('transcription_segments',[]),
          'transcription_available':src.get('transcription_available',False),
          'transcription_language':src.get('transcription_language',''),
          'transcription_source':src.get('transcription_source',''),
          'transcription_note':src.get('transcription_note',''),
          'downloaded_audio':src.get('downloaded_audio',False)})
    groups=duplicate_groups(rows)
    for n,g in enumerate(groups,1):
        for i in g: rows[i]['duplicate_group']=f'dup_{n:03d}'
    if not args.no_images: save_images(rows,max(0,args.image_limit))
    else:
        for r in rows:
            r['images']=[{'file':'','source_url':x.get('url',''),'alt':x.get('alt',''),'caption':x.get('caption',''),'download_status':'not_downloaded'} for x in r.pop('_candidates',[])[:args.image_limit]]
    # Video frames are extracted only from user-provided local files. No video
    # is downloaded by this stage; filenames must start with the YouTube ID.
    for r in rows:
        if r['type'] == 'video':
            extract_local_video_frames(r, args.media_dir, args.frame_count, args.ffmpeg, args.ffprobe)

    index=OUT/'index.jsonl'
    with index.open('w',encoding='utf-8') as f:
        for r in rows: f.write(json.dumps(r,ensure_ascii=False)+'\n')
    for r in rows:
        if r['type']=='webpage':
            f=OUT/'articles'/f"{r['id']}.md"
            lines=[f"# {r['title'] or r['id']}", '',f"- ID：{r['id']}",f"- 父頁面 ID：{r.get('parent_id') or '無（原始網址）'}",f"- 深度：{r['depth']}",f"- 網站：{r['source_name'] or '未知'}",f"- 原始網址：{r['source_url']}",f"- 狀態：{r['crawl_status']}"]
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
            lines=[f"# {r['title'] or r['id']}",'',f"- ID：{r['id']}",f"- 父頁面 ID：{r.get('parent_id') or '無（原始網址）'}",f"- 深度：{r['depth']}",f"- 頻道：{r['source_name'] or '未知'}",f"- 原始網址：{r['source_url']}",f"- 狀態：{r['crawl_status']}"]
            if r.get('local_video_file'): lines.append(f"- 本機影片檔：{r['local_video_file']}")
            lines.append(f"- 代表畫面擷取狀態：{r.get('frame_extraction_status','not_run')}")
            if r.get('frame_extraction_note'): lines.append(f"- 擷取備註：{r['frame_extraction_note']}")
            if r.get('crawl_reason'): lines.append(f"- 原因：{r['crawl_reason']}")
            lines += [f"- 時長（秒）：{r['duration_seconds'] or '未知'}",'','## 字幕逐字稿']
            if r['timestamps']: lines += [f"- **[{s['timestamp']}]** {s['text']}" for s in r['timestamps']]
            elif r['transcription_segments']:
                if r['transcription_source']: lines.append(f"- 來源：Whisper 語音辨識（{r['transcription_source']}）")
                if r['transcription_language']: lines.append(f"- 語言：{r['transcription_language']}")
                lines += [f"- **[{s['timestamp']}]** {s['text']}" for s in r['transcription_segments']]
            elif r['transcription']: lines += ['- 來源：Whisper 語音辨識',r['transcription']]
            elif r['content']: lines += [r['content']]
            else: lines += [r['transcription_note'] or r['caption_note'] or '沒有取得可用字幕；未使用語音辨識補寫。']
            if r['transcription_note'] and not r['transcription_available']:
                lines += ['',f"> 語音辨識備註：{r['transcription_note']}"]
            lines += ['','## 代表畫面']
            if r.get('video_frames'):
                for frame in r['video_frames']:
                    rel = f"{r['id']}_frames/{Path(frame['file']).name}"
                    lines += [f"### {frame['timestamp']}", f"![{frame['timestamp']}]({rel})", f"- 畫面描述：{frame['description']}"]
            else:
                lines.append(r.get('frame_extraction_note') or '未取得本機影片檔，因此沒有擷取代表畫面。')
            for im in r['images']:
                if im.get('file'): lines.append(f"- [影片縮圖](../{im['file']}) — {im['source_url']}（僅供辨識影片，不列為代表畫面）")
            (OUT/'videos'/f"{r['id']}.md").write_text('\n'.join(lines)+'\n',encoding='utf-8')
    fields=['id','parent_id','type','url','title','status','http_status','reason','depth','source_type','crawled_at','frame_extraction_status','frame_count','frame_extraction_note']
    with (OUT/'crawl_report.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for r in rows: w.writerow({'id':r['id'],'parent_id':r.get('parent_id',''),'type':r['type'],'url':r['source_url'],'title':r['title'],'status':r['crawl_status'],'http_status':r['http_status'],'reason':r['crawl_reason'] or r['caption_note'],'depth':r['depth'],'source_type':r.get('source_type',''),'crawled_at':r['crawled_at'],'frame_extraction_status':r.get('frame_extraction_status',''),'frame_count':len(r.get('video_frames',[])),'frame_extraction_note':r.get('frame_extraction_note','')})
    print(f"Exported {len(rows)} records into {OUT}")

if __name__=='__main__': main()
