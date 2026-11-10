#!/usr/bin/env python3
"""从已下载的公开数据集里，按固定随机种子分层抽样，生成统一格式的题目目录。

用法（重活放 /tmp）：
    python build_public.py --raw /tmp/bench/raw --out /tmp/bench/tasks --pool 1.3

每道题一个目录：
    tasks/<task_id>/task.json     题面、来源、许可、判分方式（给 runner 和 grader 用）
    tasks/<task_id>/workspace/    跑题前原样复制给 agent 的文件（agent 的 cwd）
    tasks/<task_id>/hidden/       只有判分器能看的东西（隐藏测试、标准答案），绝不复制给 agent

--pool 1.3 表示每个来源多抽 30% 当“候补”：预跑时坏题/全错题被剔除后，从候补里按顺序补位。
"""
import argparse, base64, json, os, pickle, random, shutil, zlib
from collections import defaultdict

SEED = 20261103

# 每个来源的正式题数和分层配额（层 -> 题数）
QUOTA = {
    "mmlu_pro":    {"math": 1, "physics": 1, "chemistry": 1, "law": 1, "engineering": 1,
                    "computer science": 1, "economics": 1, "history": 1},
    "ceval_hard":  {"*": 6},
    "lcb_v6":      {"easy": 4, "medium": 6, "hard": 5},
    "polyglot":    {"python": 10, "javascript": 5},
    "ssb_v400":    {"Cell-Level Manipulation": 10, "Sheet-Level Manipulation": 5},
    "longbench_v2_short": {"easy": 3, "hard": 5},
    "gpqa_diamond": {"Physics": 2, "Chemistry": 2, "Biology": 2},
}
CEVAL_HARD = ["advanced_mathematics", "discrete_mathematics", "probability_and_statistics",
              "college_chemistry", "college_physics", "high_school_mathematics",
              "high_school_chemistry", "high_school_physics"]

MCQ_TAIL = ("\n\n请把你的最终答案只写一个大写选项字母（例如 B），保存到当前目录的 ANSWER.txt。"
            "不要联网搜索。")


def pick(rng, items_by_stratum, quota, pool):
    """分层抽样：每层先打乱（固定种子），取 ceil(配额*pool) 个；前“配额”个是正式题，其余是候补。"""
    out = []
    for stratum, n in quota.items():
        items = sorted(items_by_stratum.get(stratum, []), key=lambda x: x[0])  # 先按 ID 排序，保证可复现
        rng.shuffle(items)
        k = min(len(items), max(n, int(round(n * pool + 0.4999))))
        for rank, (iid, payload) in enumerate(items[:k]):
            out.append({"stratum": stratum, "rank": rank, "role": "main" if rank < n else "reserve",
                        "source_id": iid, "payload": payload})
    return out


