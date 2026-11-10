# 审计用：只看题面文字逐步执行长链路题，再用 grade.py 判分，验证“题面与标准答案一致”。用法：在 tasks/ 的上级目录运行 python bench/audit_chain_exec.py
import json,os,re,shutil,tempfile,glob,copy,sys
import grade
def run(tdir):
    t=json.load(open(tdir+'/task.json')); p=t['prompt']; d=tempfile.mkdtemp(); shutil.rmtree(d); shutil.copytree(tdir+'/workspace',d)
    word=re.search(r'口令 (\w+)（',p).group(1)
    steps=re.findall(r'^(\d+)\. (.*)$',p,re.M); hist={}; created={}; report=[]; prog=[]
    J=lambda: json.load(open(d+'/config.json')); 
    def W(c): json.dump(c,open(d+'/config.json','w'),indent=2); open(d+'/config.json','a').write('\n')
    for n,s in steps:
        n=int(n); before=J()
        if m:=re.match(r'新建 notes/(\S+)，内容为一行：(.*)$',s): open(f'{d}/notes/{m[1]}','w').write(m[2]+'\n'); created[n]=m[1]
        elif m:=re.match(r'新建 notes/(\S+)，内容是 notes/(\S+) 当前内容的全大写版本',s): open(f'{d}/notes/{m[1]}','w').write(open(f'{d}/notes/{m[2]}').read().upper()); created[n]=m[1]
        elif m:=re.match(r'把 notes/(\S+) 重命名为 notes/(\S+)$',s): os.rename(f'{d}/notes/{m[1]}',f'{d}/notes/{m[2]}')
        elif m:=re.match(r'删除 notes/(\S+)$',s): os.remove(f'{d}/notes/{m[1]}')
        elif s.startswith('把 notes/ 下当前 .txt'): c=J(); c['totals']['notes']=len([f for f in os.listdir(d+'/notes') if f.endswith('.txt')]); W(c)
        elif m:=re.match(r'把 config.json 顶层键 (\w+) 设为字符串 "(\w+)"',s): c=J(); c[m[1]]=m[2]; W(c)
        elif m:=re.match(r'把 config.json 里 counters.(\w) 加 (\d+)',s): c=J(); c['counters'][m[1]]+=int(m[2]); W(c)
        elif m:=re.match(r'在 table.csv 末尾追加一行：(.*)$',s): open(d+'/table.csv','a').write(m[1]+'\n')
        elif s.startswith('把 table.csv 中 qty'): c=J(); c['totals']['qty']=sum(int(l.split(',')[1]) for l in open(d+'/table.csv').read().split('\n')[1:] if l); W(c)
        elif m:=re.match(r'撤销第 (\d+) 步',s):
            j=int(m[1]); b,a=hist[j]; c=J()
            for k in set(b)|set(a):
                if b.get(k)!=a.get(k):
                    if k=='counters':
                        for x in 'ab': c['counters'][x]-=a['counters'][x]-b['counters'][x]
                    elif k in b: c[k]=b[k]
                    else: c.pop(k,None)
            W(c)
        elif m:=re.match(r'把第 (\d+) 步新建的那个文件的文件名',s): report.append(created[int(m[1])])
        elif s.startswith('在 REPORT.md 追加一行 none'): report.append('none')
        elif s.startswith('清理'): [os.remove(f) for f in glob.glob(d+'/notes/*.tmp')]
        elif s.startswith('最后'): pass
        else: print('UNPARSED',s)
        hist[n]=(before,J()); prog.append(f'step {n} done')
    open(d+'/REPORT.md','w').write('\n'.join([word]+report)+'\n'); open(d+'/progress.log','w').write('\n'.join(prog)+'\n')
    return grade.grade(tdir,d)
for t in sorted(glob.glob('tasks/long-chain-*')):
    r=run(t); print(t.split('/')[-1],r['pass'],{k:v for k,v in r['detail'].items() if v is not True})
