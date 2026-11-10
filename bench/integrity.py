#!/usr/bin/env python3
"""runs/ 的修复与覆盖统计（调试工具，不是判分的一部分）。

用途：
  1. 每次运行是一行 JSON（jsonl）。进程被杀时最后一行可能写坏 —— 本工具把坏行摘出来
     备份成 *.bad，让文件回到「每行都是合法 JSON」的状态（否则后续统计会整段跳过）。
  2. 打印各臂各遍已完成题数、以及 status 分布，用来判断「是不是跑满了」。

用法：
  python bench/integrity.py --runs runs            # 只看统计
  python bench/integrity.py --runs runs --fix       # 真改文件（先备份 *.bad）
  python bench/integrity.py --runs runs_pre --want 110
"""
import argparse
import collections
import glob
import json
import os
import sys


def broken_lines(path):
    """返回 (保留的行, 坏行字节)。坏行 = 不是合法 JSON 的行。"""
    raw = open(path, "rb").read()
    if not raw:
        return [], b""
    keep, bad = [], b""
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            json.loads(line.decode("utf-8", "replace"))
            keep.append(line)
        except Exception:
            bad += line + b"\n"
    return keep, bad


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs", help="runs 目录（默认 runs）")
    ap.add_argument("--fix", action="store_true", help="真改文件：丢掉坏行，坏行备份到 <file>.bad")
    ap.add_argument("--want", type=int, default=0, help="每题期望的遍数（给 0 则不判「没跑满」）")
    a = ap.parse_args()

    runs = os.path.abspath(a.runs)
    if not os.path.isdir(runs):
        print("找不到 runs 目录：" + runs)
        return 2

    fixed = []
    for fp in sorted(glob.glob(os.path.join(runs, "*.jsonl"))):
        keep, bad = broken_lines(fp)
        if not bad:
            continue
        fixed.append((os.path.basename(fp), len(bad.splitlines()), len(keep)))
        if a.fix:
            with open(fp, "wb") as f:
                f.write(b"\n".join(keep) + (b"\n" if keep else b""))
            with open(fp + ".bad", "ab") as f:
                f.write(bad)

    print("坏行检查（%s）：%s" % (runs, "已修" if a.fix else "只报告，加 --fix 才改"))
    if fixed:
        for name, nb, nk in fixed:
            print("  %-28s 丢弃 %d 行，保留 %d 行" % (name, nb, nk))
    else:
        print("  无（所有 jsonl 每行都是合法 JSON）")

    cnt = collections.defaultdict(collections.Counter)
    st = collections.Counter()
    total = 0
    for fp in sorted(glob.glob(os.path.join(runs, "attempts_*.jsonl"))):
        for line in open(fp, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            total += 1
            cnt[r.get("arm", "?")][r.get("rep", 0)] += 1
            st[str(r.get("status"))] += 1
    print("")
    print("attempts 行数：%d" % total)
    if cnt:
        print("各臂各遍已完成题数%s：" % ("（期望 %d）" % a.want if a.want else ""))
        for arm in sorted(cnt):
            cells = " ".join("r%s=%d" % (rep, cnt[arm][rep]) for rep in sorted(cnt[arm]))
            print("  %-4s %s" % (arm, cells))
    print("status：" + json.dumps(dict(st), ensure_ascii=False, sort_keys=True))

    if a.want and cnt:
        short = [(arm, rep, n) for arm, reps in cnt.items() for rep, n in reps.items() if n < a.want]
        if short:
            print("")
            print("没跑满：")
            for arm, rep, n in sorted(short):
                print("  %-4s r%s 只有 %d/%d" % (arm, rep, n, a.want))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
