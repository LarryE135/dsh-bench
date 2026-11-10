#!/usr/bin/env python3
"""多维能力评分卡：把各臂的逐题结果算成 8 个能力轴的分数，画成雷达图 + “相对标准”的分差图，并输出表格。

用法：
    python scorecard.py --runs runs --base std --out scorecard            # 生成 scorecard.png / .md / .json
    python scorecard.py --runs runs --weighting difficulty --pre runs_pre # 难度加权版（需要预跑结果）
    python scorecard.py --demo --out demo                                  # 用模拟数据看图长什么样
依赖：matplotlib（画图）；其余只用标准库。中文字体自动找 微软雅黑/黑体/苹方/Noto CJK，也可 --font 指定字体文件。

算法（通俗版见 BENCHMARK.md 第 10 节）：
  1. 题目得分 s(i,臂) = 该题 r 次重复里答对的比例（0~1）。
  2. 轴分 A(k,臂) = 轴 k 内各题得分的加权平均 ×100。权重默认都为 1；
     难度加权时 w_i = 0.5 + (1 − p_i)，p_i 是标准臂预跑时该题答对率——简单题权重 0.5（仍计分，作防退化底座），最难题 1.5。
  3. 轴分区间：在轴内对“题”做 bootstrap（2000 次），取 2.5%/97.5% 分位。
  4. 维度分 = 该维度下各轴分的等权平均；综合分 = 4 个维度分的等权平均（各 25%，不按题数加权，避免题多的维度说了算）。
  5. 相对标准：Δ轴分 = A(k,臂) − A(k,标准)，用“配对” bootstrap（同一批题同时重抽），画出 95% 区间和 −5 分非劣效线。
  6. 性价比指数 = (综合分_臂 / 综合分_标准) ÷ (平均每题费用_臂 / 平均每题费用_标准)；>1 表示同样的钱买到更多正确率。
"""
import argparse, json, os, random
from collections import defaultdict

AXES = [  # (轴名, 所属维度, 判定函数：根据 task_id 前缀)
    ("算法编程", "编程", "code-lcb-"),
    ("工程编程", "编程", "code-poly-"),
    ("表格处理", "日常办公", "office-ssb-"),
    ("文件办公", "日常办公", "office-self-"),
    ("知识问答", "知识", "know-"),
    ("长文阅读", "长任务注意力", "long-lb2-"),
    ("仓库检索", "长任务注意力", "long-needle-"),
    ("长链路执行", "长任务注意力", "long-chain-"),
]
DIMS = ["编程", "日常办公", "知识", "长任务注意力"]
SEED = 20261103


def axis_of(tid):
    for name, dim, pre in AXES:
        if tid.startswith(pre):
            return name
    return None


def item_scores(recs):
    d = defaultdict(list)
    for r in recs:
        d[r["task_id"]].append(1.0 if r["pass"] else 0.0)
    return {t: sum(v) / len(v) for t, v in d.items()}


def wmean(items, s, w):
    den = sum(w.get(i, 1.0) for i in items)
    return 100 * sum(s[i] * w.get(i, 1.0) for i in items) / den if den else float("nan")


