#!/usr/bin/env python3
"""泄漏自检：扫描“会交给被测 AI 的一切”，出现测试元信息关键词就报错。

扫描范围：
  prompt   —— 组装好的最终任务文本（prompting.render）
  path     —— 工作目录里每个文件/文件夹的相对路径；工作目录本身的绝对路径
  file     —— 工作目录里的文本文件内容；xlsx/docx/pptx 解压后的 XML（含作者、标题等元数据）
  env      —— 启动 DSH 时的环境变量（名字和值）
  launch   —— 启动命令行里的每个参数、覆盖层文件内容（覆盖层里的插件规则本身是对照臂的“处理”，单独标记为 treatment）
  git      —— 工作目录里不能有 .git（提交历史可能带元信息）

关键词表 TERMS：测试/对照/插件/判分/数据集名等。题目原文里“碰巧”出现的词（例如一道经济学选择题里的 benchmark），
人工确认无害后写进 leak_allow.json 放行，必须写明理由。

用法：python leakcheck.py [--tasks ../tasks]      # 扫全部题目的提示词与工作目录（不含运行时环境）
      bench.py 每次启动 DSH 前还会对该次运行的工作目录、环境变量、命令行再扫一遍，命中就跳过该次并报错。
"""
import argparse, hashlib, json, os, re, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TERMS = [
    # 测试本身
    r"bench(?:mark)?s?", r"dsh-?bench", r"基准", r"评测", r"测评", r"对照", r"实验组", r"对照组", r"受试", r"被测",
    r"判分", r"评分", r"打分", r"graders?", r"grading", r"scor(?:e|ing)\s*card", r"leaderboard",
    r"hidden[ _-]?tests?", r"隐藏(?:的)?(?:单元)?测试", r"标准答案", r"参考答案", r"answer[ _-]?key", r"golden", r"oracle",
    r"answer\.json", r"expected\.json", r"_answers", r"manifest", r"task_id", r"题号", r"题库", r"候补",
    r"calibrat\w*", r"预跑", r"leak", r"泄漏",
    # 对照臂与预设 / 插件类词汇（任何出现在工作区里的都算可疑）
    r"\bops\b", r"presets?", r"预设", r"\barms?\b", r"对照臂", r"对照组臂",
    # 数据集 / 题源
    r"livecodebench", r"\blcb\b", r"aider", r"polyglot", r"exercism", r"spreadsheet-?bench", r"\bssb\b", r"mmlu",
    r"c-?eval", r"gpqa", r"longbench", r"\bruler\b", r"needle", r"swe-?bench", r"leetcode", r"atcoder", r"codeforces",
    r"huggingface", r"\bhf_",
]
RX = re.compile("|".join(f"(?:{t})" for t in TERMS), re.I)
TEXT_EXT = {".txt", ".md", ".py", ".js", ".mjs", ".cjs", ".json", ".csv", ".tsv", ".ini", ".cfg", ".toml", ".yml", ".yaml",
            ".log", ".ics", ".html", ".xml", ".sh", ".ps1", ".bat", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h", ""}
ALLOW_PATH = os.path.join(HERE, "leak_allow.json")


def load_allow():
    return json.load(open(ALLOW_PATH, encoding="utf-8")) if os.path.exists(ALLOW_PATH) else {}


def hits(text, where, out, ctx=30, sha=None):
    for m in RX.finditer(text or ""):
        a, b = max(0, m.start() - ctx), min(len(text), m.end() + ctx)
        out.append({"where": where, "term": m.group(0), "sha": sha, "context": text[a:b].replace("\n", "⏎")})


def scan_dir(wdir, out, label_root=True):
    if label_root:
        hits(os.path.abspath(wdir), "path:<工作目录>", out)
    for root, dirs, files in os.walk(wdir):
        if os.path.islink(root):
            continue
        for d in list(dirs):
            rel = os.path.relpath(os.path.join(root, d), wdir)
            if d == ".git":
                out.append({"where": "git", "term": ".git", "context": rel})
            if d == "node_modules":  # 预装的第三方库：只查名字，不查内容
                dirs.remove(d); continue
            hits(rel, "path", out)
        for f in files:
            full = os.path.join(root, f); rel = os.path.relpath(full, wdir); hits(rel, "path", out)
            ext = os.path.splitext(f)[1].lower()
            try:
                if ext in (".xlsx", ".xlsm", ".docx", ".pptx"):
                    with zipfile.ZipFile(full) as z:
                        for n in z.namelist():
                            if n.endswith((".xml", ".rels")):
                                hits(z.read(n).decode("utf-8", "replace"), f"file:{rel}!{n}", out)
                elif ext in TEXT_EXT and os.path.getsize(full) < 50_000_000:
                    raw = open(full, "rb").read()
                    hits(raw.decode("utf-8", "replace"), "file:" + rel, out,
                         sha=hashlib.sha256(raw).hexdigest())
            except zipfile.BadZipFile:  # 扩展名是 docx/xlsx 但其实不是压缩包：按纯文本查
                hits(open(full, "rb").read().decode("utf-8", "replace"), f"file:{rel}", out)
            except Exception as ex:
                out.append({"where": f"file:{rel}", "term": "UNREADABLE", "context": repr(ex)[:120]})


def scan_env(env, out):
    for k, v in env.items():
        hits(k, "env:name", out); hits(f"{k}={v}", f"env:{k}", out)


def filter_allowed(task_id, found, allow):
    """放行「人工确认过、且只对这一段原文成立」的命中。

    审计修正（P2-1）：原来一条 {"term": "*", "where": "file:context.txt"} 把 34 个长文文件里的
    *任何* 命中都放行，包括 answer key / hidden test / task_id —— 等于把这 1.7 MB 正文排除在自检
    之外；而且 r.get("context", "") in h["context"] 在没有 context 键时恒真。现在三条路：
      ① 显式词条：term 必须逐字等于命中词，给了 context 就必须真的出现在片段里；
      ② by_hash 豁免：只有「文件内容哈希与审定时一致」且「词也在当时那份名单里」才生效 ——
         长文被改动一个字，豁免立刻失效，改完必须重新审定；
      ③ 其余一律算命中。不再支持 term=*。
    """
    rules = allow.get(task_id, []) + allow.get("*", [])
    keep = []
    for h in found:
        term = (h.get("term") or "").lower()
        ok = False
        for r in rules:
            rt = (r.get("term") or "").lower()
            if rt in ("", "*"):
                continue  # 通配已停用：必须写明具体词
            if rt != term:
                continue
            if not h["where"].startswith(r.get("where", "")):
                continue
            ctx = r.get("context")
            if ctx and ctx not in h["context"]:
                continue
            ok = True
            break
        if not ok and h.get("sha"):
            for e in allow.get("by_hash", []):
                if e.get("sha256") != h["sha"]:
                    continue
                if term not in {(x or "").lower() for x in e.get("terms", [])}:
                    continue
                if not h["where"].startswith(e.get("where", "")):
                    continue
                ok = True
                break
        if not ok:
            keep.append(h)
    return keep


def check_task(tdir, allow=None):
    import prompting
    allow = load_allow() if allow is None else allow
    t = json.load(open(os.path.join(tdir, "task.json"), encoding="utf-8")); found = []
    hits(prompting.render(t), "prompt", found)
    scan_dir(os.path.join(tdir, "workspace"), found, label_root=False)
    return filter_allowed(t["task_id"], found, allow)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--tasks", default=os.path.join(ROOT, "tasks"))
    ap.add_argument("--json", action="store_true", help="以 JSON 输出全部命中（用来写 leak_allow.json）")
    a = ap.parse_args(); allow = load_allow(); bad = {}
    for tid in sorted(os.listdir(a.tasks)):
        if os.path.isfile(os.path.join(a.tasks, tid, "task.json")):
            h = check_task(os.path.join(a.tasks, tid), allow)
            if h:
                bad[tid] = h
    if a.json:
        print(json.dumps(bad, ensure_ascii=False, indent=1)); return 1 if bad else 0
    for tid, hs in bad.items():
        for h in hs[:5]:
            print(f"✘ {tid}  [{h['where']}]  “{h['term']}”  …{h['context']}…")
        if len(hs) > 5:
            print(f"  …另有 {len(hs) - 5} 处")
    print(f"\n扫描完成：{len(bad)} 道题有未放行的命中。" if bad else "\n扫描完成：没有发现测试元信息。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
