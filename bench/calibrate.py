#!/usr/bin/env python3
"""预跑校准：用“标准臂”在候选池上预跑 2 次的结果，定下正式题单。

规则（用户 2026-11-03 决定：简单题保留，当“防退化”底座）：
  1. 判分器报错（grader_error）或 agent 每次都超时/崩溃的题 → 记为“坏题”，剔除并人工复查；
  2. 两次预跑全错的题 → 剔除（对比两臂时它们几乎不提供信息，还白花钱）；
  3. 全对的题 → 保留，标记为 easy 层；部分对的 → medium 层；
  4. 被剔除的正式题，由同来源同分层的候补题按候补顺序补位（候补本身也要有预跑结果且不是全错）。
只用标准臂预跑来选题，不看插件臂的表现——否则等于“挑对插件有利的题”，结果会偏。

用法：python calibrate.py --tasks ../tasks --runs runs_pre --arm std --out ../manifest/final_ids.json --apply
"""
import argparse, json, os
from collections import defaultdict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", required=True); ap.add_argument("--runs", required=True)
    ap.add_argument("--arm", default="std"); ap.add_argument("--out", required=True)
    ap.add_argument("--apply", action="store_true", help="把结果写回各题 task.json：入选=main，其余=dropped")
    a = ap.parse_args()
    tasks = {}
    for tid in os.listdir(a.tasks):
        p = os.path.join(a.tasks, tid, "task.json")
        if os.path.isfile(p):
            tasks[tid] = json.load(open(p, encoding="utf-8"))
    runs = defaultdict(list)
    import glob
    for fp in glob.glob(os.path.join(a.runs, f"results_{a.arm}_r*.jsonl")):
        for l in open(fp, encoding="utf-8"):
            r = json.loads(l)
            if r.get("graded", True) is False or r.get("status") in ("leak_blocked", "dry"):
                continue  # v1.5：没真正跑过的不参与选题（否则会被当成“坏题”剔除）
            runs[r["task_id"]].append(r)
    verdict = {}
    for tid, rs in runs.items():
        if any("grader_error" in r["detail"] for r in rs) or all(r["status"] != "ok" for r in rs):
            verdict[tid] = "broken"
        elif not any(r["pass"] for r in rs):
            verdict[tid] = "all_wrong"
        elif all(r["pass"] for r in rs):
            verdict[tid] = "easy"
        else:
            verdict[tid] = "medium"
    groups = defaultdict(list)  # (source, stratum) -> [task...] 按 main 在前、reserve 按 rank
    for t in tasks.values():
        groups[(t["source"], t.get("stratum"))].append(t)
    final, log = [], []
    for key, ts in sorted(groups.items(), key=lambda kv: str(kv[0])):
        mains = [t for t in ts if t.get("role") == "main"]; reserves = [t for t in ts if t.get("role") != "main"]
        reserves.sort(key=lambda t: t["task_id"])
        keep = [t for t in mains if verdict.get(t["task_id"]) in ("easy", "medium")]
        for t in mains:  # v1.5：没有预跑记录的正式题（例如预跑后才加入的 GPQA）不再被悄悄剔除：保留并标“未预跑”
            if t["task_id"] not in verdict:
                verdict[t["task_id"]] = "unpiloted"; keep.append(t)
                log.append(f"注意：{t['task_id']} 没有预跑记录，按原样保留（层=unpiloted）；建议补跑 pre 后再 calibrate")
        need = len(mains) - len(keep)
        for t in reserves:
            if need <= 0:
                break
            if verdict.get(t["task_id"]) in ("easy", "medium"):
                keep.append(t); need -= 1; log.append(f"补位 {t['task_id']} ← {key}")
        if need > 0:
            log.append(f"注意：{key} 仍缺 {need} 题（候补不够），正式题数相应减少")
        final += [{"task_id": t["task_id"], "layer": verdict[t["task_id"]]} for t in keep]
    for tid, v in sorted(verdict.items()):
        if v in ("broken", "all_wrong"):
            log.append(f"剔除 {tid}: {v}")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump({"final": final, "log": log}, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if a.apply:
        chosen = {f["task_id"]: f["layer"] for f in final}
        for tid, t in tasks.items():
            t["role"] = "main" if tid in chosen else "dropped"; t["layer"] = chosen.get(tid, verdict.get(tid))
            json.dump(t, open(os.path.join(a.tasks, tid, "task.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    lay = defaultdict(int)
    for f in final:
        lay[f["layer"]] += 1
    print(f"正式题 {len(final)} 道，分层 {dict(lay)}"); print("\n".join(log))


if __name__ == "__main__":
    main()
