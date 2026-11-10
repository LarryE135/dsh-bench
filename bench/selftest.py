#!/usr/bin/env python3
"""判分器自检：对每道题造一份“标准答案”和一份“空答卷”，确认标准答案判对、空答卷判错。
标准答案能自动造的题型：选择题、整数题、全部自建题；（LCB/Polyglot/表格题没有现成标准解，只做“空答卷必须判错”检查。）
用法：python selftest.py <tasks_dir>
"""
import csv, io, json, os, shutil, sys, tempfile
import answers, grade


def oracle(task, tdir, rdir):
    g = task["grader"]; hid = os.path.join(tdir, "hidden")
    if g in ("mcq", "exact_int"):
        open(os.path.join(rdir, "ANSWER.txt"), "w").write(str(json.load(open(os.path.join(hid, "answer.json")))["answer"]))
        return True
    if not g.startswith("self_"):
        return False
    e = json.load(open(os.path.join(hid, "expected.json"), encoding="utf-8"))
    W = lambda p, t: (os.makedirs(os.path.dirname(os.path.join(rdir, p)) or rdir, exist_ok=True), open(os.path.join(rdir, p), "w", encoding="utf-8", newline="").write(t))
    def C(rows):
        b = io.StringIO(); csv.writer(b, lineterminator="\n").writerows(rows); return b.getvalue()
    if g in ("self_o_region", "self_o_invoices", "self_o_dedupe"):
        W(e["file"], C([e["header"]] + e["rows"]))
    elif g == "self_o_organize":
        shutil.rmtree(os.path.join(rdir, e["root"]))
        for k, v in e["files"].items():
            W(os.path.join(e["root"], k), v)
    elif g == "self_o_minutes":
        W(e["file"], json.dumps([{"owner": o, "task": "x", "due": d} for o, d in e["pairs"]], ensure_ascii=False))
    elif g == "self_o_config":
        s = "".join(f"[{sec}]\n" + "".join(f"{k} = {v}\n" for k, v in kv.items()) + "\n" for sec, kv in e["ini"].items()); W(e["file"], s)
    elif g == "self_o_budget":
        import openpyxl
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = e["sheet"]; tot = {(c, m): v for c, m, v in e["tot"]}
        ws.append(["类别"] + [f"{m}月" for m in range(1, 7)] + ["合计"])
        for c in e["cats"]:
            r = [round(tot.get((c, m), 0), 2) for m in range(1, 7)]; ws.append([c] + r + [round(sum(r), 2)])
        r = [round(sum(tot.get((c, m), 0) for c in e["cats"]), 2) for m in range(1, 7)]; ws.append(["合计"] + r + [round(sum(r), 2)])
        wb.save(os.path.join(rdir, e["file"]))
    elif g == "self_o_logs":
        W(e["file"], json.dumps(e["json"]))
    elif g == "self_o_units":
        W(e["file"], C([["sku", "weight_kg", "price_per_kg"]] + [[s, f"{k:.3f}", f"{p:.2f}"] for s, k, p in e["units"]]))
    elif g == "self_o_calendar":
        W(e["file"], "\n".join(e["lines"]) + "\n")
    elif g == "self_chain":
        nd = os.path.join(rdir, "notes")
        for f in os.listdir(nd):
            if not f.startswith("."):
                os.remove(os.path.join(nd, f))
        for k, v in e["notes"].items():
            W(f"notes/{k}", v + "\n")
        W("config.json", json.dumps(e["config"], indent=2) + "\n"); W("table.csv", C(e["table"]))
        W("REPORT.md", "\n".join(e["report"]) + "\n"); W("progress.log", "\n".join(e["progress"]) + "\n")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__ or "")
        print("用法：python bench/selftest.py <tasks 目录>　例如：python bench/selftest.py tasks")
        sys.exit(2)
    root = sys.argv[1]
    if not os.path.isdir(root):
        print("不是目录：" + root)
        sys.exit(2)
    bad = 0; n = 0

    _jest = os.path.join(os.path.expanduser("~"), ".cache", "nodekit", "node_modules", "jest", "bin", "jest.js")
    if not os.environ.get("SKIP_JS") and not os.path.isfile(_jest):
        print("提示：本机还没有 jest 缓存，判 JS 题会先联网 npm install（离线请 SKIP_JS=1 跳过 7 道 JS 题）")
    for tid in sorted(os.listdir(root)):
        tdir = os.path.join(root, tid)
        if not os.path.isfile(os.path.join(tdir, "task.json")):
            continue
        task = json.load(open(os.path.join(tdir, "task.json"), encoding="utf-8"))
        if os.environ.get("SKIP_JS") and task["grader"] == "polyglot_javascript":
            continue
        n += 1
        for mode in ("empty", "oracle"):
            rdir = tempfile.mkdtemp(prefix="dshb_")
            shutil.rmtree(rdir); shutil.copytree(os.path.join(tdir, "workspace"), rdir)
            if mode == "oracle":
                with answers.view(tdir) as v:
                    ok = oracle(task, v, rdir)
                if not ok:
                    shutil.rmtree(rdir, ignore_errors=True); continue
            r = grade.grade(tdir, rdir); shutil.rmtree(rdir, ignore_errors=True)
            want = mode == "oracle"
            # 判分器自己抛异常时 grade() 返回 pass=False，在 empty 模式下看起来「符合预期」，
            # 于是缺 openpyxl / 缺 pytest 这类故障永远发现不了（P0-2）。这里单独判失败。
            if (r.get("detail") or {}).get("grader_error"):
                bad += 1; print("GRADER_ERROR", mode, tid, str(r["detail"]["grader_error"])[:160]); continue
            if r["pass"] != want:
                bad += 1; print("FAIL", mode, tid, json.dumps(r, ensure_ascii=False)[:300])
    print(f"checked {n} tasks, {bad} problems")
    # 审计 P0-2：没有退出码时 CI 永远绿 —— 判分器坏成什么样都拦不住
    sys.exit(1 if bad else 0)
