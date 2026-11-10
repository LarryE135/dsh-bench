#!/usr/bin/env python3
"""题目包自检：manifest、tasks/ 目录、答案桶三者是否一致。不联网、不跑模型。

检查项（任一不符即 exit 1）：
  1. manifest.csv / manifest.jsonl / tasks/ 三方的 task_id 集合完全相同；
  2. 每道题都有 task.json，且必备字段齐全（task_id/dimension/source/source_id/license/role/grader/timeout_s/prompt）；
  3. task.json 的 task_id 与目录名、manifest 行一致；license/source/grader/role 与 manifest 一致；
  4. task.json 里 grader 需要的附加字段存在（见 REQUIRED_BY_GRADER）；
  5. 答案桶 tasks/_answers.bin 覆盖所有带 hidden/ 的题（用 answers.unpack 前的只读检查）；
  6. manifest/workspace_files.json 与实际 workspace/ 文件清单一致。

用法：python bench/check_tasks.py [--root .]
"""
import argparse, hashlib
import collections
import io
import json
import os
import sys
import re
import zipfile

BASE_FIELDS = ["task_id", "dimension", "source", "source_id", "license", "role", "grader", "timeout_s", "prompt"]
REQUIRED_BY_GRADER = {
    "lcb_functional": ["func_name"],
    "polyglot_python": ["test_files"],
    "polyglot_javascript": ["test_files"],
    "ssb_cells": ["answer_sheet", "answer_position"],
}


def known_graders():
    """判分器名单不在这里手写：直接读 bench/grade.py 的 GRADERS 字典键，避免两边漂移。"""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "grade.py"), encoding="utf-8").read()
    block = src.split("GRADERS = {", 1)[1].split("}", 1)[0]
    return set(re.findall(r'"([a-z0-9_]+)"\s*:', block))