def compute(scores, weights, base, n_boot=2000):
    """scores: arm -> {task: s}；返回轴分/区间/Δ/维度分/综合分。"""
    rng = random.Random(SEED)
    if not scores:
        print("  评分卡跳过：还没有任何判分结果")
        return {"axes": {}, "dims": {}, "overall": {}, "delta": {}, "n_items": {}}
    common = set.intersection(*(set(v) for v in scores.values())) if len(scores) > 0 else set()  # v1.7.2：空输入不再 TypeError
    if not common:
        print("  评分卡跳过：没有任何一道题是各臂都跑过的（先跑齐 stage1）")
        return {"axes": {}, "dims": {}, "overall": {}, "delta": {}, "n_items": {}}
    by_axis = defaultdict(list)
    for t in sorted(common):
        a = axis_of(t)
        if a:
            by_axis[a].append(t)
    out = {"axes": {}, "dims": {}, "overall": {}, "delta": {}, "n_items": {a: len(v) for a, v in by_axis.items()}}
    boots = {arm: defaultdict(list) for arm in scores}
    for a, items in by_axis.items():
        for arm in scores:
            out["axes"].setdefault(arm, {})[a] = wmean(items, scores[arm], weights)
        for _ in range(n_boot):  # 同一组重抽题号同时用于所有臂 → 配对
            samp = [items[rng.randrange(len(items))] for _ in items]
            for arm in scores:
                boots[arm][a].append(wmean(samp, scores[arm], weights))
    q = lambda v: (sorted(v)[int(.025 * len(v))], sorted(v)[int(.975 * len(v)) - 1])
    out["ci"] = {arm: {a: q(v) for a, v in boots[arm].items()} for arm in scores}
    for arm in scores:
        dims = {}
        for dname in DIMS:
            vals = [out["axes"][arm][a] for a, dd, _ in AXES if dd == dname and a in out["axes"][arm]]
            if vals:
                dims[dname] = sum(vals) / len(vals)
        out["dims"][arm] = dims
        out["overall"][arm] = sum(dims.values()) / len(dims) if dims else float("nan")
        if base in scores and arm != base:
            out["delta"][arm] = {a: (out["axes"][arm][a] - out["axes"][base][a],
                                     q([x - y for x, y in zip(boots[arm][a], boots[base][a])])) for a in by_axis}
    return out


def find_font(path=None):
    from matplotlib import font_manager as fm
    if path:
        fm.fontManager.addfont(path); return fm.FontProperties(fname=path).get_name()
    for name in ("Microsoft YaHei", "SimHei", "PingFang SC", "Hiragino Sans GB", "Noto Sans CJK SC", "Noto Sans CJK JP", "Source Han Sans SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Arial Unicode MS"):
        if any(f.name == name for f in fm.fontManager.ttflist):
            return name
    return None


def plot(res, base, out_png, font=None, title="DSH-Bench 多维能力评分卡", cost=None):
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fam = find_font(font)
    if fam:
        plt.rcParams["font.family"] = fam
    else:
        print("! 没找到中文字体，图里的中文会显示成方框。用 --font 指定一个 .ttf/.otf/.ttc 字体文件即可。")
    plt.rcParams["axes.unicode_minus"] = False
    arms = list(res["axes"]); axes = [a for a, _, _ in AXES if a in res["axes"][arms[0]]]
    # 每个臂一种颜色；未登记的臂名自动从下面这组色板里取，改臂不用改代码
    colors = {"std": "#7f7f7f", "ptc": "#ff7f0e"}
    palette = ["#1f77b4", "#d62728", "#2ca02c", "#8c564b", "#9467bd", "#17becf", "#e377c2", "#bcbd22"]
    for i, a in enumerate(arms):
        colors.setdefault(a, palette[i % len(palette)])
    fig = plt.figure(figsize=(14, 6.6), dpi=150)
    ax = fig.add_subplot(1, 2, 1, polar=True)
    ax.set_theta_offset(math.pi / 2); ax.set_theta_direction(-1)  # 第一个轴在正上方，顺时针排列
    ang = [2 * math.pi * i / len(axes) for i in range(len(axes))] + [0]
    for arm in arms:
        v = [res["axes"][arm][a] for a in axes]; v += v[:1]
        c = colors.get(arm); ls = "--" if arm == base else "-"
        ax.plot(ang, v, ls, color=c, lw=2, label=f"{arm}  综合 {res['overall'][arm]:.1f}")
        ax.fill(ang, v, color=c, alpha=0.06 if arm == base else 0.10)
    ax.set_xticks(ang[:-1]); ax.set_xticklabels([f"{a}\n(n={res['n_items'][a]})" for a in axes], fontsize=10)
    ax.set_ylim(0, 100); ax.set_yticks([20, 40, 60, 80, 100]); ax.set_yticklabels(["20", "40", "60", "80", "100"], fontsize=7, color="#888")
    ax.set_rlabel_position(360 / len(axes) / 2); ax.tick_params(axis="x", pad=14)
    ax.set_title("各能力轴得分（0–100）", pad=22, fontsize=12)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=2, fontsize=9, frameon=False)
    bx = fig.add_subplot(1, 2, 2)
    # v1.7.6：只有单臂数据时 res["delta"] 里没有该臂，原来会 KeyError 把整张评分卡画崩
    others = [a for a in arms if a != base and a in res["delta"]]; h = 0.8 / max(len(others), 1)
    for j, arm in enumerate(others):
        ys = [i + (j - (len(others) - 1) / 2) * h for i in range(len(axes))]
        d = [res["delta"][arm][a][0] for a in axes]
        lo = [res["delta"][arm][a][0] - res["delta"][arm][a][1][0] for a in axes]
        hi = [res["delta"][arm][a][1][1] - res["delta"][arm][a][0] for a in axes]
        bx.errorbar(d, ys, xerr=[lo, hi], fmt="o", color=colors.get(arm), capsize=3, ms=5, lw=1.4, label=arm)
    bx.axvline(0, color="#444", lw=1); bx.axvline(-5, color="#d62728", lw=1, ls=":", label="非劣效界限 −5")
    bx.set_yticks(range(len(axes))); bx.set_yticklabels(axes); bx.set_ylim(len(axes) - 0.5, -0.5)
    bx.set_xlabel(f"相对「{base}」的分差（分，点 = 估计值，横线 = 95% 区间）")
    bx.set_title("各轴相对标准的变化", fontsize=12); bx.grid(axis="x", alpha=.3)
    bx.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=4, fontsize=9, frameon=False)
    if cost:
        try:
            import analyze as _an
            _cur = _an.CUR
        except Exception:
            _cur = ""
        txt = "   ".join(f"{a}: 费用×{cost[a]['cost_ratio']:.2f}  性价比 {cost[a]['value_index']:.2f}" for a in others if a in cost) + "　（单位 %s；费用比只算两臂都记到账的题，漏记不计）" % _cur
        fig.text(0.5, 0.015, txt, ha="center", fontsize=9, color="#333")
    fig.suptitle(title, fontsize=14, y=0.99)
    fig.tight_layout(rect=(0, 0.04, 1, 0.96)); fig.savefig(out_png); plt.close(fig)


