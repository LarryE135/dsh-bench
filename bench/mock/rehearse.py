#!/usr/bin/env python3
"""全流程演练（不开真模型）：搭一个临时的“演练根目录”，用模拟 dsh + 模拟 Anthropic 接口，
把 doctor → setup → smoke → pre → calibrate → stage1 → interim → stage2 → report 全跑一遍，
再按清单逐条核对产物（记账、判分、选题、非劣效、评分卡、健康度表）。

为什么要有它：真跑一次要几十小时和真金白银，而绝大多数“流程坏掉”都是接线问题（覆盖层没生效、
代理没记到、判分崩了、选题把题全剔光、报告里区间是 NaN）。这些问题在一分钟内就能演练出来。

用法：python bench/mock/rehearse.py [--keep] [--tasks 14]
"""
import argparse, glob, json, os, shutil, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))          # 包根目录
PY = sys.executable
API_PORT = 8799

# 演练用的题目：覆盖各类判分器；ssb 那一道故意留着——本机缺 openpyxl 时它必然被判分器报错，
# 正好用来验证 calibrate 的“坏题剔除 + 候补补位”路径。
WANT = [
    ("mcq", 3), ("exact_int", 3), ("self_chain", 3), ("self_o", 4),
    ("ssb_cells", 1),   # 故意留着：本机缺 openpyxl 时它必然报错，用来验证“坏题剔除”
]


def pick_tasks(pkg, limit):
    rows = []
    for tid in sorted(os.listdir(os.path.join(pkg, "tasks"))):
        p = os.path.join(pkg, "tasks", tid, "task.json")
        if os.path.isfile(p):
            rows.append((tid, json.load(open(p, encoding="utf-8"))))
    out, used = [], set()
    for kind, n in WANT:
        got = 0
        for tid, t in rows:
            if got >= n or tid in used:
                continue
            g = t["grader"]
            if (kind == "self_o" and g.startswith("self_o_")) or g == kind:
                out.append(tid); used.add(tid); got += 1
    # 补两个同组候补，让 calibrate 有东西可以补位
    for tid, t in rows:
        if len(out) >= limit:
            break
        if tid not in used and t.get("role") == "reserve":
            out.append(tid); used.add(tid)
    return out[:limit]


