"""编码鲁棒性检查（v1.5）：把标准答案改存成 GBK / UTF-16 再判分，内容正确就必须判对。
用法：python bench/mock/encoding_check.py"""
import json,os,shutil,tempfile,glob,collections,sys
HERE=os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0,os.path.join(HERE,'..'))
import answers,selftest,grade
res=collections.defaultdict(lambda:[0,0,0])
for td in sorted(glob.glob(os.path.join(HERE,'..','..','tasks','*/'))):
    td=td.rstrip('/'); t=json.load(open(td+'/task.json'))
    if not (t['grader'].startswith('self_') or t['grader'] in('mcq','exact_int')): continue
    if t['grader']=='mcq' and 'lb2' in td: continue
    for enc in ('gbk','utf-16'):
        d=tempfile.mkdtemp(); shutil.rmtree(d); shutil.copytree(td+'/workspace',d)
        before={os.path.join(r,f):os.path.getmtime(os.path.join(r,f)) for r,_,fs in os.walk(d) for f in fs}
        with answers.view(td) as v: selftest.oracle(t,v,d)
        for r,_,fs in os.walk(d):
            for f in fs:
                p=os.path.join(r,f)
                if f.endswith(('.txt','.csv','.json','.ini','.md','.log')) and (p not in before or os.path.getmtime(p)!=before[p]):
                    s=open(p,encoding='utf-8').read(); open(p,'w',encoding=enc,newline='').write(s)
        g=grade.grade(td,d); k=(t['grader'],enc); res[k][0]+=1; res[k][1]+=g['pass']; res[k][2]+='grader_error' in g['detail']
        shutil.rmtree(d,ignore_errors=True)
for k,v in sorted(res.items()): print(k,'n',v[0],'pass',v[1],'grader_error',v[2])

bad=[k for k,v in res.items() if v[1]!=v[0]]
print('编码检查：'+('通过' if not bad else '失败 %s'%bad)); sys.exit(1 if bad else 0)
