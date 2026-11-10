#!/usr/bin/env python3
"""打包工具（v1.6）：连空目录一起打进去，并复查 UTF-8 标志位。

为什么单独写一个：v1.5 用通用方式重打包时，24 道知识题的 workspace/ 是空目录，被丢掉了，
解压后 shutil.copytree 直接 FileNotFoundError —— 每个槽线程死掉，整个评测静默少跑，报告照样生成。

用法（在包根目录下）：python bench/mock/pack_package.py [--out ../dsh-bench-v1.6.zip]
"""
import argparse, os, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))
ROOT_ONLY_SKIP = {"config.json", "report.md"}  # v1.7.9
SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", "runs", "runs_pre", "runs_smoke", ".idea", ".vscode"}


def pack(src, dst):
    top = os.path.basename(os.path.abspath(src))
    n_dir = n_file = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.write(src, top + "/"); n_dir += 1
        for root, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            rel = os.path.relpath(root, src)
            if rel != ".":
                z.write(root, top + "/" + rel.replace(os.sep, "/") + "/"); n_dir += 1
            for f in sorted(files):
                if f.endswith(".pyc") or f.startswith("._"):
                    continue
                if rel == "." and f in ROOT_ONLY_SKIP:  # 只排除包根目录下的本机配置；tasks/ 里同名文件是题目的一部分，绝不能排除
                    continue
                p = os.path.join(root, f)
                z.write(p, top + "/" + os.path.relpath(p, src).replace(os.sep, "/")); n_file += 1
    return n_dir, n_file


def verify(dst):
    z = zipfile.ZipFile(dst)
    names = set(z.namelist())
    na = [i for i in z.infolist() if any(ord(c) > 127 for c in i.filename)]
    flag_bad = [i.filename for i in na if not (i.flag_bits & 0x800)]
    top = os.path.splitext(os.path.basename(dst))[0]
    tasks = [n for n in names if n.startswith(top + "/tasks/") and n.endswith("/") and n.count("/") == 3]
    no_ws = [d.rstrip("/").split("/")[-1] for d in tasks if d + "workspace/" not in names]
    print("  zip 条目 %d，非 ASCII 名 %d（未置 UTF-8 标志位 %d）" % (len(names), len(na), len(flag_bad)))
    print("  题目目录 %d 个，缺 workspace/ 的 %d 个" % (len(tasks), len(no_ws)))
    bad = []
    if flag_bad:
        bad.append("未置 UTF-8 标志位：" + str(flag_bad[:5]))
    if no_ws:
        bad.append("缺 workspace/ 目录条目：" + str(no_ws[:5]))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=PKG)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    src = os.path.abspath(a.src)
    out = a.out or os.path.join(os.path.dirname(src), os.path.basename(src) + ".zip")
    n_dir, n_file = pack(src, out)
    print("packed %d 个目录条目 + %d 个文件 -> %s" % (n_dir, n_file, out))
    bad = verify(out)
    for b in bad:
        print("  ✘ " + b)
    print("打包体检：" + ("通过" if not bad else "有问题"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())