def write_task(out_dir, tid, meta, ws_files=None, hidden_files=None, copytrees=None):
    d = os.path.join(out_dir, tid)
    os.makedirs(os.path.join(d, "workspace"), exist_ok=True)
    os.makedirs(os.path.join(d, "hidden"), exist_ok=True)
    for rel, content in (ws_files or {}).items():
        p = os.path.join(d, "workspace", rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb" if isinstance(content, bytes) else "w", **({} if isinstance(content, bytes) else {"encoding": "utf-8"})).write(content)
    for rel, content in (hidden_files or {}).items():
        p = os.path.join(d, "hidden", rel); os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb" if isinstance(content, bytes) else "w", **({} if isinstance(content, bytes) else {"encoding": "utf-8"})).write(content)
    for src, dst in (copytrees or []):
        dst = os.path.join(d, dst); os.makedirs(os.path.dirname(dst), exist_ok=True)
        (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
    json.dump({"task_id": tid, **meta}, open(os.path.join(d, "task.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return {"task_id": tid, **{k: v for k, v in meta.items() if k != "prompt"}}


def mcq_prompt(q, opts):
    letters = "ABCDEFGHIJ"
    return q.strip() + "\n\n" + "\n".join(f"{letters[i]}. {o}" for i, o in enumerate(opts)) + MCQ_TAIL


def build_mmlu_pro(raw, out, pool, rng):
    import pandas as pd
    df = pd.read_parquet(os.path.join(raw, "mmlu_pro_test.parquet"))
    by = defaultdict(list)
    for r in df.itertuples():
        by[r.category].append((int(r.question_id), (r.question, list(r.options), r.answer, r.src)))
    rows = []
    for s in pick(rng, by, QUOTA["mmlu_pro"], pool):
        q, opts, ans, src = s["payload"]
        tid = f"know-mmlupro-{s['source_id']}"
        rows.append(write_task(out, tid, {
            "dimension": "知识", "source": "MMLU-Pro (TIGER-Lab/MMLU-Pro, test)", "source_id": str(s["source_id"]),
            "license": "MIT", "stratum": s["stratum"], "role": s["role"], "grader": "mcq",
            "prompt": mcq_prompt(q, opts), "timeout_s": 600},
            hidden_files={"answer.json": json.dumps({"answer": ans})}))
    return rows


def build_ceval(raw, out, pool, rng):
    import pandas as pd
    items = []
    for subj in CEVAL_HARD:
        df = pd.read_parquet(os.path.join(raw, "ceval", f"{subj}.val.parquet"))
        for r in df.itertuples():
            items.append((f"{subj}/val/{int(r.id)}", (r.question, [r.A, r.B, r.C, r.D], r.answer)))
    rows = []
    for s in pick(rng, {"*": items}, QUOTA["ceval_hard"], pool):
        q, opts, ans = s["payload"]
        tid = "know-ceval-" + s["source_id"].replace("/", "-")
        rows.append(write_task(out, tid, {
            "dimension": "知识", "source": "C-Eval Hard 子集 (ceval/ceval-exam, val)", "source_id": s["source_id"],
            "license": "CC BY-NC-SA 4.0", "stratum": s["source_id"].split("/")[0], "role": s["role"],
            "grader": "mcq", "prompt": mcq_prompt(q, opts), "timeout_s": 600},
            hidden_files={"answer.json": json.dumps({"answer": ans})}))
    return rows


def lcb_decode(s):
    try:
        return json.loads(s)
    except Exception:
        return json.loads(pickle.loads(zlib.decompress(base64.b64decode(s.encode("utf-8")))))


def build_lcb(raw, out, pool, rng):
    path = os.path.join(raw, "lcb_test6.jsonl")
    by = defaultdict(list)
    for line in open(path, encoding="utf-8"):  # 第一遍只拿元数据，省内存
        x = json.loads(line)
        by[x["difficulty"]].append((x["question_id"], None))
    chosen = {s["source_id"]: s for s in pick(rng, by, QUOTA["lcb_v6"], pool)}
    rows = []
    for line in open(path, encoding="utf-8"):  # 第二遍只解码被选中的题
        x = json.loads(line)
        s = chosen.get(x["question_id"])
        if not s:
            continue
        tests = json.loads(x["public_test_cases"]) + lcb_decode(x["private_test_cases"])
        meta = json.loads(x["metadata"] or "{}")
        functional = tests[0]["testtype"] == "functional"
        if functional:
            how = (f"请补全当前目录 solution.py 里已给出的 starter code（LeetCode 风格，类名 Solution，方法 {meta.get('func_name')}）。"
                   "可以 `from typing import *`。")
            starter = "from typing import *\n\n" + x["starter_code"]
        else:
            how = "请在当前目录写 solution.py：从标准输入读入、向标准输出打印（AtCoder 风格），用 Python 3。"
            starter = "# 从 stdin 读入，向 stdout 输出\n"
        prompt = (f"编程题：{x['question_title']}\n\n{x['question_content']}\n\n{how}\n"
                  "隐藏测试含更多用例并有时间限制（每个用例约 6 秒）。完成后不需要解释。")
        tid = f"code-lcb-{x['question_id']}"
        rows.append(write_task(out, tid, {
            "dimension": "编程", "source": "LiveCodeBench code_generation_lite release_v6 (2025-01~2025-04)",
            "source_id": x["question_id"], "license": "CC（数据集卡）；题面版权归 LeetCode/AtCoder，仅限评测研究",
            "stratum": s["stratum"], "platform": x["platform"], "role": s["role"],
            "grader": "lcb_functional" if functional else "lcb_stdio", "func_name": meta.get("func_name"),
            "prompt": prompt, "timeout_s": 1500},
            ws_files={"solution.py": starter},
            hidden_files={"tests.json": json.dumps(tests)}))
    return rows


def build_polyglot(raw, out, pool, rng):
    base = os.path.join(raw, "polyglot")
    by = {lang: [(n, None) for n in os.listdir(os.path.join(base, lang, "exercises", "practice"))]
          for lang in ("python", "javascript")}
    rows = []
    for s in pick(rng, by, QUOTA["polyglot"], pool):
        lang, name = s["stratum"], s["source_id"]
        ex = os.path.join(base, lang, "exercises", "practice", name)
        cfg = json.load(open(os.path.join(ex, ".meta", "config.json")))
        docs = ""
        for f in ("introduction.md", "instructions.md", "instructions.append.md"):
            p = os.path.join(ex, ".docs", f)
            if os.path.exists(p):
                docs += open(p, encoding="utf-8").read() + "\n\n"
        sol = cfg["files"]["solution"]; tests = cfg["files"]["test"]
        ws = {}
        for f in os.listdir(ex):  # 除测试文件和 .meta 以外全部给 agent（含 package.json 等）
            if f in tests or f in (".meta", ".docs"):
                continue
            ws[f] = os.path.join(ex, f)
        tid = f"code-poly-{lang}-{name}"
        prompt = (f"{docs}\n请修改当前目录下的 {', '.join(sol)} 完成上述练习，只用标准库"
                  f"{'（不要新增 npm 依赖）' if lang == 'javascript' else ''}。"
                  "判分时会放入隐藏的单元测试运行；不要改函数/类名。")
        copies = [(p, os.path.join("workspace", f)) for f, p in ws.items()]
        copies += [(os.path.join(ex, t), os.path.join("hidden", t)) for t in tests]
        rows.append(write_task(out, tid, {
            "dimension": "编程", "source": "Aider Polyglot (Aider-AI/polyglot-benchmark, 源自 Exercism)",
            "source_id": f"{lang}/{name}", "license": "MIT（Exercism 各语言 track）",
            "stratum": lang, "role": s["role"], "grader": f"polyglot_{lang}", "test_files": tests,
            "prompt": prompt, "timeout_s": 1500}, copytrees=copies))
    return rows


def build_ssb(raw, out, pool, rng):
    base = os.path.join(raw, "ssb", "spreadsheetbench_verified_400")
    data = json.load(open(os.path.join(base, "dataset.json")))
    by = defaultdict(list)
    for x in data:
        by[x["instruction_type"]].append((str(x["id"]), x))
    rows = []
    for s in pick(rng, by, QUOTA["ssb_v400"], pool):
        x = s["payload"]; d = os.path.join(base, x["spreadsheet_path"])
        files = sorted(os.listdir(d))
        init = [f for f in files if "_init." in f or f.startswith("initial.")][0]
        gold = [f for f in files if "golden." in f][0]
        ext = init.rsplit(".", 1)[1]
        prompt = (f"日常办公·表格任务。当前目录有 input.{ext}。需求如下：\n\n{x['instruction']}\n\n"
                  f"请把处理后的工作簿另存为当前目录的 output.{ext}（保留其它内容不变）。"
                  f"判分只看{('工作表「' + x['answer_sheet'] + '」的 ') if x.get('answer_sheet') else ''}{x['answer_position']} 区域的单元格值，"
                  "所以请把结果写成具体数值/文本（如用公式，确保保存了计算结果）。")
        tid = f"office-ssb-{x['id']}"
        rows.append(write_task(out, tid, {
            "dimension": "日常办公", "source": "SpreadsheetBench Verified-400 (KAKA22/SpreadsheetBench)",
            "source_id": str(x["id"]), "license": "CC BY-SA 4.0", "stratum": s["stratum"], "role": s["role"],
            "grader": "ssb_cells", "answer_sheet": x.get("answer_sheet"), "answer_position": x["answer_position"], "ext": ext,
            "prompt": prompt, "timeout_s": 1200},
            copytrees=[(os.path.join(d, init), f"workspace/input.{ext}"), (os.path.join(d, gold), f"hidden/golden.{ext}")]))
    return rows


def build_longbench(raw, out, pool, rng):
    import ijson
    path = os.path.join(raw, "longbench_v2.json")
    by = defaultdict(list)
    with open(path, "rb") as f:
        for x in ijson.items(f, "item"):
            if x["length"] == "short":
                by[x["difficulty"]].append((x["_id"], x["domain"]))
    chosen = {s["source_id"]: s for s in pick(rng, by, QUOTA["longbench_v2_short"], pool)}
    rows = []
    with open(path, "rb") as f:
        for x in ijson.items(f, "item"):
            s = chosen.get(x["_id"])
            if not s:
                continue
            opts = [x["choice_A"], x["choice_B"], x["choice_C"], x["choice_D"]]
            # v1.8.1：LongBench v2 抽到的正式题答案全是 B/C，只在两项里猜就有 50%。按固定种子洗牌选项，答案字母跟着换。
            order = list(range(4)); random.Random(f"{SEED}-lb2-opts-{x['_id']}").shuffle(order)
            ans = "ABCD"[order.index("ABCD".index(x["answer"]))]
            opts = [opts[i] for i in order]
            prompt = ("长文阅读题。材料在当前目录的 context.txt（很长，请自己决定怎么读）。\n\n问题："
                      + mcq_prompt(x["question"], opts))
            tid = f"long-lb2-{x['_id']}"
            rows.append(write_task(out, tid, {
                "dimension": "长任务注意力", "source": "LongBench v2 (zai-org/LongBench-v2, length=short)",
                "source_id": x["_id"], "license": "Apache-2.0", "stratum": x["difficulty"],
                "domain": x["domain"], "context_chars": len(x["context"]), "role": s["role"],
                "grader": "mcq", "options_shuffled": True, "prompt": prompt, "timeout_s": 1200},
                ws_files={"context.txt": x["context"]},
                hidden_files={"answer.json": json.dumps({"answer": ans})}))
    return rows


def build_gpqa(raw, out, pool, rng):
    """GPQA Diamond 是“门控”数据集（需登录 Hugging Face 同意条款），不能替你下载/转发。
    在本机：huggingface-cli login 后下载 Idavidrein/gpqa 的 gpqa_diamond.csv 放到 raw/ 再运行本函数。
    作者要求：不要把题目明文发到网上（防止进入训练数据），所以生成的题目目录不要推到公开仓库。"""
    path = os.path.join(raw, "gpqa_diamond.csv")
    if not os.path.exists(path):
        print("  跳过 gpqa_diamond：缺少", path); return []
    import csv as _csv
    by = defaultdict(list)
    for r in _csv.DictReader(open(path, encoding="utf-8")):
        by[r["High-level domain"]].append((r["Record ID"], r))
    rows = []
    for s in pick(rng, by, QUOTA["gpqa_diamond"], pool):
        r = s["payload"]
        opts = [r["Correct Answer"], r["Incorrect Answer 1"], r["Incorrect Answer 2"], r["Incorrect Answer 3"]]
        order = list(range(4)); random.Random(f"{SEED}-gpqa-{s['source_id']}").shuffle(order)  # 选项顺序也固定打乱
        ans = "ABCD"[order.index(0)]
        tid = f"know-gpqa-{s['source_id']}"
        rows.append(write_task(out, tid, {
            "dimension": "知识", "source": "GPQA Diamond (Idavidrein/gpqa, 门控)", "source_id": s["source_id"],
            "license": "CC BY 4.0（门控；勿公开转载题目）", "stratum": s["stratum"], "role": s["role"], "grader": "mcq",
            "prompt": mcq_prompt(r["Question"], [opts[i] for i in order]), "timeout_s": 900},
            hidden_files={"answer.json": json.dumps({"answer": ans})}))
    return rows


BUILDERS = {"gpqa_diamond": build_gpqa, "mmlu_pro": build_mmlu_pro, "ceval_hard": build_ceval, "lcb_v6": build_lcb,
            "polyglot": build_polyglot, "ssb_v400": build_ssb, "longbench_v2_short": build_longbench}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--pool", type=float, default=1.3)
    ap.add_argument("--only", nargs="*", default=list(BUILDERS))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for name in a.only:
        rng = random.Random(f"{SEED}-{name}")  # 每个来源独立的随机流：改一个来源不影响其它来源的抽样
        rows = BUILDERS[name](a.raw, a.out, a.pool, rng)
        with open(os.path.join(a.out, f"_manifest_{name}.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(name, len(rows), "tasks", sum(r["role"] == "main" for r in rows), "main")
