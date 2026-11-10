#!/usr/bin/env python3
"""汇总与统计检验：把各臂的判分结果和记账代理日志合起来，输出对比报告（Markdown）。

用法：python analyze.py --runs runs --base std --margin 5
只用标准库。核心思路（通俗版见 BENCHMARK.md 第 5 节）：
  * 配对：同一道题在两个臂上比，题目难易的差异被抵消，只剩“插件带来的差别”。
  * 正确率差 Δ：先把每题的多次重复取平均，再对“题”做 10000 次 bootstrap 重抽样，得到 95% 置信区间。
  * 非劣效：如果 Δ 的 95% 区间下限 > −margin（默认 −5 分），就判“正确率不比标准差”。
  * McNemar 精确检验：只看“一边对一边错”的配对（同题同第几次），检验两边是否有系统性差别。
  * 省钱：token/费用比值（臂 ÷ 标准）同样用按题 bootstrap 给区间。
"""
import argparse, glob, json, math, os, random
from collections import defaultdict

PRICE_DEFAULT = {"cache_hit": 0.04, "cache_miss": 2.0, "output": 8.0}  # v1.7.4：元/百万 tokens（deepseek-flash 高峰价）
CUR = "¥"


def load_price(path):
    """读 config 里的价表；兼容旧的 pricing_usd_per_mtok（换算成人民币）。"""
    global CUR
    p = dict(PRICE_DEFAULT)
    if path and os.path.exists(path):
        c = json.load(open(path, encoding="utf-8"))
        CUR = "¥" if (c.get("currency") or "CNY").upper() == "CNY" else "$"
        tbl = c.get("pricing_cny_per_mtok")
        if not tbl and c.get("pricing_usd_per_mtok"):
            fx = float(c.get("fx_usd_cny") or 7.0)
            tbl = {k: (v * fx if isinstance(v, (int, float)) else v) for k, v in c["pricing_usd_per_mtok"].items()}
        if tbl:
            p = {k: v for k, v in tbl.items() if not k.startswith("_")}
    return p


BLOCKED = {}
_SEEN = {}  # v1.7.6：去重用（arm, task, rep）→ 最新记录


def load(runs, price):
    res = defaultdict(dict)  # arm -> (task, rep) -> rec
    for p in glob.glob(os.path.join(runs, "results_*.jsonl")):
        for l in open(p, encoding="utf-8"):
            r = json.loads(l)
            # leak_suspect：运行中读到过答案/题号的痕迹。README 承诺「出现即判失败」，
            # 但原来只标记不剔除，读到答案的样本照样进配对（通常还只污染一侧 → 系统性偏差）。
            if r.get("status") in ("leak_blocked", "dry") or r.get("graded") is False or r.get("leak_suspect"):
                # 启动前自检拦下 / dry-run / 没判分的：没有有效答卷，不计入正确率比较（健康度表里另计）
                BLOCKED[r["arm"]] = BLOCKED.get(r["arm"], 0) + 1; continue
            key = (r["arm"], r["task_id"], r["rep"])
            # v1.7.6：同一格出现多条记录时（续跑、文件被复制过）取时间戳最新的一条，别让 glob 顺序决定结果
            if key in _SEEN and str(r.get("ts", "")) < str(_SEEN[key].get("ts", "")):
                continue
            _SEEN[key] = r
            res[r["arm"]][(r["task_id"], r["rep"])] = r
    use = defaultdict(lambda: defaultdict(float))  # tag -> metric
    for p in glob.glob(os.path.join(runs, "usage_*.jsonl")):
        for l in open(p, encoding="utf-8"):
            u = json.loads(l)
            if not u.get("tag"):
                continue
            m = use[u["tag"]]
            hit = u.get("prompt_cache_hit_tokens", u.get("cache_read_input_tokens", 0)) or 0
            miss = u.get("prompt_cache_miss_tokens")
            if miss is None:
                miss = (u.get("prompt_tokens") or u.get("input_tokens") or 0) - (hit if "prompt_tokens" in u else 0) \
                    + (u.get("cache_creation_input_tokens") or 0)  # Anthropic 口径：写缓存的部分单列，按未命中计
            out = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
            m["requests"] += 1; m["hit"] += hit; m["miss"] += miss; m["out"] += out
            m["reasoning"] += u.get("reasoning_tokens", 0) or 0; m["tool_calls"] += u.get("tool_calls", 0) or 0
            m["cost"] += (hit * price["cache_hit"] + miss * price["cache_miss"] + out * price["output"]) / 1e6
            m["cost_nocache"] += ((hit + miss) * price["cache_miss"] + out * price["output"]) / 1e6  # 敏感性：假设缓存全没命中
    for arm in res:
        for k, r in res[arm].items():
            u = use.get(r["tag"])
            # 代理没记到 ≠ 花了 0 元：留显式标记，统计时把这些记录从费用比里剔除
            r["usage"] = dict(u) if u else {}
            r["usage_missing"] = u is None
    return res


