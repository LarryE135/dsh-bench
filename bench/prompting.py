#!/usr/bin/env python3
"""把题目组装成交给被测 AI 的最终任务文本（各臂完全一致）。

最终文本 = prompts/common.md（通用工作约束）+ 题目正文 + 按题型选的“交付要求”模板。
题目正文来自 task.json 的 prompt，但先去掉其中会暴露“这是测试”的说法（判分、隐藏测试、练习、LeetCode/AtCoder 风格、
“长文阅读题”等）和原来的交付说明，交付说明统一改由模板给出。
用法：python prompting.py <题目目录>   # 打印组装结果，方便人工检查
"""
import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = os.path.join(os.path.dirname(HERE), "prompts")
CASE_SECONDS = 6

TEMPLATE_OF = {"mcq": "choice", "exact_int": "integer", "lcb_stdio": "algo_stdio", "lcb_functional": "algo_func",
               "polyglot_python": "code_project", "polyglot_javascript": "code_project", "ssb_cells": "sheet",
               "self_chain": "longtask"}  # 其余 self_o_* → office


def template_name(task):
    return TEMPLATE_OF.get(task["grader"], "office" if task["grader"].startswith("self_o_") else None)


def _read(name):
    return open(os.path.join(PROMPTS, name + ".md"), encoding="utf-8").read().strip("\n")


def _cut(s, marker):
    i = s.find(marker)
    return s[:i] if i >= 0 else s


def ssb_range_text(task):
    """把判分区域写成人能读懂的样子（v1.5：原始数据里的区域串有的丢了开头引号，如 worksheet2'!C3:C6）。"""
    import grade
    rs = grade._ranges(task.get("answer_position") or "", task.get("answer_sheet"))
    by = {}
    for sh, rg in rs:
        by.setdefault(sh, []).append(rg)
    parts = [(f"工作表「{sh}」的 " if sh else "") + "、".join(rgs) for sh, rgs in by.items()]
    return "；".join(parts) or "相关"


def body_and_vars(task):
    """返回（清理后的正文, 模板变量）。每条规则都对应原题面里的一种固定写法。"""
    s, g, v = task["prompt"], task["grader"], {}
    if g.startswith("lcb_"):
        s = re.sub(r"^编程题：[^\n]*\n+", "", s)  # 原标题行（有的是数字编号，没有信息量）
        s = _cut(_cut(s, "请补全当前目录 solution.py"), "请在当前目录写 solution.py")
        v = {"func_name": task.get("func_name", ""), "case_seconds": CASE_SECONDS}
    elif g.startswith("polyglot_"):
        m = re.search(r"请修改当前目录下的 (\S+) 完成上述练习", s)
        v = {"file": m.group(1) if m else "源文件", "lang": "Python " if g.endswith("python") else "JavaScript（Node.js）"}
        s = _cut(s, "请修改当前目录下的")
    elif g == "mcq":
        s = _cut(s, "请把你的最终答案只写一个大写选项字母")
        s = re.sub(r"^长文阅读题。材料在当前目录的 (\S+)（很长，请自己决定怎么读）。",
                   r"请阅读当前目录的 \1（篇幅很长，怎么读由你决定），然后回答下面的问题。", s)
    elif g == "exact_int":
        s = _cut(s, "把最终答案（只写一个整数）保存到当前目录的 ANSWER.txt")
    elif g == "ssb_cells":
        s = re.sub(r"^日常办公·表格任务。当前目录有 input\.xlsx。需求如下：\s*", "当前目录有 input.xlsx。需求如下：\n\n", s)
        v = {"range": ssb_range_text(task)}
        s = _cut(s, "请把处理后的工作簿另存为")
    elif g == "self_chain":
        s = _cut(s, "全部做完后只回复“完成”。")
    else:  # 自建办公
        s = s.replace("\n只用当前目录里的文件，不要联网。", "")
    return s.strip(), v


def render(task):
    body, v = body_and_vars(task)
    deliver = _read(template_name(task)).format(**v)
    minutes = max(1, round(task.get("timeout_s", 1200) / 60))
    return _read("common").format(minutes=minutes, body=body, deliver=deliver)


if __name__ == "__main__":
    t = json.load(open(os.path.join(sys.argv[1], "task.json"), encoding="utf-8"))
    print(render(t))
