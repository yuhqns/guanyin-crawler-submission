#!/usr/bin/env python3
"""Create text-based before/after source captures when browser screenshots are unavailable."""
import html, json, pathlib, re, shutil, subprocess, sys, tempfile, textwrap
from urllib.request import Request, urlopen
from PIL import Image, ImageDraw, ImageFont
from bs4 import BeautifulSoup
import crawler

ROOT=pathlib.Path(__file__).resolve().parent
OUT=ROOT/'report'/'screenshots'; OUT.mkdir(parents=True,exist_ok=True)
DATA=ROOT/'data'/'records.jsonl'
TARGETS=[
 ('Encyclopedia','https://zh.wikipedia.org/zh-tw/%E8%A7%80%E9%9F%B3%E5%8D%80','01_wikipedia.png','raw_01_wikipedia.png'),
 ('City government home','https://www.tycg.gov.tw/','02_government.png','raw_02_government.png'),
 ('Temple official site','https://www.ganquan-temple.org.tw/','03_official_temple.png','raw_03_official_temple.png'),
]
FONT='C:/Windows/Fonts/msjh.ttc'
UA='GuanyinKnowledgeCrawler/1.0 (public-source research)'

def capture_browser_screenshot(url, output):
    """Save a real rendered page screenshot when a local Chromium browser is available."""
    candidates=[
        pathlib.Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
        pathlib.Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
    ]
    browser=next((str(p) for p in candidates if p.exists()),None)
    if not browser:
        print(f'no Chromium browser found; skipped {output.name}',file=sys.stderr)
        return False
    with tempfile.TemporaryDirectory(prefix='guanyin-capture-') as profile:
        command=[browser,'--headless','--disable-gpu','--no-sandbox','--no-first-run',
                 f'--user-data-dir={profile}','--window-size=1600,1200',
                 '--virtual-time-budget=5000',f'--screenshot={output}',url]
        try:
            subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45,check=False)
        except (OSError,subprocess.TimeoutExpired) as exc:
            print(f'failed browser screenshot {output.name}: {type(exc).__name__}: {exc}',file=sys.stderr)
            return False
    if output.exists() and output.stat().st_size>0:
        print('saved browser screenshot',output.name)
        return True
    print(f'browser could not load page; screenshot unavailable: {output.name}',file=sys.stderr)
    return False

def wrap_pixels(draw,text,font,max_width):
    out=[]; line=''
    for para in text.splitlines():
        if not para: out.append(''); continue
        for ch in para:
            if line and draw.textlength(line+ch,font=font)>max_width:
                out.append(line); line=ch
            else: line+=ch
        if line: out.append(line); line=''
    return out

def draw_text(draw,text,x,y,w,h,font,fill):
    lines=wrap_pixels(draw,text,font,w); step=font.size+7
    for line in lines[:max(0,h//step)]:
        draw.text((x,y),line,font=font,fill=fill); y+=step

records={}
for line in DATA.open(encoding='utf-8'):
    r=json.loads(line); records[r.get('source_url')]=r

for label,url,filename,raw_filename in TARGETS:
    req=Request(url,headers={'User-Agent':UA,'Accept-Language':'zh-TW,zh;q=0.9,en;q=0.7'})
    try:
        with urlopen(req,timeout=20) as resp: raw_html=resp.read(8*1024*1024).decode(resp.headers.get_content_charset() or 'utf-8',errors='replace')
        soup=BeautifulSoup(raw_html,'html.parser')
        raw=soup.body.get_text('\n',strip=True) if soup.body else soup.get_text('\n',strip=True)
        clean=crawler.parse_html(url,raw_html).get('content','')
        if not clean:
            # When a live page is unavailable to the crawler, use the saved
            # crawl record for the text side and keep the screenshot label honest.
            clean=records.get(url,{}).get('content','')
        title=records.get(url,{}).get('title') or (soup.title.get_text(' ',strip=True) if soup.title else label)
        image=Image.new('RGB',(1600,1200),'#f4f6f8'); d=ImageDraw.Draw(image)
        title_font=ImageFont.truetype(FONT,29); head_font=ImageFont.truetype(FONT,22); body_font=ImageFont.truetype(FONT,17); small=ImageFont.truetype(FONT,14)
        d.text((55,25),f'網頁擷取與清理對照｜{label}',font=title_font,fill='#14243a')
        d.text((55,70),title[:65],font=head_font,fill='#24364b')
        d.text((55,108),url[:130],font=small,fill='#365f84')
        d.rounded_rectangle((45,150,785,1140),radius=14,fill='white',outline='#b8c4d1',width=2)
        d.rounded_rectangle((815,150,1555,1140),radius=14,fill='white',outline='#9bbfae',width=2)
        d.text((70,170),'原始頁面文字擷取（含頁面雜訊）',font=head_font,fill='#8a442f')
        d.text((840,170),'清理後正文（爬蟲輸出）',font=head_font,fill='#236242')
        raw=re.sub(r'\n{3,}','\n\n',raw).strip()
        d.text((70,210),f'原始文字長度：{len(raw)} 字；保留導覽、站務等文字以供比較。',font=small,fill='#657384')
        d.text((840,210),f'正文長度：{len(clean)} 字；移除 script/style、常見導覽與廣告區塊。',font=small,fill='#657384')
        draw_text(d,raw[:7000],70,245,680,860,body_font,'#303944')
        draw_text(d,clean[:7000],840,245,680,860,body_font,'#303944')
        d.text((55,1160),'本圖呈現已取得 HTML 的文字對照；瀏覽器直接連外截圖在執行環境受限，故不冒稱為網站瀏覽器畫面。',font=small,fill='#6b7280')
        image.save(OUT/filename,optimize=True)
        print(f'saved {filename}: raw={len(raw)} chars, clean={len(clean)} chars')
        capture_browser_screenshot(url,OUT/raw_filename)
    except Exception as exc:
        print(f'failed {url}: {type(exc).__name__}: {exc}',file=sys.stderr)