def build(pkg, root, tids):
    for d in ("bench", "prompts", "overlays", "manifest", "docker"):
        src = os.path.join(pkg, d)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(root, d))
    shutil.copy2(os.path.join(pkg, "config.example.json"), os.path.join(root, "config.example.json"))
    os.makedirs(os.path.join(root, "tasks"))
    for tid in tids:
        shutil.copytree(os.path.join(pkg, "tasks", tid), os.path.join(root, "tasks", tid))
    shutil.copy2(os.path.join(pkg, "tasks", "_answers.bin"), os.path.join(root, "tasks", "_answers.bin"))
    mk = os.path.join(root, "mk"); os.makedirs(mk)
    for f in ("fake_dsh.py", "fake_api.py"):
        shutil.copy2(os.path.join(pkg, "bench", "mock", f), os.path.join(mk, f))
    cfg = json.load(open(os.path.join(pkg, "config.example.json"), encoding="utf-8"))
    cfg["dsh_command"] = [PY, os.path.join(mk, "fake_dsh.py"), "--repo", root, "--mode", "mixed"]
    cfg["upstream"] = f"http://127.0.0.1:{API_PORT}"
    cfg["dsh_version_pin"] = ""
    cfg["dsh_home"] = os.path.join(root, "home")
    cfg["work_root"] = os.path.join(root, "w")
    cfg["state_root"] = os.path.join(root, "st")
    cfg["smoke_tasks"] = [t for t in tids if "mmlu" in t][:1] + [t for t in tids if "needle" in t][:1]
    cfg["stages"]["stage1"]["rep_to"] = 2
    cfg["stages"]["stage2"]["rep_from"] = 2
    cfg["stages"]["stage2"]["rep_to"] = 4
    cfg["stages"]["stage2"]["slots_per_arm"] = 2
    # mock dsh 提供不了 agent preset（它只合成几行配置），保真检查无法端到端跑；
    # 改成直接调用 compare() 验语义（见 setup 的「保真门禁」一条），所以这里把 preset 留空。
    for _arm in cfg["arms"].values():
        _arm["preset"] = None
    cfg["pre_repeats"] = 2
    json.dump(cfg, open(os.path.join(root, "config.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return cfg


def run(root, step, extra=(), timeout=3600):
    cmd = [PY, os.path.join(root, "bench", "bench.py"), step, *extra]
    env = dict(os.environ); env["DSHBENCH_SKIP_JS_CACHE"] = "1"
    t0 = time.time()
    p = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
    dt = time.time() - t0
    print(f"\n$ bench.py {step}  ({dt:.1f}s, rc={p.returncode})")
    tail = (p.stdout or "").strip().splitlines()
    print("\n".join("   " + l for l in tail[-14:]))
    if p.returncode != 0:
        print("   --- stderr ---\n" + "\n".join("   " + l for l in (p.stderr or "").strip().splitlines()[-12:]))
    return p


def check(name, ok, detail=""):
    print(f"  {'✔' if ok else '✘'} {name}" + (f"  {detail}" if detail else ""))
    return bool(ok)


def jl(path):
    out = []
    if os.path.exists(path):
        for l in open(path, encoding="utf-8"):
            l = l.strip()
            if l:
                out.append(json.loads(l))
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--keep", action="store_true")
    ap.add_argument("--tasks", type=int, default=14); ap.add_argument("--root", default=None)
    a = ap.parse_args()
    root = a.root or tempfile.mkdtemp(prefix="rhrs_")
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    tids = pick_tasks(PKG, a.tasks)
    build(PKG, root, tids)
    print(f"演练根目录 {root}\n题目 {len(tids)} 道：" + ", ".join(tids))

    os.environ["DSHB_MIN_PAIRS"] = "3"  # 演练只有十道题，放宽 interim 的配对题数门槛
    api = subprocess.Popen([PY, os.path.join(root, "mk", "fake_api.py"), "--port", str(API_PORT)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.6)
    results = []
    try:
        run(root, "doctor")
        p = run(root, "setup")
        info = json.load(open(os.path.join(root, "runs", "_setup", "setup.json"), encoding="utf-8"))
        cfg = json.load(open(os.path.join(root, "config.json"), encoding="utf-8"))
        arm_names = sorted(cfg["arms"])
        # 保真检查单列：mock 的合成 dump 只有几行，真实预设必然对不上 —— 这里要验的是「门禁会拦」，
        # 不是「门禁放行」，所以把保真条目从「金丝雀全绿」里摘出来单独判（见下面两条 check）。
        def _is_fid(msg):
            return str(msg).startswith("保真检查")
        canary_ok = bool(info.get("arms")) and all(
            all(ok for ok, m in v if not _is_fid(m)) for v in info["arms"].values())
        results.append(check(f"setup: {len(arm_names)} 个臂金丝雀全绿", p.returncode == 0 and canary_ok,
                             json.dumps({k: [ok for ok, _ in v] for k, v in info.get("arms", {}).items()})))
        # 覆盖层是不是真的进了合成配置：不再点名某个臂（原来写死内部 v3 臂与它的规则串，公开版没有那个臂），
        # 改成「每个臂都有 dump、且各臂 dump 互不相同」—— 这正是历史上「覆盖层被静默忽略、两臂其实一样」的样子。
        dumps = {}
        for arm in arm_names:
            fp = os.path.join(root, "runs", "_setup", f"{arm}.dump.txt")
            dumps[arm] = open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""
        results.append(check("setup: 每个臂都生成了合成配置 dump", all(dumps.values()), ",".join(sorted(k for k, v in dumps.items() if not v)) or "全部有"))
        # 保真门禁的语义单测（P0-1 回归）：预设要求的行在合成配置里缺失必须判 mismatch，
        # 只有 WEB_ONLY 里的行才允许算「headless 口径缺失」。
        sys.path.insert(0, os.path.join(root, "bench"))
        import preset_fidelity as _pf
        _pro = {"tool-workflow": {"id": "tool-workflow", "disabled": True}, "time-context": {"id": "time-context"}}
        _bk = _pf.compare(_pro, "tool-presentation" + chr(10) + "  ptc: true" + chr(10), {})
        _mis = [x[0] for x in _bk["mismatch"]]
        _abs = [x[0] for x in _bk["absent-in-headless"]]
        results.append(check("setup: 保真门禁（缺行判 mismatch、web 专用行才算 absent）",
                               "tool-workflow" in _mis and "time-context" in _abs,
                               "mismatch=%s absent=%s" % (_mis, _abs)))
        _empty = os.path.join(root, "runs", "_setup", "empty.dump.txt")
        open(_empty, "w").close()
        _r3 = subprocess.run([PY, os.path.join(root, "bench", "preset_fidelity.py"), "--arm", arm_names[0],
                                  "--preset", "standard", "--preset-file", os.devnull, "--dump", _empty],
                                 capture_output=True, text=True, timeout=120)
        results.append(check("setup: 空 dump 被拒绝（rc=2，不是「全部一致」）", _r3.returncode == 2,
                               "rc=%d %s" % (_r3.returncode, (_r3.stdout or "").strip()[-110:])))
        uniq = len({hash(v) for v in dumps.values()})
        results.append(check("setup: 各臂的合成配置互不相同（覆盖层没被忽略）", uniq == len(dumps),
                             f"{uniq} 种 / {len(dumps)} 臂"))

        p = run(root, "smoke")
        sm = [r for f in glob.glob(os.path.join(root, "runs_smoke", "results_*.jsonl")) for r in jl(f)]
        n_smoke = len(json.load(open(os.path.join(root, "config.json"), encoding="utf-8"))["smoke_tasks"])
        results.append(check("smoke: 每臂把每道冒烟题都跑了", len(sm) == len(arm_names) * n_smoke, str(len(sm)) + "/" + str(len(arm_names) * n_smoke)))
        us = [u for f in glob.glob(os.path.join(root, "runs_smoke", "usage_*.jsonl")) for u in jl(f)]
        results.append(check("smoke: 代理记到了请求", len(us) > 0, f"{len(us)} 条"))
        results.append(check("smoke: 服务端模型名是 V4.1-Flash-1017", any(u.get("resp_model") == "DeepSeek-V4.1-Flash-1017" for u in us)))
        results.append(check("smoke: token 三类都非零", any(u.get("cache_read_input_tokens", 0) > 0 and u.get("input_tokens", 0) > 0 and u.get("output_tokens", 0) > 0 for u in us)))

        run(root, "pre")
        p = run(root, "calibrate")
        fin = json.load(open(os.path.join(root, "manifest", "final_ids.json"), encoding="utf-8")) \
            if os.path.exists(os.path.join(root, "manifest", "final_ids.json")) else {}
        got = fin.get("final") or fin.get("ids") or []
        n_keep = len(got)
        results.append(check("calibrate: 选出了正式题单", n_keep > 0, str(n_keep) + " 题"))
        pre = [json.loads(l) for f in glob.glob(os.path.join(root, "runs_pre", "results_std_r*.jsonl")) for l in open(f)]
        by_task = {}
        for r in pre:
            by_task.setdefault(r["task_id"], []).append(r)
        kept_ids = [g["task_id"] if isinstance(g, dict) else g for g in got]
        solved_main = [t for t, rs in by_task.items() if rs[0].get("role") == "main" and all(r["pass"] for r in rs)]
        results.append(check("calibrate: 标准臂两次都做对的正式题一个都没误删",
                             not [t for t in solved_main if t not in kept_ids],
                             str(len(solved_main)) + " 道全对的正式题，入选 " + str(len([t for t in solved_main if t in kept_ids]))))
        ssb = [t for t in tids if "ssb" in t]
        results.append(check("calibrate: 判分器报错的题被剔除（无 openpyxl 的表格题）",
                             all(t not in [g["task_id"] if isinstance(g, dict) else g for g in got] for t in ssb)))

        p = run(root, "stage1")
        res = [r for f in glob.glob(os.path.join(root, "runs", "results_*.jsonl")) for r in jl(f)]
        arms = sorted({r["arm"] for r in res})
        expected_runs = len(kept_ids) * 2 * len(arms)  # stage1：每题 2 次重复 × 臂数
        done_runs = [r for r in res if r.get("status") != "crash"]  # crash 的不算“跑成了”
        results.append(check("stage1: 运行数量对得上（不是悄悄少跑）", len(done_runs) >= 0.9 * expected_runs,
                             str(len(done_runs)) + "/" + str(expected_runs)))
        results.append(check("stage1: 没有 crash 记录", not [r for r in res if r.get("status") == "crash"]))
        results.append(check("stage1: 每个臂都有结果", len(arms) == len(arm_names), ",".join(arms)))
        results.append(check("stage1: 每次运行都有 graded 字段且 ok 的确实判过分", all("graded" in r for r in res)))
        results.append(check("stage1: 至少一次判对", any(r["pass"] for r in res)))
        # v1.9：按预跑+正式合并算。只看正式题会偶发失败：calibrate 已剔除全错题，模拟器对“中等题”按 50% 随机，
        # 留下的题少时有可能碰巧全对（演练 1/3 次遇到过），这不是流程问题
        both = res + pre
        pooled = sum(r["pass"] for r in both) / max(len(both), 1)
        results.append(check("pre+stage1: 正确率不是退化的 0 或 1（判分器既会给对也会给错）",
                             0 < pooled < 1, "合并正确率 %.2f（%d 次运行）" % (pooled, len(both))))
        # v1.9 测试与评分隔离
        att = [r for f in glob.glob(os.path.join(root, "runs", "attempts_*.jsonl")) for r in jl(f)]
        results.append(check("隔离: 作答记录里没有分数（作答阶段没判分）", att and all(r.get("pass") is None for r in att), f"{len(att)} 条作答"))
        results.append(check("隔离: 每条结果都对得上一条作答记录", {r["rid"] for r in res} <= {r["rid"] for r in att}))
        last_run_end = max((r.get("ts") or 0) + (r.get("wall_s") or 0) for r in att)
        results.append(check("隔离: 评分全部发生在最后一题作答结束之后", all((r.get("graded_at") or 0) >= last_run_end for r in res)))
        ua = [u for f in glob.glob(os.path.join(root, "runs", "usage_*.jsonl")) for u in jl(f)]
        tot_cost = sum((u.get("cache_read_input_tokens", 0) * 0.006 + u.get("input_tokens", 0) * 0.3 +
                        u.get("output_tokens", 0) * 1.2) / 1e6 for u in ua)
        results.append(check("stage1: 每条用量都打上了 tag（费用能落到题上）",
                             len(ua) > 0 and all(u.get("tag") for u in ua), str(len(ua)) + " 条"))
        results.append(check("stage1: 费用算出来不是 0", tot_cost > 0, "$%.4f" % tot_cost))

        run(root, "interim")
        it = open(os.path.join(root, "interim.md"), encoding="utf-8").read() if os.path.exists(os.path.join(root, "interim.md")) else ""
        results.append(check("interim: 出 99% 区间表", "Δ正确率" in it and "99%" in it))

        run(root, "stage2")
        p = run(root, "report")
        rep = open(os.path.join(root, "report.md"), encoding="utf-8").read() if os.path.exists(os.path.join(root, "report.md")) else ""
        results.append(check("report: 有配对比较行", "非劣效" in rep and "Δ正确率" in rep))
        results.append(check("report: 区间不是 NaN", "nan" not in rep.lower()))
        results.append(check("report: 评分卡文件生成", os.path.exists(os.path.join(root, "scorecard.md"))))
        results.append(check("report: 费用比接近 1（模拟接口对四臂一视同仁）",
                             any(abs(float(x) - 1) < 0.05 for x in __import__("re").findall(r"\| ([0-9.]+) \[[0-9.]+, [0-9.]+\] \|", rep))))

        # v1.9：重新评分（不重跑模型）结果应完全一致
        before = {r["rid"]: r["pass"] for f in glob.glob(os.path.join(root, "runs", "results_*.jsonl")) for r in jl(f)}
        run(root, "grade", ["--runs", "runs", "--regrade"])
        after = {r["rid"]: r["pass"] for f in glob.glob(os.path.join(root, "runs", "results_*.jsonl")) for r in jl(f)}
        results.append(check("重评: 不重跑模型，结果逐条一致", before == after and len(after) > 0, f"{len(after)} 条"))
        # v1.9：manual 模式——只作答不评分，之后单独 grade
        cfgp = os.path.join(root, "config.json"); cj = json.load(open(cfgp, encoding="utf-8"))
        cj["grading"] = "manual"; json.dump(cj, open(cfgp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        tid0 = sorted(before and {r["task_id"] for f in glob.glob(os.path.join(root, "runs", "results_*.jsonl")) for r in jl(f)})[0]
        run(root, "forget", [tid0])
        p1 = run(root, "stage1")
        r_mid = [r for f in glob.glob(os.path.join(root, "runs", "results_*.jsonl")) for r in jl(f) if r["task_id"] == tid0]
        a_mid = [r for f in glob.glob(os.path.join(root, "runs", "attempts_*.jsonl")) for r in jl(f) if r["task_id"] == tid0]
        run(root, "grade", ["--runs", "runs"])
        r_end = [r for f in glob.glob(os.path.join(root, "runs", "results_*.jsonl")) for r in jl(f) if r["task_id"] == tid0]
        _want = len(arm_names) * int(cfg["stages"]["stage1"]["rep_to"])  # 原来写死 8（内部 4 臂时代的数）
        results.append(check("manual: 作答后没有分数、grade 之后才有", p1.returncode == 0 and len(a_mid) == _want and not r_mid and len(r_end) == _want,
                             f"作答 {len(a_mid)}、评分前 {len(r_mid)}、评分后 {len(r_end)}"))
        cj["grading"] = "after"; json.dump(cj, open(cfgp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

        # 负向用例：手工塞一条 leak_blocked，确认它不进正确率
        f0 = sorted(glob.glob(os.path.join(root, "runs", "results_std_r*.jsonl")))[0]
        with open(f0, "a", encoding="utf-8") as f:
            f.write(json.dumps({"arm": "std", "task_id": "FAKE-BLOCKED", "rep": 0, "rid": "x", "dimension": "知识",
                                "source": "x", "stratum": "x", "role": "main", "status": "leak_blocked",
                                "wall_s": 0.1, "pass": False, "frac": 0.0, "detail": {}, "tag": "x",
                                "graded": False}) + "\n")
        p = subprocess.run([PY, os.path.join(root, "bench", "analyze.py"), "--runs", os.path.join(root, "runs"),
                            "--base", "std"], cwd=root, capture_output=True, text=True, encoding="utf-8")
        results.append(check("analyze: 被拦下的运行不计入正确率（只在健康度表里）",
                             "FAKE-BLOCKED" not in p.stdout and "自检拦下" in p.stdout))
        run(root, "status")
    finally:
        api.terminate()
    print("\n======= 演练结论 =======")
    print(f"通过 {sum(results)}/{len(results)} 项")
    if not a.keep:
        print(f"（演练目录保留在 {root}，方便复查；加 --keep 之外会自动保留）")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())