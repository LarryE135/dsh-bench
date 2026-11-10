#!/usr/bin/env python3
"""样本量/重复次数估算（模拟法）：给定每题的“答对概率”分布，算出在不同题数 n、重复次数 r 下，
证明“非劣效（Δ 下限 > −margin）”的把握（统计功效）。

两种用法：
  python sample_size.py                       # 用设计阶段的假设分布（见 BENCHMARK.md 第 5.4 节）
  python sample_size.py --pre "runs_pre/results_std_r*.jsonl"   # 预跑后用真实的每题答对率重算
假设两臂真实正确率相同（Δ=0），这是“插件不掉分”时我们能否把它证明出来的概率。
"""
import argparse, json, random, statistics
from collections import defaultdict


def prior(n, rng):
    """设计阶段假设：35% 稳定会做(p≈0.9-1)，40% 时对时错(p 0.3-0.9)，25% 偏难(p 0.05-0.3)。"""
    ps = []
    for _ in range(n):
        u = rng.random()
        ps.append(rng.uniform(.9, 1) if u < .35 else rng.uniform(.3, .9) if u < .75 else rng.uniform(.05, .3))
    return ps


def power(ps, r, margin, sims=2000, delta=0.0, seed=1):
    rng = random.Random(seed); n = len(ps); hit = 0
    for _ in range(sims):
        d = []
        for p in ps:
            q = min(1, max(0, p + delta))
            a = sum(rng.random() < q for _ in range(r)) / r; b = sum(rng.random() < p for _ in range(r)) / r
            d.append(a - b)
        m = statistics.fmean(d); se = statistics.pstdev(d) / n ** .5
        hit += (m - 1.96 * se) > -margin
    return hit / sims


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--pre"); ap.add_argument("--sims", type=int, default=1000)
    a = ap.parse_args(); rng = random.Random(20261103)
    if a.pre:
        d = defaultdict(list)
        import glob
        for fp in glob.glob(a.pre):
            for l in open(fp, encoding="utf-8"):
                x = json.loads(l); d[x["task_id"]].append(x["pass"])
        base = [min(.97, max(.03, sum(v) / len(v))) for v in d.values()]  # 2 次预跑太少，夹到 [0.03,0.97]
        if len(base) < 30:
            print(f"! 只有 {len(base)} 道题有预跑结果：样本太小，功效估计很不稳（正式估计请在 calibrate 之后用完整题单重跑）。")
    else:
        base = prior(100, rng)
    print(f"题数 {len(base)}，平均答对率 {statistics.fmean(base):.2f}")
    print("margin(分)  r=1    r=2    r=3    r=5    （真实 Δ=0 时证明非劣效的把握）")
    for m in (2, 3, 5, 8):
        row = [power(base, r, m / 100, a.sims) for r in (1, 2, 3, 5)]
        print(f"   {m:>2}      " + "  ".join(f"{x:.2f}" for x in row))
    print("\n若真实恰好掉 5 分（Δ=−5，正好在界限上），r=3 时被误判为“非劣效”的概率（应≈2.5%）：", power(base, 3, .05, a.sims, delta=-.05))
    print("若真实掉 3 分（Δ=−3），r=3、margin 5 时能证明非劣效的把握：", power(base, 3, .05, a.sims, delta=-.03))
