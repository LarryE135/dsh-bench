# 审计用：自建办公题的独立解题脚本（不读标准答案），验证判分与题面一致。用法同上。
import csv,json,os,re,shutil,tempfile,glob,io
from datetime import datetime,date
from collections import Counter
import grade
def new(t):
    d=tempfile.mkdtemp(); shutil.rmtree(d); shutil.copytree(t+'/workspace',d); return d
def wcsv(p,rows):
    with open(p,'w',newline='') as f: csv.writer(f,lineterminator='\n').writerows(rows)
M={m:i for i,m in enumerate(['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'],1)}
def s_invoices(d):
    out=[]
    for f in sorted(glob.glob(d+'/invoices/*.txt')):
        t=open(f).read(); no=re.search(r'(FP\d{8})',t)[1]
        if m:=re.search(r'(\d{4})年(\d+)月(\d+)日',t): dt=date(int(m[1]),int(m[2]),int(m[3]))
        elif m:=re.search(r'Date: (\d+) (\w{3}) (\d{4})',t): dt=date(int(m[3]),M[m[2]],int(m[1]))
        elif m:=re.search(r'(\d{4})/(\d{2})/(\d{2})',t): dt=date(*map(int,m.groups()))
        else: m=re.search(r'(\d{4})-(\d{2})-(\d{2})',t); dt=date(*map(int,m.groups()))
        m=re.search(r'(?:价税合计（小写）：¥|Total: |RMB |总计：)([\d,]+\.\d+)',t); tot=float(m[1].replace(',',''))
        out.append([no,dt.isoformat(),f'{tot:.2f}'])
    out.sort(key=lambda r:(r[1],r[0])); wcsv(d+'/invoices.csv',[['invoice_no','date','total']]+out)
def s_logs(d):
    ips=Counter();hrs=Counter();s5=0;paths=set()
    for l in open(d+'/access.log'):
        m=re.match(r'(\S+) - - \[\d+/\w+/\d+:(\d\d):.*?"GET (\S+) HTTP/1.1" (\d+)',l)
        ips[m[1]]+=1;hrs[m[2]]+=1;s5+=int(m[4])>=500;paths.add(m[3].split('?')[0])
    top=sorted(ips,key=lambda k:(-ips[k],k))[:3]; bh=sorted(hrs,key=lambda h:(-hrs[h],h))[0]
    json.dump({'top_ips':top,'status_5xx':s5,'busiest_hour':bh,'unique_paths':len(paths)},open(d+'/answers.json','w'))
def s_calendar(d):
    ev=[];cur={}
    for l in open(d+'/events.ics').read().splitlines():
        if ':' in l: k,v=l.split(':',1); cur[k]=v
        if l=='END:VEVENT': ev.append((cur['UID'],datetime.strptime(cur['DTSTART'],'%Y%m%dT%H%M%S'),datetime.strptime(cur['DTEND'],'%Y%m%dT%H%M%S')))
    pr=sorted(f'{min(a[0],b[0])},{max(a[0],b[0])}' for i,a in enumerate(ev) for b in ev[i+1:] if a[1]<b[2] and b[1]<a[2])
    open(d+'/conflicts.txt','w').write('\n'.join(pr)+'\n')
def s_units(d):
    rows=[]
    for l in open(d+'/products.md'):
        c=[x.strip() for x in l.strip().strip('|').split('|')]
        if not c[0].startswith('SKU-'): continue
        w=c[2]; m=re.match(r'([\d.]+)(kg|g|斤|lb)',w); v=float(m[1]); kg={'g':v/1000,'kg':v,'斤':v/2,'lb':v*0.45359237}[m[2]]
        rows.append([c[0],kg,float(c[3])/kg])
    rows.sort(key=lambda r:r[2]); wcsv(d+'/products.csv',[['sku','weight_kg','price_per_kg']]+[[s,f'{k:.3f}',f'{p:.2f}'] for s,k,p in rows])
def s_organize(d):
    for f in os.listdir(d+'/inbox'):
        ym=None
        if m:=re.search(r'(\d{4})(\d{2})\d{2}',f): ym=f'{m[1]}-{m[2]}'
        if m:=re.search(r'(\d{4})[-.](\d{2})[-.]\d{2}',f): ym=f'{m[1]}-{m[2]}'
        if m:=re.match(r'\d{2}-(\d{2})-(\d{4})_',f): ym=f'{m[2]}-{m[1]}'
        if ym: os.makedirs(f'{d}/inbox/{ym}',exist_ok=True); shutil.move(f'{d}/inbox/{f}',f'{d}/inbox/{ym}/{f}')
def s_dedupe(d):
    seen=set();out=[]
    for r in list(csv.DictReader(open(d+'/contacts.csv'))):
        e=r['email'].strip().lower()
        if e in seen: continue
        seen.add(e); out.append([r['name'],e,re.sub(r'\D','',r['phone'].replace('+86',''))])
    wcsv(d+'/contacts_clean.csv',[['name','email','phone']]+out)
def s_region(d):
    reg={r['customer_id']:r['region'] for r in csv.DictReader(open(d+'/customers.csv'))}; agg={}
    for r in csv.DictReader(open(d+'/orders.csv')):
        if '2026-04-01'<=r['date']<='2026-06-30': a=agg.setdefault(reg[r['customer_id']],[0,0.0]); a[0]+=1; a[1]+=float(r['amount'])
    rows=sorted(([k,v[0],f'{v[1]:.2f}'] for k,v in agg.items()),key=lambda x:-float(x[2])); wcsv(d+'/region_summary.csv',[['region','order_count','total_amount']]+rows)
for t in sorted(glob.glob('tasks/office-self-*')):
    k=t.split('-')[2]; fn=globals().get('s_'+k)
    if not fn: continue
    d=new(t); fn(d); r=grade.grade(t,d); print(t.split('/')[-1],r['pass'],'' if r['pass'] else str(r['detail'])[:200])
