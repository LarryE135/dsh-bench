#!/usr/bin/env python3
"""清理题目工作目录里会暴露“题源/测试”的东西（维护用；幂等，可重复运行）。2026-11-04 v1.3 加入。

1. Aider Polyglot（来自 Exercism）：
   - 删掉以点开头的目录（如 .approaches 是官方“解题思路”，等于把答案思路交给模型）和 .eslintrc、LICENSE（许可记在题单里）；
   - package.json 换成中性的最小配置；babel.config.js 改用通用的 @babel/preset-env（原来是 @exercism/... 预设）。
2. SpreadsheetBench：xlsx 的 workbook.xml 里残留了出题人电脑上的绝对路径（含数据集目录名），删掉这一段。
用法：python sanitize_tasks.py <tasks_dir>
"""
import io, json, os, re, shutil, sys, zipfile

PKG = {"name": "app", "version": "1.0.0", "private": True, "scripts": {"test": "jest"},
       "devDependencies": {"@babel/core": "^7.25.2", "@babel/preset-env": "^7.25.4", "babel-jest": "^29.7.0",
                           "core-js": "~3.37.1", "jest": "^29.7.0"}}
BABEL = "module.exports = {\n  presets: [['@babel/preset-env', { targets: { node: 'current' } }]],\n};\n"
ABSPATH = re.compile(rb"<mc:AlternateContent[^>]*>(?:(?!</mc:AlternateContent>).)*?x15ac:absPath(?:(?!</mc:AlternateContent>).)*</mc:AlternateContent>", re.S)


def clean_polyglot(ws):
    for n in os.listdir(ws):
        p = os.path.join(ws, n)
        if n.startswith(".") and os.path.isdir(p):
            shutil.rmtree(p)
        elif n in (".eslintrc", "LICENSE"):
            os.remove(p)
    if os.path.exists(os.path.join(ws, "package.json")):
        json.dump(PKG, open(os.path.join(ws, "package.json"), "w", encoding="utf-8"), indent=2)
        open(os.path.join(ws, "babel.config.js"), "w", encoding="utf-8").write(BABEL)


def clean_xlsx(path):
    src = open(path, "rb").read(); zin = zipfile.ZipFile(io.BytesIO(src)); changed = False; buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info)
            if info.filename == "xl/workbook.xml" and b"absPath" in data:
                data = ABSPATH.sub(b"", data); changed = True
            zout.writestr(info, data)
    if changed:
        open(path, "wb").write(buf.getvalue())
    return changed


def main(tasks):
    n = 0
    for tid in sorted(os.listdir(tasks)):
        ws = os.path.join(tasks, tid, "workspace")
        if not os.path.isdir(ws):
            continue
        if tid.startswith("code-poly-"):
            clean_polyglot(ws); n += 1
        for root, _, files in os.walk(ws):
            for f in files:
                if f.lower().endswith((".xlsx", ".xlsm")):
                    n += clean_xlsx(os.path.join(root, f))
    print("cleaned", n)


if __name__ == "__main__":
    main(sys.argv[1])
