#!/usr/bin/env python3
"""v1.9 评分阶段：读作答记录 attempts_*.jsonl，给每份存档的答卷判分，写 results_*.jsonl。

为什么要和作答分开（通俗说法）：
- 考试时不该有标准答案在考场里。作答阶段从头到尾不解答案、不跑判分器；评分阶段才把答案逐题临时解出来。
- 判分器有 bug 或换了判分规则，只要重新评分（--regrade），不用花钱让模型重跑。
- 评分可以换一台工具齐全的机器做（比如桌面端作答、WSL 评分），各臂用同一台机器、同一套工具评，公平。

用法：python grade_runs.py --runs runs [--tasks ../tasks] [--regrade] [--prefix long-lb2-]
每份答卷存放在 <runs>/_logs/<随机ID>/work。判分在它的临时副本上做，存档本身不会被改，所以可以反复重评。
"""
import argparse, glob, json, os, shutil, socket, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import grade  # noqa: E402

TASKS = os.path.join(os.path.dirname(HERE), "tasks")


def _lines(fp):
    for l in open(fp, encoding="utf-8"):
        try:
            yield json.loads(l)
        except Exception:
            continue


def _host():
    try:
        import isolation
        return isolation.host_platform({})
    except Exception:
        return sys.platform


def grade_one(att, tasks_dir, runs_dir):
    """一条作答记录 → 一条结果记录（作答字段原样保留，补上 pass/frac/detail 和评分信息）。"""
    r = dict(att)
    r["graded_at"] = time.time(); r["graded_on"] = _host(); r["grader_node"] = socket.gethostname()
    if att.get("graded") is False:  # 没真正跑过（被拦下 / 崩溃 / dry），不判分
        r.update({"pass": False, "frac": 0.0})
        r["detail"] = att.get("detail") or {"skipped": att.get("status")}
        return r
    tdir = os.path.join(tasks_dir, att["task_id"])
    work = os.path.join(runs_dir, "_logs", att["rid"], "work")
    if not os.path.isdir(tdir) or not os.path.isdir(work):
        r.update({"pass": False, "frac": 0.0, "graded": False, "status": "work_missing",
                  "detail": {"error": "题目目录或答卷存档不存在", "task_dir": os.path.isdir(tdir), "work": os.path.isdir(work)}})
        return r
    tmp = tempfile.mkdtemp(prefix="dshb_regrade_")
    try:
        cp = os.path.join(tmp, "work")
        shutil.copytree(work, cp, symlinks=True)  # 在副本上判：判分器会往目录里放测试、跑代码，存档要保持原样
        g = grade.grade(tdir, cp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    r.update({"pass": g["pass"], "frac": g["frac"], "detail": g["detail"]})
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True); ap.add_argument("--tasks", default=TASKS)
    ap.add_argument("--regrade", action="store_true", help="已评过的也重新评（旧结果先备份）")
    ap.add_argument("--prefix", nargs="*", help="只评这些题号前缀")
    a = ap.parse_args()
    runs = os.path.abspath(a.runs)
    atts = []
    for fp in sorted(glob.glob(os.path.join(runs, "attempts_*.jsonl"))):
        atts += list(_lines(fp))
    # 同一 rid 出现多次（文件被复制过 / 续跑写了两次）只判一次：分析链路按 (臂, 题, 遍) 去重能兜住，
    # 但 calibrate.py 与 sample_size.py 是按行读的，重复行会改变「两次预跑全对/全错」的判定（审计 P2-3）。
    _seen, _uniq = set(), []
    for x in atts:
        rid = x.get("rid")
        if not rid or rid in _seen:
            continue
        _seen.add(rid)
        _uniq.append(x)
    if len(_uniq) != len(atts):
        print("去重：%d 条作答里有 %d 条重复 rid，已按首次出现保留" % (len(atts), len(atts) - len(_uniq)))
    atts = _uniq
    if a.prefix:
        atts = [x for x in atts if any(x.get("task_id", "").startswith(p) for p in a.prefix)]
    done = set()
    res_files = sorted(glob.glob(os.path.join(runs, "results_*.jsonl")))
    for fp in res_files:
        for r in _lines(fp):
            if r.get("rid"):
                done.add(r["rid"])
    att_rids = {x.get("rid") for x in atts}
    if a.regrade:
        bak = os.path.join(runs, f"_regrade_backup_{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(bak, exist_ok=True)
        for fp in res_files:
            shutil.copy(fp, bak)
        # 旧版（跑完马上评分）留下的结果没有作答记录：答卷还在的，把它当作答记录重新评
        for fp in res_files:
            keep = []
            for r in _lines(fp):
                hit = (not a.prefix) or any(r.get("task_id", "").startswith(p) for p in a.prefix)
                if hit and r.get("rid") not in att_rids and os.path.isdir(os.path.join(runs, "_logs", str(r.get("rid")), "work")):
                    x = {k: v for k, v in r.items() if k not in ("pass", "frac", "detail", "graded_at", "graded_on", "grader_node")}
                    atts.append(x); att_rids.add(x["rid"])
                elif not (hit and r.get("rid") in att_rids):
                    keep.append(r)  # 不在本次重评范围、或答卷已丢的，原样保留
            with open(fp, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in keep)
        done = set()
        print(f"旧结果已备份到 {os.path.relpath(bak, runs)}/")
    todo = [x for x in atts if x.get("rid") not in done]
    if not todo:
        print(f"{os.path.basename(runs)}：没有待评分的答卷。")
        return 0
    print(f"{os.path.basename(runs)}：待评分 {len(todo)} 份（逐题串行，避免并行抢 CPU 让编程题超时）")
    n_pass = n_err = 0; t0 = time.time()
    for i, x in enumerate(todo, 1):
        try:
            r = grade_one(x, a.tasks, runs)
        except Exception as ex:  # 评分阶段的异常不影响作答记录，修好后再跑 grade 即可补评
            print(f"  ✘ {x.get('task_id')} {x.get('rid')} 评分异常：{type(ex).__name__}: {str(ex)[:150]}")
            n_err += 1; continue
        n_pass += bool(r["pass"]); n_err += "grader_error" in (r.get("detail") or {})
        fp = os.path.join(runs, f"results_{r['arm']}_r{r['rep']}.jsonl")
        with open(fp, "a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        if i % 25 == 0 or i == len(todo):
            print(f"  已评 {i}/{len(todo)}，对 {n_pass}，用时 {time.time() - t0:.0f}s")
    if n_err:
        print(f"  注意：{n_err} 份判分器报错（detail 里有 grader_error）。多半是评分机缺工具（pytest/node/openpyxl），装好后 grade --regrade。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
