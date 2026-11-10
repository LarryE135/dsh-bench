#!/usr/bin/env python3
"""难题批次 hard2（v1.8，2026-11-07）：给标准臂约 95% 的“天花板”加一批更难的题，提高区分度。

为什么要加：标准臂几乎全对时，省钱设置哪怕在难题上掉分也看不出来（答对的题再多也只证明“简单题没退化”）。
原 94 道正式题一道不动（简单题仍是防退化底座），本批题另标 batch=hard2，报告里可以单独看、也可以合并看。

来源（都不需要 Docker，Windows/WSL 都能跑）：
  1. LiveCodeBench release_v6 的 hard 题（排除已入选的题号）            → 编程
  2. LongBench v2 length=short、difficulty=hard（排除已入选的）         → 长任务注意力（长文阅读）
  3. 自建长链路 K=32/36/40/44（更多步骤、规矩只在开头说一次）           → 长任务注意力（零污染）

用法（重活放 /tmp）：
  python bench/build_hard2.py --raw /tmp/raw --out /tmp/hard2 --existing tasks
随机流与原批次独立：Random("20261103-hard2-<来源>")，不影响任何已有题的抽样。
"""
import argparse, json, os, random, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_public as bp
import gen_selfmade as gs

SEED = 20261103
PLAN = {
    "lcb": {"main": 8, "reserve": 4},
    "lb2": {"main": 4, "reserve": 2},
    "lcb_min_cases": 20,  # 审计：隐藏测试太少（如 abc400_e 只有 5 组小输入）测不出效率，判分偏松，不选
    "chain": [(32, "main"), (36, "main"), (40, "main"), (44, "main"), (34, "reserve"), (38, "reserve")],
}


def pick(rng, ids, plan, ok=lambda i: True):
    """先按固定种子把全部候选打乱，再顺序取用；不合格（ok 为假）的跳过、由下一个顶上，其余入选顺序不变。"""
    ids = sorted(ids); rng.shuffle(ids)
    n_main, n_res = plan["main"], plan["reserve"]
    good = [i for i in ids if ok(i)][:n_main + n_res]
    return {i: ("main" if k < n_main else "reserve") for k, i in enumerate(good)}


def tag(meta):
    meta["batch"] = "hard2"
    return meta


def build_lcb(raw, out, existing):
    path = os.path.join(raw, "lcb_test6.jsonl")
    ids, ncase = [], {}
    for line in open(path, encoding="utf-8"):
        x = json.loads(line)
        if x["difficulty"] == "hard" and f"code-lcb-{x['question_id']}" not in existing:
            ids.append(x["question_id"])
            ncase[x["question_id"]] = len(json.loads(x["public_test_cases"])) + len(bp.lcb_decode(x["private_test_cases"]))
    chosen = pick(random.Random(f"{SEED}-hard2-lcb"), ids, PLAN["lcb"], ok=lambda i: ncase[i] >= PLAN["lcb_min_cases"])
    rows = []
    for line in open(path, encoding="utf-8"):
        x = json.loads(line)
        role = chosen.get(x["question_id"])
        if not role:
            continue
        tests = json.loads(x["public_test_cases"]) + bp.lcb_decode(x["private_test_cases"])
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
        rows.append(bp.write_task(out, f"code-lcb-{x['question_id']}", tag({
            "dimension": "编程", "source": "LiveCodeBench code_generation_lite release_v6 (2025-01~2025-04)",
            "source_id": x["question_id"], "license": "CC（数据集卡）；题面版权归 LeetCode/AtCoder，仅限评测研究",
            "stratum": "hard", "platform": x["platform"], "role": role,
            "grader": "lcb_functional" if functional else "lcb_stdio", "func_name": meta.get("func_name"),
            "prompt": prompt, "timeout_s": 1500}),
            ws_files={"solution.py": starter}, hidden_files={"tests.json": json.dumps(tests)}))
    return rows


def build_lb2(raw, out, existing):
    import ijson
    path = os.path.join(raw, "longbench_v2.json")
    ids = []
    with open(path, "rb") as f:
        for x in ijson.items(f, "item"):
            if x["length"] == "short" and x["difficulty"] == "hard" and f"long-lb2-{x['_id']}" not in existing:
                ids.append(x["_id"])
    chosen = pick(random.Random(f"{SEED}-hard2-lb2"), ids, PLAN["lb2"])
    rows = []
    with open(path, "rb") as f:
        for x in ijson.items(f, "item"):
            role = chosen.get(x["_id"])
            if not role:
                continue
            opts = [x["choice_A"], x["choice_B"], x["choice_C"], x["choice_D"]]
            # 审计：LongBench v2 正式题的标准答案只有 B、C（原批 8 道 + 本批 4 道全是 B/C），只在两项里猜就有 50%。
            # 这里按固定种子把选项洗牌，答案字母跟着换，消除位置偏差。
            order = list(range(4)); random.Random(f"{SEED}-hard2-lb2-opts-{x['_id']}").shuffle(order)
            ans = "ABCD"[order.index("ABCD".index(x["answer"]))]
            opts = [opts[i] for i in order]
            prompt = "长文阅读题。材料在当前目录的 context.txt（很长，请自己决定怎么读）。\n\n问题：" + bp.mcq_prompt(x["question"], opts)
            rows.append(bp.write_task(out, f"long-lb2-{x['_id']}", tag({
                "dimension": "长任务注意力", "source": "LongBench v2 (zai-org/LongBench-v2, length=short)",
                "source_id": x["_id"], "license": "Apache-2.0", "stratum": "hard", "domain": x["domain"],
                "context_chars": len(x["context"]), "role": role, "grader": "mcq", "options_shuffled": True, "prompt": prompt, "timeout_s": 1200}),
                ws_files={"context.txt": x["context"]}, hidden_files={"answer.json": json.dumps({"answer": ans})}))
    return rows


def build_chain(out, existing):
    rows = []
    for j, (K, role) in enumerate(PLAN["chain"]):
        i = 100 + j  # 与原批次的 i=0..9 错开，随机流不重叠
        tid = f"long-chain-{K:02d}-{i}"
        if tid in existing:
            continue
        p, ws, exp = gs.chain_task(random.Random(f"{SEED}-hard2-chain-{K}-{i}"), K)
        rows.append(gs.task(out, tid, tag({
            "dimension": "长任务注意力", "source": "自建长链路多步指令", "source_id": f"chain/K={K}#{i}", "stratum": f"K{K}",
            "role": role, "grader": "self_chain", "prompt": p, "timeout_s": 2400}),
            ws, {"expected.json": json.dumps(exp, ensure_ascii=False)}))
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--existing", required=True, help="已有题目目录（排除已入选的题号）")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    existing = {d for d in os.listdir(a.existing) if os.path.isdir(os.path.join(a.existing, d))}
    rows = build_lcb(a.raw, a.out, existing) + build_lb2(a.raw, a.out, existing) + build_chain(a.out, existing)
    with open(os.path.join(a.out, "_manifest_hard2.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("hard2", len(rows), "tasks", sum(r["role"] == "main" for r in rows), "main")
