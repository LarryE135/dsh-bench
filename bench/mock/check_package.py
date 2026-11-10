#!/usr/bin/env python3
"""打包体检：检查 zip 本身和题包内部是否自洽（不跑模型）。

查四件事：
  1. 压缩包里每个非 ASCII 文件名都置了 UTF-8 标志位（0x800）——否则解压出来就是乱码，
     office-self-organize 这种按精确路径比对的题会两个臂一起全错；
  2. 每道题的 hidden/ 都在 _answers.bin 里（缺了就是“判分器必崩”，会被当成坏题剔除）；
  3. manifest 与实际题目目录一致（题数、role）；
  4. 每道题的题面都能渲染、模板变量都填上了。

用法：python bench/mock/check_package.py [--zip ../dsh-bench-v1.4.zip] [--pkg <包目录>]
"""
import argparse, csv, json, os, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(HERE))


def check_zip(path):
    bad = []
    z = zipfile.ZipFile(path)
    infos = z.infolist()
    nonascii = [i for i in infos if any(ord(c) > 127 for c in i.filename)]
    for i in nonascii:
        if not (i.flag_bits & 0x800):
            bad.append(f"未置 UTF-8 标志位：{i.filename!r}")
    utf8_ok = len(nonascii) - len(bad)
    print(f"  zip 条目 {len(infos)}，非 ASCII 名 {len(nonascii)}，其中标志位正确 {utf8_ok}")
    return bad


def check_pkg(pkg, z=None):
    bad = []
    sys.path.insert(0, os.path.join(pkg, "bench"))
    import answers, prompting
    tasks = os.path.join(pkg, "tasks")
    zz = answers._load(tasks)
    packed = {n.split("/", 1)[0] for n in (zz.namelist() if zz else [])}
    tids = [d for d in sorted(os.listdir(tasks)) if os.path.isdir(os.path.join(tasks, d))]
    missing = [t for t in tids if t not in packed]
    if missing:
        bad.append(f"这些题没有答案包：{missing[:5]}")
    leaked = [t for t in tids if os.path.isdir(os.path.join(tasks, t, "hidden"))]
    if leaked:
        bad.append(f"这些题的 hidden/ 还是明文（应已 pack 掉）：{leaked[:5]}")
    nws = [t for t in tids if not os.path.isdir(os.path.join(tasks, t, "workspace"))]
    if nws:
        bad.append(f"这些题没有 workspace/ 目录（v1.5 就是这样丢了 24 个空目录，run_slot 会崩、整槽停机）：{nws[:5]}")
    # v1.7.9：逐题核对 workspace 文件清单与内容指纹（v1.7.8 打包时把 10 道长链路题的 workspace/config.json 当成
    # “本机配置”排除掉了，题目照样能跑，但 config 检查项变成不可能做对，长任务维度整体失真）
    fl_path = os.path.join(pkg, "manifest", "workspace_files.json")
    flist = json.load(open(fl_path, encoding="utf-8")) if os.path.exists(fl_path) else None
    if flist is None:
        bad.append("缺 manifest/workspace_files.json（逐题文件清单），无法确认题目文件齐全")
    else:
        import hashlib
        for t in tids:
            w = os.path.join(tasks, t, "workspace"); want = flist.get(t, {})
            have = {}
            for dp, dn, fn in os.walk(w):
                for f in fn:
                    fp = os.path.join(dp, f)
                    have[os.path.relpath(fp, w).replace(os.sep, "/")] = hashlib.sha256(open(fp, "rb").read()).hexdigest()[:16]
            lost = sorted(set(want) - set(have)); extra = sorted(set(have) - set(want))
            changed = sorted(k for k in set(want) & set(have) if want[k] != have[k])
            if lost or extra or changed:
                bad.append(f"{t}: 题目文件与清单不符 缺{lost[:3]} 多{extra[:3]} 改{changed[:3]}")
        if z is not None:
            tops = sorted({n.split("/")[0] for n in z.namelist() if "/" in n})
            pre = (tops[0] + "/") if tops else ""
            zn = set(z.namelist())
            zlost = [f"{t}/{f}" for t, fs in flist.items() for f in fs if f"{pre}tasks/{t}/workspace/{f}" not in zn]
            if zlost:
                bad.append(f"zip 里缺题目文件 {len(zlost)} 个，例如 {zlost[:3]}（检查打包脚本的排除规则）")
    rows = list(csv.DictReader(open(os.path.join(pkg, "manifest", "manifest.csv"), encoding="utf-8-sig")))
    if {r["task_id"] for r in rows} != set(tids):
        bad.append("manifest 与题目目录不一致（先跑 python bench/bench.py manifest）")
    for t in tids:
        tp = os.path.join(tasks, t, "task.json")
        try:
            task = json.load(open(tp, encoding="utf-8"))
            txt = prompting.render(task)
            if "{" in txt.split("交付要求：")[-1]:
                bad.append(f"{t}: 交付模板有没填上的变量")
        except Exception as ex:
            bad.append(f"{t}: 题面渲染失败 {ex!r}"[:160])
    if z is not None:
        tops = sorted({n.split("/")[0] for n in z.namelist() if "/" in n})
        prefix = (tops[0] + "/") if tops else ""
        names = set(z.namelist())
        nodir = [t for t in tids if f"{prefix}tasks/{t}/workspace/" not in names]
        if nodir:
            bad.append(f"zip 里缺 workspace/ 目录条目（空目录被丢）：{nodir[:5]}；用 python bench/mock/pack_package.py 重新打包")
    print(f"  题目 {len(tids)} 道，答案包覆盖 {len(packed)} 道，manifest {len(rows)} 行")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=None)
    ap.add_argument("--pkg", default=PKG)
    a = ap.parse_args()
    z = zipfile.ZipFile(a.zip) if a.zip else None
    bad = []
    if z is not None:
        bad += check_zip(a.zip)
    bad += check_pkg(a.pkg, z)
    for b in bad[:20]:
        print("  ✘ " + b)
    print("打包体检：" + ("通过" if not bad else f"{len(bad)} 个问题"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