def table_md(res, base, cost=None):
    arms = list(res["axes"]); L = ["| 能力轴 | 题数 | " + " | ".join(f"{a} 分 [95%CI]" for a in arms) + " |", "|---|---|" + "---|" * len(arms)]
    for a, _, _ in AXES:
        if a not in res["axes"][arms[0]]:
            continue
        L.append(f"| {a} | {res['n_items'][a]} | " + " | ".join(
            f"{res['axes'][arm][a]:.1f} [{res['ci'][arm][a][0]:.0f}, {res['ci'][arm][a][1]:.0f}]" for arm in arms) + " |")
    for d in DIMS:
        if all(d not in res["dims"][arm] for arm in arms):  # 该维度这一批没有题：整行不写，避免满屏 nan
            continue
        L.append(f"| **维度：{d}** | | " + " | ".join(f"{res['dims'][arm].get(d, float('nan')):.1f}" for arm in arms) + " |")
    L.append("| **综合分** | | " + " | ".join(f"**{res['overall'][arm]:.1f}**" for arm in arms) + " |")
    if cost:
        L.append("| 费用比（÷标准） | | " + " | ".join(f"{cost[a]['cost_ratio']:.2f}" if a in cost else "1.00" for a in arms) + " |")
        L.append("| 性价比指数 | | " + " | ".join(f"{cost[a]['value_index']:.2f}" if a in cost else "1.00" for a in arms) + " |")
    return "\n".join(L) + "\n"


