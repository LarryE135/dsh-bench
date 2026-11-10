#!/usr/bin/env python3
"""去掉 xlsx 里的个人信息（docProps 的 creator / lastModifiedBy / Company / Manager）。

为什么：SpreadsheetBench 的原始表格是真人做的，xlsx 里带着作者姓名与公司。
题目本身不含这些信息，但文件元数据会跟着仓库一起公开 —— 属于不必要的个人信息暴露。

只改 docProps/core.xml 与 docProps/app.xml 里那几个字段，其余 zip 成员逐字节保留，
所以不影响表格内容、公式与样式。

用法：python bench/strip_xlsx_meta.py [tasks 目录]   # 默认 tasks
"""
import glob
import os
import re
import sys
import zipfile

# 占位值不能是测试自己的名字：leakcheck 的关键词表里就有它（实测「DSH-Bench」会被判 19 条泄漏）
PLACEHOLDER = "Anonymous"
FIELDS = ("dc:creator", "cp:lastModifiedBy", "Company", "Manager")


def strip_one(path):
    zin = zipfile.ZipFile(path)
    items = [(i, zin.read(i.filename)) for i in zin.infolist()]
    zin.close()
    out, touched = [], False
    for info, data in items:
        if info.filename in ("docProps/core.xml", "docProps/app.xml"):
            txt = data.decode("utf-8")
            pat = r"(<(?:%s)>)([^<]*)(</)" % "|".join(f.replace(":", r"\:") for f in FIELDS)
            new = re.sub(pat, r"\1" + PLACEHOLDER + r"\3", txt)
            if new != txt:
                touched = True
                data = new.encode("utf-8")
        out.append((info, data))
    if not touched:
        return False
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in out:
            zi = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            zout.writestr(zi, data)
    os.replace(tmp, path)
    return True


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "tasks"
    files = sorted(set(glob.glob(os.path.join(root, "**", "*.xlsx"), recursive=True)))
    changed = sum(1 for f in files if strip_one(f))
    print("xlsx 共 %d 个，改写元数据 %d 个（占位值：%s）" % (len(files), changed, PLACEHOLDER))
    return 0


if __name__ == "__main__":
    sys.exit(main())