def per_item(recs, key):
    d = defaultdict(list)
    for (tid, rep), r in recs.items():
        d[tid].append(key(r))
    return {t: sum(v) / len(v) for t, v in d.items()}


CONF = 0.95


def bootstrap(items, stat, n=10000, seed=20261103):
    rng = random.Random(seed); vals = []
    for _ in range(n):
        s = [items[rng.randrange(len(items))] for _ in items]
        try:
            vals.append(stat(s))
        except ZeroDivisionError:
            pass
    if not vals:
        return float('nan'), float('nan')  # 例如没有 token 记录（代理没开）
    q = (1 - CONF) / 2
    vals.sort(); return vals[int(q * len(vals))], vals[int((1 - q) * len(vals)) - 1]


def mcnemar_exact(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c); p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def summarize(recs):
    n = len(recs); acc = sum(r["pass"] for r in recs.values()) / max(n, 1)
    tot = defaultdict(float)
    for r in recs.values():
        for k, v in r["usage"].items():
            tot[k] += v
        tot["wall"] += r["wall_s"]
    return n, acc, tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="runs"); ap.add_argument("--base", default="std")
    ap.add_argument("--margin", type=float, default=5.0, help="非劣效界限（百分点）")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--conf", type=float, default=0.95, help="置信水平；期中检查用 0.99")
    ap.add_argument("--platform", default=None, help="v1.8：只统计某个平台的运行（windows / wsl / linux）")
    ap.add_argument("--batch", default=None, help="v1.8：只统计某一批题（base = 原题，hard2 = 新加难题）")
    a = ap.parse_args()
    global CONF
    CONF = a.conf
    price = load_price(a.config)
    res = load(a.runs, price); L = []
    # v1.8：按平台 / 题批过滤。没有 host 字段的旧记录记为 unknown；没有 batch 字段的题记为 base
    batch_of = {}
    mf = os.path.join(os.path.dirname(os.path.abspath(a.runs)), "manifest", "manifest.jsonl")
    if os.path.exists(mf):
        for l in open(mf, encoding="utf-8"):
            t = json.loads(l); batch_of[t["task_id"]] = t.get("batch") or "base"
    for arm in list(res):
        res[arm] = {k: r for k, r in res[arm].items()
                    if (not a.platform or (r.get("host") or "unknown") == a.platform)
                    and (not a.batch or batch_of.get(k[0], "base") == a.batch)}
        if not res[arm]:
            del res[arm]
    hosts = {arm: sorted({r.get("host") or "unknown" for r in rs.values()}) for arm, rs in res.items()}
    L.append(f"# 对比报告（置信水平 {CONF:.0%}）\n")
    setup = os.path.join(os.path.dirname(os.path.abspath(a.runs)), "runs", "_setup", "setup.json")
    if os.path.exists(setup):
        L.append(f"DSH 版本：{json.load(open(setup, encoding='utf-8')).get('dsh_version', '?')}\n")
    if os.path.exists(a.config):  # v1.7.3：报告头写清“哪条路、哪个模型”，免得把 wb 路由的数据当官方 1017 看
        _c = json.load(open(a.config, encoding="utf-8"))
        _route = _c.get("llm_provider") or "deepseek"
        L.append("路线：" + _route + "（upstream " + str(_c.get("upstream")) + "）；模型 " + str(_c.get("model")) +
                 "；期望版本 " + str(_c.get("model_version_expected")) + "\n")
        if _route == "wb":
            L.append("> 注意：wb 路由下 model 与 model_version_expected 只能是网关的模型 id，版本漂移检测弱于官方路线；"
                     "费用列按 DeepSeek 官方单价从 token 重算，各臂之间可比，绝对金额不是账单。\n")
    L.append("平台：" + "；".join(f"{arm} {'/'.join(h)}" for arm, h in sorted(hosts.items()))
             + ("" if not a.platform else f"（已过滤：只看 {a.platform}）") + ("" if not a.batch else f"；题批：{a.batch}") + "\n")
    if len({tuple(h) for h in hosts.values()}) > 1 or any(len(h) > 1 for h in hosts.values()):
        L.append("> ⚠ 各臂的运行平台不一致或混了多个平台。DSH 桌面端（Windows）与 WSL 在编程题上的得分本身就有差距，"
                 "混在一起比会把平台差异算成插件差异。请用 --platform 分开出报告。\n")
    L.append("## 运行健康度\n")
    L.append("| 臂 | 正常结束 | 超时 | 非零退出 | 判分器报错 | 疑似偷看答案 | 代理没记到 token | 自检拦下未跑 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for arm in sorted(res):
        rs = list(res[arm].values())
        L.append(f"| {arm} | {sum(r['status'] == 'ok' for r in rs)} | {sum(r['status'] == 'timeout' for r in rs)} | "
                 f"{sum(str(r['status']).startswith('exit') for r in rs)} | {sum('grader_error' in r['detail'] for r in rs)} | "
                 f"{sum(r.get('leak_suspect', False) for r in rs)} | {sum(not r['usage'].get('requests') for r in rs)} | {BLOCKED.get(arm, 0)} |")
    L.append("\n## 各臂总览\n")
    L.append("| 臂 | 次数 | 正确率 | 缓存命中 tok | 未命中 tok | 输出 tok | 估算费用 " + CUR + " | 总耗时 h | 工具调用 | API 请求 | 每答对一题费用 " + CUR + " | 输出占费用 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    # P1-16：选择题有猜测地板（4 选项 25%、10 选项 10%）。26/110 主测是 mcq，地板 ≈4.7pp，与非劣界 5pp 同量级，
    # 所以报告里同时给「扣掉地板后的净正确率」，让读者知道多少分来自猜。
    FLOOR = {}
    try:
        import re as _re
        _root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tasks")
        for _tid in os.listdir(_root):
            _tj = os.path.join(_root, _tid, "task.json")
            if not os.path.isfile(_tj):
                continue
            _t = json.load(open(_tj, encoding="utf-8"))
            if _t.get("grader") == "mcq":
                _n = len(_re.findall(r"^[A-J][.] ", _t.get("prompt") or "", flags=_re.M)) or 10
                FLOOR[_tid] = 1.0 / max(_n, 2)
    except Exception:
        FLOOR = {}
    # R5：缺题必须显示出来：交集剔掉的往往正是「该臂跑不动」的题，静默剔掉会系统性高估该臂
    SENS, MISS = [], {}
    for arm in sorted(res):
        n, acc, t = summarize(res[arm]); correct = sum(r["pass"] for r in res[arm].values())
        L.append(f"| {arm} | {n} | {acc*100:.1f}% | {t['hit']/1e6:.2f}M | {t['miss']/1e6:.2f}M | {t['out']/1e6:.2f}M | "
                 f"{t['cost']:.2f} | {t['wall']/3600:.2f} | {int(t['tool_calls'])} | {int(t['requests'])} | {t['cost']/max(correct,1):.4f} | "
                 f"{(t['out'] * price.get('output', 8) / 1e6) / max(t['cost'], 1e-9) * 100:.0f}% |")
    if FLOOR:
        L.append("\n> **扣猜测地板后的净正确率**（只对选择题按选项数扣：4 选项扣 25%，10 选项扣 10%；其它题型不变）\n")
        L.append("| 臂 | 原始正确率 | 平均地板 | 净正确率 |")
        L.append("|---|---|---|---|")
        for _arm in sorted(res):
            _rs = res[_arm]
            _n, _acc, _t = summarize(_rs)
            _f = sum(FLOOR.get(_r.get("task_id"), 0.0) for _r in _rs.values()) / max(len(_rs), 1)
            _net = (_acc - _f) / max(1 - _f, 1e-9)
            L.append(f"| {_arm} | {_acc*100:.1f}% | {_f*100:.1f}% | {_net*100:.1f}% |")
    base = res.get(a.base)
    if base:
        L.append(f"\n## 与「{a.base}」的配对比较（非劣效界限 −{a.margin:g} 分）\n")
        L.append(f"| 臂 | 配对题数 | Δ正确率(分) | {CONF:.0%} CI | 非劣效? | McNemar b/c, p | 总 token 比 [CI] | 费用比 [CI] | 费用比·不计缓存 | 耗时比 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for arm in sorted(res):
            if arm == a.base:
                continue
            A, B = res[arm], base
            _na, _nb = len({t for t, _ in A}), len({t for t, _ in B})
            MISS[arm] = (len({t for t, _ in A} & {t for t, _ in B}), _na - len({t for t, _ in A} & {t for t, _ in B}), _nb - len({t for t, _ in A} & {t for t, _ in B}))
            common = sorted({t for t, _ in A} & {t for t, _ in B})
            pa = per_item({k: v for k, v in A.items() if k[0] in common}, lambda r: r["pass"])
            pb = per_item({k: v for k, v in B.items() if k[0] in common}, lambda r: r["pass"])
            # 费用只在「两臂都记到了账」的题上比：漏记按 0 元入账会把费用比拉向 1（P0-3）
            Aok = {k: v for k, v in A.items() if k[0] in common and not v.get("usage_missing")}
            Bok = {k: v for k, v in B.items() if k[0] in common and not v.get("usage_missing")}
            ccom = sorted({t for t, _ in Aok} & {t for t, _ in Bok})
            ca = per_item({k: v for k, v in Aok.items() if k[0] in ccom}, lambda r: r["usage"].get("cost", 0))
            cb = per_item({k: v for k, v in Bok.items() if k[0] in ccom}, lambda r: r["usage"].get("cost", 0))
            miss_cost = len(common) - len(ccom)
            tok = lambda r: sum(r["usage"].get(k, 0) for k in ("hit", "miss", "out"))
            ta = per_item({k: v for k, v in A.items() if k[0] in common}, tok)
            tb = per_item({k: v for k, v in B.items() if k[0] in common}, tok)
            wa = per_item({k: v for k, v in A.items() if k[0] in common}, lambda r: r["wall_s"])
            wb = per_item({k: v for k, v in B.items() if k[0] in common}, lambda r: r["wall_s"])
            items = [(pa[t], pb[t], ca.get(t, 0.0), cb.get(t, 0.0), ta[t], tb[t]) for t in common]
            if not items:
                continue
            d = sum(x[0] - x[1] for x in items) / len(items) * 100
            lo, hi = bootstrap(items, lambda s: sum(x[0] - x[1] for x in s) / len(s) * 100)
            # R4：超时/非零退出按答错进分母，而「工具面更慢」更容易撞超时 —— 单出一行「仅正常结束」做敏感性对照
            _okk = [k for k in A if k in B and k[0] in common
                    and str(A[k].get("status")) == "ok" and str(B[k].get("status")) == "ok"]
            if _okk:
                _it = [(1.0 if A[k]["pass"] else 0.0, 1.0 if B[k]["pass"] else 0.0) for k in _okk]
                _d2 = sum(q[0] - q[1] for q in _it) / len(_it) * 100
                _l2, _h2 = bootstrap(_it, lambda s: sum(q[0] - q[1] for q in s) / len(s) * 100)
                # 单位对齐：items 是「题」，_it 是「题×遍」；被排除的题数按题去重
                SENS.append((arm, len(_it), _d2, _l2, _h2, len(common) - len({k[0] for k in _okk})))
            b = sum(1 for k in A if k in B and A[k]["pass"] and not B[k]["pass"])
            c = sum(1 for k in A if k in B and not A[k]["pass"] and B[k]["pass"])
            tr = sum(x[4] for x in items) / max(sum(x[5] for x in items), 1e-9)
            tlo, thi = bootstrap(items, lambda s: sum(x[4] for x in s) / sum(x[5] for x in s))
            cr = sum(x[2] for x in items) / max(sum(x[3] for x in items), 1e-9)
            clo, chi = bootstrap(items, lambda s: sum(x[2] for x in s) / sum(x[3] for x in s))
            wr = sum(wa.values()) / max(sum(wb.values()), 1e-9)
            na = per_item({k: v for k, v in A.items() if k[0] in common}, lambda r: r["usage"].get("cost_nocache", 0))
            nb = per_item({k: v for k, v in B.items() if k[0] in common}, lambda r: r["usage"].get("cost_nocache", 0))
            nr = sum(na.values()) / max(sum(nb.values()), 1e-9)  # v1.5 敏感性：缓存命中率可能受“谁先跑同一前缀”影响
            L.append(f"| {arm} | {len(items)} | {d:+.1f} | [{lo:+.1f}, {hi:+.1f}] | {'是' if lo > -a.margin else '未能证明'} | "
                     f"{b}/{c}, {mcnemar_exact(b, c):.3f} | {tr:.2f} [{tlo:.2f}, {thi:.2f}] | {cr:.2f} [{clo:.2f}, {chi:.2f}] | {nr:.2f} | {wr:.2f} |")
        # 读法提示（P3-4）与功效（P2-5）：区间没做多重比较校正；功效按实际题数与遍数现算。
        L.append(f"\n> **读法提示**：每条轴各自一个 {CONF:.0%} 区间，未做多重比较校正（多条轴同看时族错误率明显高于 5%）；判据以「综合分」为主，其余轴当描述。\n")
        try:
            import sample_size as _ss
            _ps = [pb[t] for t in common]
            _reps = max([len({k[1] for k in A if k[0] == t}) or 1 for t in common] or [1])
            _pw = _ss.power(_ps, _reps, a.margin / 100.0, sims=300)
            L.append(f"> 功效：按当前 {len(common)} 道配对题、r={_reps}、margin={a.margin:g} 分，真实 Δ=0 时「证明非劣效」的把握约 **{_pw:.2f}**。把握不足 0.8 时，「未能证明非劣效」不能读成「掉分了」（见审计 R9）。\n")
        except Exception as _ex:
            L.append(f"> 功效：估算不可用（{_ex!r}）\n")
        if SENS:
            L.append("\n### 敏感性：只算「两臂都正常结束」的题\n")
            L.append("| 臂 | 正常结束题数 | Δ正确率(分) | %d%% CI | 被排除 |" % round(CONF * 100))
            L.append("|---|---|---|---|---|")
            for _arm, _n, _d, _l, _h, _drop in SENS:
                L.append("| %s | %d | %+.1f | [%+.1f, %+.1f] | %d |" % (_arm, _n, _d, _l, _h, _drop))
        if MISS:
            L.append("\n### 缺题对账（交集会把这部分题从两臂比较里剔掉）\n")
            L.append("| 臂 | 两臂共同题 | 本臂独缺 | 基准臂独缺 |")
            L.append("|---|---|---|---|")
            for _arm, _t in sorted(MISS.items()):
                L.append("| %s | %d | %d | %d |" % (_arm, _t[0], _t[1], _t[2]))
        L.append("\n## 分维度（仅描述，题数少不做检验）\n")
        dims = sorted({r["dimension"] for v in res.values() for r in v.values()})
        L.append("| 维度 | " + " | ".join(f"{arm} 正确率 / 费用{CUR}" for arm in sorted(res)) + " |")
        L.append("|---|" + "---|" * len(res))
        for dim in dims:
            cells = []
            for arm in sorted(res):
                rs = [r for r in res[arm].values() if r["dimension"] == dim]
                cells.append(f"{sum(r['pass'] for r in rs)/max(len(rs),1)*100:.0f}% / {sum(r['usage'].get('cost',0) for r in rs):.2f}" if rs else "-")
            L.append(f"| {dim} | " + " | ".join(cells) + " |")
    txt = "\n".join(L) + "\n"
    print(txt)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(txt)


if __name__ == "__main__":
    main()