def demo_data():
    """模拟数据，仅用于展示图的样子：不代表任何真实结果。"""
    rng = random.Random(7); tasks = []
    for _, _, pre in AXES:
        n = {"code-lcb-": 15, "code-poly-": 15, "office-ssb-": 15, "office-self-": 10, "know-": 14, "long-lb2-": 8, "long-needle-": 9, "long-chain-": 8}[pre]
        tasks += [f"{pre}{i}" for i in range(n)]
    p = {t: rng.uniform(.15, .98) for t in tasks}
    shift = {"ptc": {"long-chain-": -.06, "long-needle-": -.03}}
    recs = defaultdict(list)
    for arm in ("std", "ptc"):
        for t in tasks:
            q = p[t] + next((v for k, v in shift.get(arm, {}).items() if t.startswith(k)), 0)
            for _ in range(3):
                recs[arm].append({"task_id": t, "pass": rng.random() < q})
    cost = {"ptc": .90}
    return recs, cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs"); ap.add_argument("--base", default="std"); ap.add_argument("--out", default="scorecard")
    ap.add_argument("--weighting", choices=["none", "difficulty"], default="none"); ap.add_argument("--pre", default=None)
    ap.add_argument("--font", default=None); ap.add_argument("--demo", action="store_true")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"))
    a = ap.parse_args()
    cost_ratio = {}
    if a.demo:
        recs, cost_ratio = demo_data()
    else:
        import analyze, glob
        # 单价只该有一份：原来这里写 0.006/0.3/1.2（比例恰好相同，费用比不受影响，但绝对金额
        # 比 analyze 小 6.7 倍且没标单位）—— 审计 P3-2
        price = dict(analyze.PRICE_DEFAULT)
        if os.path.exists(a.config):
            _c = json.load(open(a.config, encoding="utf-8"))
            _t = _c.get("pricing_cny_per_mtok")
            if not _t and _c.get("pricing_usd_per_mtok"):  # 兼容旧配置
                _fx = float(_c.get("fx_usd_cny") or 7.0)
                _t = {k: (v * _fx if isinstance(v, (int, float)) else v) for k, v in _c["pricing_usd_per_mtok"].items()}
            price = {k: v for k, v in (_t or {}).items() if not k.startswith("_")} or price
        loaded = analyze.load(a.runs, price)
        recs = {arm: list(v.values()) for arm, v in loaded.items()}
        _bs = [r for r in recs.get(a.base, []) if not r.get("usage_missing")]  # 漏记不能按 0 元进基准
        bc = sum(r["usage"].get("cost", 0) for r in _bs) / max(len(_bs), 1)
        for arm, rs in recs.items():
            if arm != a.base and bc > 0:
                # 代理没记到账的记录不能按 0 元参与（会把费用比拉向 1）
                paid = [r for r in rs if not r.get("usage_missing")]
                cost_ratio[arm] = (sum(r["usage"].get("cost", 0) for r in paid) / max(len(paid), 1) / bc) if paid else None
    weights = {}
    if a.weighting == "difficulty":
        import glob
        pre = defaultdict(list)
        for fp in glob.glob(os.path.join(a.pre, f"results_{a.base}_r*.jsonl")):
            for l in open(fp, encoding="utf-8"):
                r = json.loads(l); pre[r["task_id"]].append(r["pass"])
        weights = {t: 0.5 + (1 - sum(v) / len(v)) for t, v in pre.items()}
    rank = ["std", "ptc"]
    order = sorted(recs, key=lambda x: (x != a.base, rank.index(x) if x in rank else 9, x))
    scores = {arm: item_scores(recs[arm]) for arm in order}  # 标准臂放第一列
    res = compute(scores, weights, a.base)
    cost = {arm: {"cost_ratio": cr, "value_index": (res["overall"][arm] / res["overall"][a.base]) / cr}
            for arm, cr in cost_ratio.items() if cr > 0} if a.base in res["overall"] else None
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    title = "DSH-Bench 多维能力评分卡" + ("（模拟数据示例，非真实结果）" if a.demo else "")
    try:  # matplotlib 是可选依赖：没装也要出 md/json，不能让报告整份失败
        plot(res, a.base, a.out + ".png", a.font, title, cost)
    except ImportError as ex:
        print(f"! 没装 matplotlib，跳过图片（表格照样输出）：{ex}")
    md = table_md(res, a.base, cost)
    open(a.out + ".md", "w", encoding="utf-8").write(f"# {title}\n\n权重：{a.weighting}\n\n" + md)
    json.dump({"result": res, "cost": cost, "weighting": a.weighting}, open(a.out + ".json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=list)
    print(md)


if __name__ == "__main__":
    main()