def load_csv(path):
    import csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def answer_bucket(path):
    """_answers.bin 是异或混淆的 zip，只读列目录，不改文件。"""
    if not os.path.exists(path):
        return None
    key = b"DSH-Bench-v1|20261103|not-a-secret"
    raw = open(path, "rb").read()
    plain = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    z = zipfile.ZipFile(io.BytesIO(plain))
    return collections.Counter(n.split("/", 1)[0] for n in z.namelist() if "/" in n)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    tasks_dir = os.path.join(root, "tasks")
    man_csv = os.path.join(root, "manifest", "manifest.csv")
    man_jsonl = os.path.join(root, "manifest", "manifest.jsonl")
    ws_files = os.path.join(root, "manifest", "workspace_files.json")

    problems = []
    notes = []
    KNOWN_GRADERS = known_graders()

    csv_rows = load_csv(man_csv)
    csv_ids = [r["task_id"] for r in csv_rows]
    jsonl_ids = [json.loads(l)["task_id"] for l in open(man_jsonl, encoding="utf-8") if l.strip()]
    dir_ids = sorted(d for d in os.listdir(tasks_dir)
                     if os.path.isdir(os.path.join(tasks_dir, d)) and not d.startswith("_"))

    if collections.Counter(csv_ids) != collections.Counter(jsonl_ids):
        problems.append("manifest.csv 与 manifest.jsonl 的 task_id 集合不同：仅 csv 有 %s；仅 jsonl 有 %s"
                        % (sorted(set(csv_ids) - set(jsonl_ids)), sorted(set(jsonl_ids) - set(csv_ids))))
    if collections.Counter(csv_ids) != collections.Counter(dir_ids):
        problems.append("manifest 与 tasks/ 目录不匹配：仅 manifest 有 %s；仅目录有 %s"
                        % (sorted(set(csv_ids) - set(dir_ids)), sorted(set(dir_ids) - set(csv_ids))))
    dup = [k for k, v in collections.Counter(csv_ids).items() if v > 1]
    if dup:
        problems.append("manifest.csv 里 task_id 重复：" + ", ".join(sorted(dup)))

    jrow = {}
    _jl = os.path.join(root, "manifest", "manifest.jsonl")
    if os.path.isfile(_jl):
        for _line in open(_jl, encoding="utf-8"):
            _line = _line.strip()
            if not _line:
                continue
            try:
                _o = json.loads(_line)
                jrow[_o.get("task_id")] = _o
            except Exception:
                problems.append("manifest.jsonl 里有解析不了的行")
    by_id = {r["task_id"]: r for r in csv_rows}
    for tid in dir_ids:
        tdir = os.path.join(tasks_dir, tid)
        tj = os.path.join(tdir, "task.json")
        if not os.path.isfile(tj):
            problems.append(tid + "：缺 task.json")
            continue
        try:
            task = json.load(open(tj, encoding="utf-8"))
        except Exception as e:
            problems.append(tid + "：task.json 解析失败（%s）" % e)
            continue
        for f in BASE_FIELDS:
            if f not in task or task[f] in ("", None):
                problems.append("%s：task.json 缺字段 %s" % (tid, f))
        if task.get("task_id") != tid:
            problems.append("%s：task.json 的 task_id=%r 与目录名不一致" % (tid, task.get("task_id")))
        row = by_id.get(tid)
        if row:
            for f in ("license", "source", "grader", "role", "dimension"):
                if str(task.get(f, "")) != str(row.get(f, "")):
                    problems.append("%s：%s 不一致（task.json=%r / manifest.csv=%r）"
                                    % (tid, f, task.get(f), row.get(f)))
        jr = jrow.get(tid)
        if jr:
            for f in ("license", "source", "grader", "role", "dimension"):
                if f in jr and str(jr.get(f, "")) != str(row.get(f, "")):
                    problems.append("%s：manifest.jsonl 的 %s 与 csv 不一致（jsonl=%r / csv=%r）"
                                    % (tid, f, jr.get(f), row.get(f)))
        g = task.get("grader")
        if g not in KNOWN_GRADERS:
            problems.append("%s：未知 grader %r（bench/grade.py 的 GRADERS 里没有）" % (tid, g))
        else:
            for f in REQUIRED_BY_GRADER.get(g, []):
                if f not in task:
                    problems.append("%s：grader=%s 需要字段 %s" % (tid, g, f))
        if not os.path.isdir(os.path.join(tdir, "workspace")):
            notes.append(tid + "：没有 workspace/（纯问答题可以没有，其它题型请确认）")

    bucket = answer_bucket(os.path.join(tasks_dir, "_answers.bin"))
    if bucket is None:
        problems.append("缺 tasks/_answers.bin（判分读不到答案与隐藏测试）")
    else:
        hidden_missing = [tid for tid in dir_ids
                          if not os.path.isdir(os.path.join(tasks_dir, tid, "hidden")) and tid not in bucket]
        if hidden_missing:
            problems.append("这些题既没有明文 hidden/、答案桶里也没有：%s" % ", ".join(sorted(hidden_missing)[:12]))
        extra = [tid for tid in bucket if tid not in set(dir_ids)]
        if extra:
            problems.append("答案桶里有题号但 tasks/ 里没有：%s" % ", ".join(sorted(extra)[:12]))

    if os.path.isfile(ws_files):
        declared = json.load(open(ws_files, encoding="utf-8"))
        actual = {}  # tid -> {相对路径: sha256 前 16 位}
        for tid in dir_ids:
            wdir = os.path.join(tasks_dir, tid, "workspace")
            files = {}
            for r, _, fs in os.walk(wdir):
                for f in fs:
                    fp = os.path.join(r, f)
                    rel = os.path.relpath(fp, wdir).replace(os.sep, "/")
                    try:
                        files[rel] = hashlib.sha256(open(fp, "rb").read()).hexdigest()[:16]
                    except OSError as ex:
                        problems.append("%s：workspace 文件读不出来 %s（%s）" % (tid, rel, ex))
            actual[tid] = files
        if isinstance(declared, dict):
            for tid, files in declared.items():
                if tid not in actual:
                    problems.append("workspace_files.json 里有不存在的题：" + tid)
                else:
                    decl = dict(files) if isinstance(files, dict) else {k: None for k in files}
                    cur = actual[tid]
                    only_d = sorted(set(decl) - set(cur))
                    only_a = sorted(set(cur) - set(decl))
                    if only_d or only_a:
                        problems.append("%s：workspace 清单不一致（清单多 %s / 实际多 %s）"
                                        % (tid, only_d[:5], only_a[:5]))
                    else:
                        dirty = [k for k, v in decl.items() if v and cur.get(k) != v]  # 只比名字查不出改内容
                        if dirty:
                            problems.append("%s：workspace 文件内容被改过（哈希不符）：%s"
                                            % (tid, ", ".join(sorted(dirty)[:5])))
    else:
        notes.append("没有 manifest/workspace_files.json（跳过清单核对）")

    up = os.path.join(root, "manifest", "upstream.json")
    if os.path.exists(up):
        spec = json.load(open(up, encoding="utf-8"))
        ext = [x["task_id"] for x in spec.get("tasks", [])]
        leaked = sorted(set(ext) & set(dir_ids))
        if leaked:
            # 本地抓回来跑是允许的（.gitignore 里已按题号前缀排除），但不能被提交
            notes.append("有 %d 道受限题已被本地抓回（%s…）：跑得动，但 .gitignore 已排除，别提交"
                         % (len(leaked), leaked[0]))
        miss = sorted({x.get("provider") for x in spec.get("tasks", [])} - set(spec.get("providers") or {}))
        if miss:
            problems.append("upstream.json 里有题引用了未记录的 provider：%s" % ", ".join(map(str, miss)))
        notes.append("按上游许可只披露、不发布：%d 道（python bench/fetch_data.py --provider <名字> 可本地抓回）" % len(ext))
    print("题目包自检：%d 个任务目录 / manifest.csv %d 行 / manifest.jsonl %d 行 / %d 种判分器"
          % (len(dir_ids), len(csv_ids), len(jsonl_ids), len(KNOWN_GRADERS)))
    print("答案桶：%s" % ("%d 题" % len(bucket) if bucket else "缺失"))
    for n in notes:
        print("  提示：" + n)
    if problems:
        print("")
        print("✖ %d 处不一致：" % len(problems))
        for p in problems:
            print("  - " + p)
        return 1
    print("✓ 三方一致：manifest.csv / manifest.jsonl / tasks/ / _answers.bin")
    return 0


if __name__ == "__main__":
    sys.exit(main())