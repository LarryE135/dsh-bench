#!/usr/bin/env python3
"""抓回「披露但没随仓库发布」的第三方题目。

仓库里只放**题号 + 数据集 + 修订号**（manifest/upstream.json），题面/选项/答案一律不进仓库：
C-Eval 是 CC BY-NC-SA 4.0（再分发要同协议、还不能商用），LCB 是"仅限研究"且题面版权在竞赛站手上。
要复核或复现全量题单，就在自己机器上抓一次 —— 抓取与使用都发生在你本地，没有再分发，
许可义务自然落在你自己的使用范围里。

只用标准库：走 HuggingFace 的 datasets-server rows 接口取行，不需要 pandas / pyarrow。

用法：
    python bench/fetch_data.py --provider ceval            # 抓取 + 重建到 tasks/
    python bench/fetch_data.py --provider ceval --check    # 只核对内容哈希，不写 tasks/

抓完重打答案包（新题的答案在本地生成，仓库里的 _answers.bin 不含它们）：
    python bench/answers.py pack tasks && python bench/check_tasks.py
"""
import argparse, hashlib, json, os, sys, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPSTREAM = os.path.join(ROOT, "manifest", "upstream.json")
ROWS_API = "https://datasets-server.huggingface.co/rows"


def _json(url, tries=4):
    """datasets-server 的 rows 接口会间歇性 502，退避重试（实测一次抓取里能碰上两次）。"""
    import time
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dsh-bench-fetch/1"})
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise SystemExit(f"  取数据失败（已重试 {tries} 次）：{last}")


def _rows(dataset, config, split, want_ids=None, page=100):
    """按页取行，直到取完整个 split（或已拿到想要的 id）。"""
    out, off = [], 0
    while True:
        q = urllib.parse.urlencode({"dataset": dataset, "config": config, "split": split,
                                    "offset": off, "length": page})
        j = _json(f"{ROWS_API}?{q}")
        batch = [r["row"] for r in j.get("rows", [])]
        out += batch
        off += len(batch)
        total = j.get("num_rows_total") or 0
        if not batch or off >= total:
            break
        if want_ids and set(want_ids) <= {int(r["id"]) for r in out}:
            break
    return out


def _task_json_hash(tid, meta):
    """按 build_public.write_task 的落盘格式算哈希（不落盘），用来核对抓回来的还是不是同一道题。"""
    blob = json.dumps({"task_id": tid, **meta}, ensure_ascii=False, indent=1).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def fetch_ceval(pv, tasks, check=False):
    import build_public as B

    by_subj = {}
    for t in tasks:
        by_subj.setdefault(t["subject"], []).append(t)
    bad = []
    for subj, items in sorted(by_subj.items()):
        want = {int(t["row_id"]) for t in items}
        rows = {int(r["id"]): r for r in _rows(pv["dataset"], subj, "val", want_ids=want)}
        print(f"  {subj}: 取到 {len(rows)} 行，需要 {len(want)} 道")
        for t in sorted(items, key=lambda x: x["task_id"]):
            r = rows.get(int(t["row_id"]))
            if r is None:
                bad.append((t["task_id"], f"数据集里没有 id={t['row_id']}"))
                continue
            meta = {"dimension": t["dimension"], "source": pv["source_label"], "source_id": t["source_id"],
                    "license": pv["license"], "stratum": t["stratum"], "role": t["role"],
                    "grader": t["grader"], "prompt": B.mcq_prompt(r["question"], [r["A"], r["B"], r["C"], r["D"]]),
                    "timeout_s": t["timeout_s"]}
            h = _task_json_hash(t["task_id"], meta)
            if h != t["task_json_sha256"]:
                bad.append((t["task_id"], "重建出来的题面与仓库里记录的哈希不一致"
                                          f"（记录 {t['task_json_sha256'][:12]}，实得 {h[:12]}）——上游数据可能变了"))
                continue
            if not check:
                B.write_task(os.path.join(ROOT, "tasks"), t["task_id"], meta,
                             hidden_files={"answer.json": json.dumps({"answer": r["answer"]})})
    return bad


PROVIDERS = {"ceval": fetch_ceval}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, choices=sorted(PROVIDERS))
    ap.add_argument("--check", action="store_true", help="只核对内容哈希，不写 tasks/")
    a = ap.parse_args()
    spec = json.load(open(UPSTREAM, encoding="utf-8"))
    pv = spec["providers"][a.provider]
    tasks = [t for t in spec["tasks"] if t["provider"] == a.provider]
    if not tasks:
        sys.exit(f"  upstream.json 里没有 {a.provider} 的题")
    print(f"{a.provider}：{len(tasks)} 道 / {pv['dataset']}@{pv['revision'][:12]} / {pv['license']}")
    bad = PROVIDERS[a.provider](pv, tasks, check=a.check)
    if bad:
        print(f"  ✖ {len(bad)} 道对不上：")
        for tid, why in bad:
            print(f"    {tid}: {why}")
        return 1
    print(f"  ✔ {len(tasks)} 道全部与仓库记录的哈希一致（{'只核对' if a.check else '已重建到 tasks/'}）")
    if not a.check:
        print("  下一步：python bench/answers.py pack tasks && python bench/check_tasks.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
