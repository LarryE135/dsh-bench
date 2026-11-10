#!/usr/bin/env python3
"""自建题生成器（零污染：题目在本机按种子现生成，任何模型都不可能背过）。

两类：
  日常办公 office-*  10 个模板 × 1 实例（另生成 1 个候补实例）
  长任务注意力
     long-needle-*  “大仓库找信息”：3 种题型（带干扰的单针 / 跨文件多跳变量追踪 / 多条件汇总）× 3 个规模（S/M/L）
     long-chain-*   “长链路多步指令”：一条很长的指令清单（10~28 步），开头立规矩，后面步骤互相依赖、
                    还会要求“撤销第 k 步”“写出第 k 步建的文件名”。专门检验“早压缩/只读新的”会不会把前面的约束忘掉。

判分：每题 hidden/expected.json + grader 名（见 selfmade_checks.py），全部检查项都过才算对；另记录通过比例便于诊断。
用法：python gen_selfmade.py --out /tmp/bench/tasks
"""
import argparse, csv, hashlib, io, json, os, random, string
from datetime import date, datetime, timedelta

SEED = 20261103
SURN = list("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗")
GIVEN = ["伟", "芳", "娜", "敏", "静", "强", "磊", "洋", "艳", "勇", "军", "杰", "娟", "涛", "明", "超", "霞", "平", "刚", "桂英"]
REGIONS = ["华东", "华南", "华北", "西南", "西北", "东北"]
WORDS = ("alpha beta gamma delta order invoice cache retry queue worker billing user account token session "
         "report metric export ingest parse render route handler config limit window batch stream index").split()


def name(rng):
    return rng.choice(SURN) + rng.choice(GIVEN)


def task(out, tid, meta, ws, hidden):
    d = os.path.join(out, tid)
    for sub, files in (("workspace", ws), ("hidden", hidden)):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
        for rel, c in files.items():
            p = os.path.join(d, sub, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "wb").write(c if isinstance(c, bytes) else c.encode("utf-8"))
    meta = {"task_id": tid, "license": "自建（CC0）", **meta}
    json.dump(meta, open(os.path.join(d, "task.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return {k: v for k, v in meta.items() if k != "prompt"}


def csv_text(rows):
    b = io.StringIO(); csv.writer(b, lineterminator="\n").writerows(rows); return b.getvalue()

# ---------------------------------------------------------------- 日常办公 10 个模板

def o_region(rng):
    custs = [(f"C{100+i}", name(rng), rng.choice(REGIONS)) for i in range(30)]
    orders, exp = [], {}
    for i in range(220):
        c = rng.choice(custs); d = date(2026, 1, 1) + timedelta(days=rng.randrange(240))
        amt = round(rng.uniform(20, 3000), 2); orders.append((f"O{5000+i}", c[0], f"{amt:.2f}", d.isoformat()))
        if date(2026, 4, 1) <= d <= date(2026, 6, 30):
            e = exp.setdefault(c[2], [0, 0.0]); e[0] += 1; e[1] += amt
    rows = sorted(([r, v[0], f"{v[1]:.2f}"] for r, v in exp.items()), key=lambda x: -float(x[2]))
    p = ("当前目录有 orders.csv（订单）和 customers.csv（客户，含所属大区）。请统计 2026 年第二季度（4月1日至6月30日，含两端）"
         "每个大区的订单数和订单总金额，写到 region_summary.csv，表头为 region,order_count,total_amount，金额保留两位小数，"
         "按 total_amount 从大到小排序。")
    ws = {"orders.csv": csv_text([("order_id", "customer_id", "amount", "date")] + orders),
          "customers.csv": csv_text([("customer_id", "name", "region")] + custs)}
    return p, ws, {"file": "region_summary.csv", "header": ["region", "order_count", "total_amount"], "rows": rows, "ordered": True}


def o_invoices(rng):
    exp, ws = [], {}
    for i in range(25):
        no = f"FP{rng.randrange(10**7, 10**8)}"; d = date(2026, 1, 1) + timedelta(days=rng.randrange(200))
        tot = round(rng.uniform(100, 99999), 2); fmt = rng.randrange(4)
        if fmt == 0:
            t = f"增值税普通发票\n发票号码：{no}\n开票日期：{d.year}年{d.month}月{d.day}日\n价税合计（小写）：¥{tot:,.2f}\n"
        elif fmt == 1:
            t = f"INVOICE\nInvoice No: {no}\nDate: {d.day} {d.strftime('%b')} {d.year}\nSubtotal: {tot*0.9:,.2f}\nTotal: {tot:,.2f} CNY\n"
        elif fmt == 2:
            t = f"收款凭证 / Receipt\n编号 No.{no}\n日期 {d.year}/{d.month:02d}/{d.day:02d}\n金额合计 RMB {tot:.2f}\n备注：含税\n"
        else:
            t = f"发票\n号码 {no}\n{d.isoformat()}\n明细若干\n总计：{tot:,.2f} 元（税额 {tot*0.06:,.2f}）\n"
        ws[f"invoices/inv_{i:02d}.txt"] = t; exp.append([no, d.isoformat(), f"{tot:.2f}"])
    exp.sort(key=lambda r: (r[1], r[0]))
    p = ("invoices/ 目录里有 25 张格式不一的发票文本。请提取每张的发票号码、开票日期、价税合计（总金额，不是小计或税额），"
         "写到 invoices.csv，表头 invoice_no,date,total；日期统一成 YYYY-MM-DD，金额去掉千分位并保留两位小数；按日期升序、同日按号码升序。")
    return p, ws, {"file": "invoices.csv", "header": ["invoice_no", "date", "total"], "rows": exp, "ordered": True}


def o_organize(rng):
    ws, exp = {}, {}
    for i in range(30):
        d = date(2025, 9, 1) + timedelta(days=rng.randrange(300)); tag = "".join(rng.choices(string.ascii_lowercase, k=6))
        f = rng.choice([f"scan_{d:%Y%m%d}_合同{i}.pdf", f"IMG-{d:%Y-%m-%d}-{tag}.jpg", f"报告 {d:%Y.%m.%d} 第{i}版.docx",
                        f"{d.day:02d}-{d.month:02d}-{d.year}_meeting{i}.txt"])
        ws[f"inbox/{f}"] = f"id={tag}{i}\n"; exp[f"{d:%Y-%m}/{f}"] = f"id={tag}{i}\n"
    for f in ("readme.txt", "模板-空白.docx", "notes_v2.md"):
        ws[f"inbox/{f}"] = f"keep {f}\n"; exp[f] = f"keep {f}\n"
    p = ("inbox/ 里是一堆从手机和扫描仪导出的文件，文件名里带日期（格式各不相同，注意 DD-MM-YYYY 这种日在前的写法）。"
         "请把带日期的文件按日期移动到 inbox/ 下的 YYYY-MM/ 子文件夹（文件名和内容都不要改）；文件名里没有日期的文件留在 inbox/ 原地。")
    return p, ws, {"root": "inbox", "files": exp}


def o_minutes(rng):
    lines, exp = ["# 项目周会纪要（2026-03-16，周一）", ""], []
    tasks = ["提交下季度预算", "更新官网首页", "联系供应商确认交期", "整理客户反馈", "修复登录页报错", "准备投标材料", "安排新人培训", "复核合同条款"]
    for t in tasks:
        who = name(rng); due = date(2026, 3, 17) + timedelta(days=rng.randrange(20)); k = rng.randrange(4)
        lines.append([f"- @{who} 负责在 {due.month}月{due.day}日 前{t}。",
                      f"- ACTION: {who} - {t} (due {due.isoformat()})",
                      f"- {who} 答应 {due:%Y/%m/%d} 之前{t}。",
                      f"- 待办：{t}，责任人 {who}，截止 {due.month:02d}-{due.day:02d}"][k])
        exp.append([who, due.isoformat()])
        if rng.random() < 0.6:
            lines.append(f"- {name(rng)} 上周已完成「{rng.choice(tasks)}」的初稿，无需跟进。")
        if rng.random() < 0.4:
            lines.append(f"- 讨论：{rng.choice(tasks)} 是否需要外包，暂无结论。")
    p = ("minutes.md 是一份会议纪要。请找出其中所有『需要跟进的待办事项』（已完成的、只是讨论没结论的不算），"
         "写成 todo.json：一个列表，每项是 {\"owner\": 责任人姓名, \"task\": 简述, \"due\": \"YYYY-MM-DD\"}。年份都按 2026 年。")
    return p, {"minutes.md": "\n".join(lines) + "\n"}, {"file": "todo.json", "pairs": sorted(exp)}


def o_config(rng):
    port = rng.randrange(8000, 9000); newport = port + rng.randrange(1, 50); w = rng.randrange(2, 9)
    ini = (f"[server]\nhost = 0.0.0.0\nport = {port}\ndebug = true\nworkers = {w}\n\n[database]\nurl = postgres://db:5432/app\n"
           f"pool_size = 5\nlegacy_mode = on\n\n[mail]\nsmtp_host = smtp.example.com\nsender = noreply@example.com\n")
    email = (f"Hi，上线前请帮我改一下 settings.ini：\n1）server 的端口改成 {newport}；\n2）生产环境要关掉 debug（写 false）；\n"
             f"3）database 里那个 legacy_mode 已经没用了，直接删掉这一项；\n4）pool_size 翻倍；\n"
             f"5）新增一个 [cache] 段，里面 backend = redis，ttl = {w*60}；\n6）mail 段的 sender 键名改成 from_addr，值不变。\n"
             "其他都别动，谢谢！\n—— 运维 小周\n")
    exp = {"server": {"host": "0.0.0.0", "port": str(newport), "debug": "false", "workers": str(w)},
           "database": {"url": "postgres://db:5432/app", "pool_size": "10"},
           "mail": {"smtp_host": "smtp.example.com", "from_addr": "noreply@example.com"},
           "cache": {"backend": "redis", "ttl": str(w*60)}}
    return "按 email.txt 里同事的要求修改当前目录的 settings.ini（直接改原文件）。", {"settings.ini": ini, "email.txt": email}, {"file": "settings.ini", "ini": exp}


def o_budget(rng):
    cats = rng.sample(["差旅", "办公用品", "餐饮", "软件订阅", "培训", "市场推广", "物流"], 5)
    rows, tot = [], {}
    for _ in range(150):
        d = date(2026, 1, 1) + timedelta(days=rng.randrange(181)); c = rng.choice(cats); a = round(rng.uniform(10, 2000), 2)
        rows.append((d.isoformat(), c, f"{a:.2f}")); tot[(c, d.month)] = round(tot.get((c, d.month), 0) + a, 2)
    p = ("expenses.csv 是上半年的报销流水。请生成 budget.xlsx，里面一个名为 Summary 的工作表：A1 写“类别”，B1:G1 依次写 1月..6月，"
         "H1 写“合计”；第 2 行起每行一个类别（按类别名的 Unicode 顺序排序），填各月金额和该类别合计；最后一行 A 列写“合计”，填每月合计和总合计。"
         "所有金额写成保留两位小数的数值（不要用公式），没有花费的月份写 0。")
    return p, {"expenses.csv": csv_text([("date", "category", "amount")] + rows)}, {"file": "budget.xlsx", "sheet": "Summary", "cats": sorted(cats), "tot": [[c, m, v] for (c, m), v in tot.items()]}


def o_dedupe(rng):
    people, rows = [], []
    for i in range(28):
        n = name(rng); e = f"{''.join(rng.choices(string.ascii_lowercase, k=5))}{i}@corp.cn"; ph = "1" + "".join(rng.choices(string.digits, k=10))
        people.append((n, e, ph))
    seq = people + rng.sample(people, 12)
    rng.shuffle(seq)
    for n, e, ph in seq:
        e2 = rng.choice([e, e.upper(), " " + e + " ", e.capitalize()])
        ph2 = rng.choice([ph, f"{ph[:3]}-{ph[3:7]}-{ph[7:]}", f"+86 {ph[:3]} {ph[3:7]} {ph[7:]}", f"({ph[:3]}){ph[3:]}"])
        rows.append((n, e2, ph2))
    seen, exp = set(), []
    for n, e, ph in rows:
        k = e.strip().lower()
        if k not in seen:
            seen.add(k); exp.append([n, k, "".join(c for c in ph.replace("+86", "") if c.isdigit())])
    p = ("contacts.csv 是从几个系统导出合并的通讯录，有重复。请生成 contacts_clean.csv（表头 name,email,phone）："
         "邮箱去掉首尾空格并转小写，以邮箱判重，只保留第一次出现的那条，保持原先后顺序；手机号去掉 +86 国家码和所有非数字字符，只留 11 位数字。")
    return p, {"contacts.csv": csv_text([("name", "email", "phone")] + rows)}, {"file": "contacts_clean.csv", "header": ["name", "email", "phone"], "rows": exp, "ordered": True}


def o_logs(rng):
    ips = [f"10.0.{rng.randrange(256)}.{rng.randrange(256)}" for _ in range(40)]
    weights = [rng.random() ** 3 for _ in ips]
    paths = ["/", "/login", "/api/orders", "/api/users", "/static/app.js", "/health", "/api/report", "/admin"]
    lines, ipc, hours, s5, up = [], {}, {}, 0, set()
    for i in range(2000):
        ip = rng.choices(ips, weights)[0]; t = datetime(2026, 10, 30, 0, 0) + timedelta(seconds=rng.randrange(86400))
        st = rng.choices([200, 301, 404, 500, 502, 503], [80, 5, 8, 3, 2, 2])[0]; pth = rng.choice(paths) + ("?id=%d" % rng.randrange(99) if rng.random() < .3 else "")
        lines.append((t, f'{ip} - - [{t:%d/%b/%Y:%H:%M:%S} +0800] "GET {pth} HTTP/1.1" {st} {rng.randrange(100, 9000)}'))
        ipc[ip] = ipc.get(ip, 0) + 1; hours[t.hour] = hours.get(t.hour, 0) + 1; s5 += st >= 500; up.add(pth.split("?")[0])
    lines.sort()
    top = sorted(ipc, key=lambda k: (-ipc[k], k))[:3]; bh = max(sorted(hours), key=lambda h: hours[h])
    p = ("access.log 是一天的 Nginx 访问日志。请写 answers.json，包含：top_ips（请求数最多的 3 个 IP，按次数降序，次数相同按 IP 字符串升序），"
         "status_5xx（状态码 ≥500 的请求数），busiest_hour（请求最多的小时，两位字符串如 \"09\"；并列时取较早的小时），unique_paths（不计查询参数 ?… 的不同路径数）。")
    return p, {"access.log": "\n".join(l for _, l in lines) + "\n"}, {"file": "answers.json", "json": {"top_ips": top, "status_5xx": s5, "busiest_hour": f"{bh:02d}", "unique_paths": len(up)}}


def o_units(rng):
    rows, exp = ["| SKU | 品名 | 重量 | 价格(元) |", "|---|---|---|---|"], []
    for i in range(18):
        kg = rng.uniform(0.05, 20); u = rng.randrange(4); price = round(rng.uniform(5, 800), 2)
        w = [f"{kg*1000:.0f}g", f"{kg:.2f}kg", f"{kg*2:.1f}斤", f"{kg/0.45359237:.2f}lb"][u]
        realkg = [round(kg*1000)/1000, round(kg, 2), round(kg*2, 1)/2, round(kg/0.45359237, 2)*0.45359237][u]
        sku = f"SKU-{rng.randrange(1000, 9999)}"; rows.append(f"| {sku} | 商品{i} | {w} | {price} |"); exp.append([sku, realkg, price/realkg])
    exp.sort(key=lambda r: r[2])
    p = ("products.md 里有一张商品表，重量单位混用了 g、kg、斤（1 斤 = 0.5 kg）和 lb（1 lb = 0.45359237 kg）。"
         "请生成 products.csv，表头 sku,weight_kg,price_per_kg：weight_kg 保留 3 位小数，price_per_kg（元/千克）用换算后未经四舍五入的重量计算、"
         "结果保留 2 位小数，按 price_per_kg 升序。")
    return p, {"products.md": "\n".join(rows) + "\n"}, {"file": "products.csv", "units": [[s, k, p] for s, k, p in exp]}


def o_calendar(rng):
    ev, ics = [], ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for i in range(25):
        d = date(2026, 11, 9) + timedelta(days=rng.randrange(5)); st = datetime.combine(d, datetime.min.time()) + timedelta(minutes=rng.randrange(16, 36) * 30)
        en = st + timedelta(minutes=rng.choice([30, 60, 90, 120])); uid = f"EV{i:02d}"; ev.append((uid, st, en))
        ics += ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:会议{i}", f"DTSTART:{st:%Y%m%dT%H%M%S}", f"DTEND:{en:%Y%m%dT%H%M%S}", "END:VEVENT"]
    ics.append("END:VCALENDAR")
    pairs = sorted(f"{min(a[0], b[0])},{max(a[0], b[0])}" for i, a in enumerate(ev) for b in ev[i+1:] if a[1] < b[2] and b[1] < a[2])
    p = ("events.ics 是下周的日程。请找出所有时间重叠的会议对（首尾刚好相接不算重叠），写入 conflicts.txt：每行一对 UID，"
         "形如 EV03,EV11（行内小的在前），各行按字符串升序，没有其它内容。")
    return p, {"events.ics": "\r\n".join(ics) + "\r\n"}, {"file": "conflicts.txt", "lines": pairs}


OFFICE = [o_region, o_invoices, o_organize, o_minutes, o_config, o_budget, o_dedupe, o_logs, o_units, o_calendar]

# ---------------------------------------------------------------- 长任务：大仓库找信息（RULER 式）
SIZES = {"S": 40, "M": 100, "L": 200}


def filler_module(rng, mod):
    out = [f'"""{mod}: {" ".join(rng.choices(WORDS, k=8))}."""', "import logging", "", "log = logging.getLogger(__name__)", ""]
    for _ in range(rng.randrange(4, 8)):
        fn = "_".join(rng.choices(WORDS, k=2)); a, b = rng.sample(WORDS, 2)
        out += [f"def {fn}({a}, {b}=None):", f'    """{" ".join(rng.choices(WORDS, k=12))}."""',
                f"    if {b} is None:", f"        {b} = {rng.randrange(1000)}",
                f"    # {' '.join(rng.choices(WORDS, k=10))}", f"    return {a} * {rng.randrange(2, 9)} + {b}", ""]
    return out


def repo(rng, n):
    mods = [f"src/{rng.choice(['core', 'svc', 'util', 'api', 'jobs'])}/{'_'.join(rng.sample(WORDS, 2))}_{i}.py" for i in range(n)]
    return mods, {m: filler_module(rng, m) for m in mods}


def n_single(rng, n):
    mods, files = repo(rng, n); val = rng.randrange(1000, 9999)
    t = rng.choice(mods[n//2:]); files[t].insert(4, f"ORDER_SERVICE_TIMEOUT_MS = {val}  # production")
    for name_, v in (("ORDER_SERVICE_TIMEOUT_S", val // 1000 + 1), ("ORDERS_SERVICE_TIMEOUT_MS", val + 17), ("ORDER_SERVICE_TIMEOUT_MS_OLD", val - 250)):
        files[rng.choice(mods)].insert(4, f"{name_} = {v}")
    files["tests/test_timeouts.py"] = ["# 测试专用覆盖值", f"ORDER_SERVICE_TIMEOUT_MS = {val + 1000}"]
    q = "在这个仓库的 src/ 下（不算 tests/），常量 ORDER_SERVICE_TIMEOUT_MS 的值是多少？注意名字相近的常量。"
    return files, q, val


def n_chain(rng, n):
    mods, files = repo(rng, n); hops = {40: 6, 100: 10, 200: 14}[n]
    chain = rng.sample(mods, hops); names = [f"K_{''.join(rng.choices(string.ascii_uppercase, k=4))}" for _ in chain]
    v = rng.randrange(1, 50); files[chain[0]].insert(4, f"{names[0]} = {v}")
    for i in range(1, hops):
        prev = chain[i-1].replace("/", ".")[:-3]; k = rng.randrange(2, 9)
        op = rng.choice(["+", "*", "-"] if abs(v) < 500 else ["+", "-"])  # 审计修正：数别滚得太大
        files[chain[i]].insert(1, f"from {prev} import {names[i-1]}")
        files[chain[i]].insert(5, f"{names[i]} = {names[i-1]} {op} {k}")
        v = v + k if op == "+" else v * k if op == "*" else v - k
    for _ in range(4):  # 干扰：同前缀、但不在链上的常量
        files[rng.choice(mods)].insert(4, f"K_{''.join(rng.choices(string.ascii_uppercase, k=4))} = {rng.randrange(999)}")
    q = f"这个仓库里常量 {names[-1]} 的最终数值是多少？（它由其它模块里的常量一步步计算而来，请按代码实际含义求值，不要运行任何代码。）"
    return files, q, v


def n_aggr(rng, n):
    mods, files = repo(rng, n); total = 0
    for m in mods:
        enabled = rng.random() < 0.5
        files[m].insert(4, f"ENABLED = {enabled}")
        for _ in range(rng.choice([0, 0, 1, 2])):
            r = rng.randrange(1, 9); route = "/" + "/".join(rng.sample(WORDS, 2))
            files[m] += [f'@route("{route}", retry={r})', f"def h_{rng.randrange(10**6)}():", "    pass", ""]
            total += r if enabled else 0
        if rng.random() < 0.15:
            files[m] += ["# 注释掉的旧路由，不算：", '# @route("/old", retry=9)', ""]
    q = ("统计这个仓库里：所有 ENABLED = True 的模块中，被 @route(...) 装饰的处理函数的 retry 参数之和是多少？"
         "（ENABLED = False 的模块和被注释掉的路由都不算。不要运行代码。）")
    return files, q, total


NEEDLE = {"single": n_single, "chain": n_chain, "aggr": n_aggr}

# ---------------------------------------------------------------- 长任务：长链路多步指令


def chain_task(rng, K):
    st = {"notes": {}, "config": {"version": 1, "counters": {"a": 0, "b": 0}, "totals": {}}, "table": [["item", "qty"]], "report": []}
    hist, steps, created, reverted = [], [], {}, set()
    word = "".join(rng.choices("ABCDEFGHJKMNPQRSTUVWXYZ", k=6))
    for i in range(1, K + 1):
        kinds = ["create", "create", "json_set", "json_inc", "csv_append", "csv_sum"]
        if st["notes"]:
            kinds += ["copy_upper", "rename", "count"]
        if len(st["notes"]) > 2:
            kinds.append("delete")
        if i > 5:
            kinds += ["revert", "recall"]
        k = rng.choice(kinds); snap = json.dumps(st["config"])
        if k == "create":
            f = f"{rng.choice(WORDS)}_{i}.txt"; c = f"{rng.choice(WORDS)}-{rng.randrange(1000)}"
            st["notes"][f] = c; created[i] = f; s = f"新建 notes/{f}，内容为一行：{c}"
        elif k == "copy_upper":
            a = rng.choice(sorted(st["notes"])); f = f"upper_{i}.txt"; st["notes"][f] = st["notes"][a].upper(); created[i] = f
            s = f"新建 notes/{f}，内容是 notes/{a} 当前内容的全大写版本"
        elif k == "rename":
            a = rng.choice(sorted(st["notes"])); f = f"renamed_{i}.txt"; st["notes"][f] = st["notes"].pop(a)
            s = f"把 notes/{a} 重命名为 notes/{f}"
        elif k == "delete":
            a = rng.choice(sorted(st["notes"])); st["notes"].pop(a); s = f"删除 notes/{a}"
        elif k == "count":
            st["config"]["totals"]["notes"] = len(st["notes"]); s = "把 notes/ 下当前 .txt 文件的个数（整数）写到 config.json 的 totals.notes"
        elif k == "json_set":
            key = rng.choice(["owner", "region", "mode"]); v = rng.choice(WORDS); st["config"][key] = v
            s = f"把 config.json 顶层键 {key} 设为字符串 \"{v}\""
        elif k == "json_inc":
            c = rng.choice("ab"); n = rng.randrange(1, 20); st["config"]["counters"][c] += n
            s = f"把 config.json 里 counters.{c} 加 {n}"
        elif k == "csv_append":
            it = rng.choice(WORDS); q = rng.randrange(1, 100); st["table"].append([it, str(q)])
            s = f"在 table.csv 末尾追加一行：{it},{q}"
        elif k == "csv_sum":
            st["config"]["totals"]["qty"] = sum(int(r[1]) for r in st["table"][1:]); s = "把 table.csv 中 qty 列当前的总和写到 config.json 的 totals.qty"
        elif k == "revert":
            j = rng.choice([h for h in range(max(1, i - 12), i) if hist[h-1][0] in ("json_set", "json_inc") and h not in reverted] or [None])
            if j is None:
                c = rng.choice("ab"); st["config"]["counters"][c] += 1; s = f"把 config.json 里 counters.{c} 加 1"; k = "json_inc"
            else:
                # 只把“第 j 步改过的那个键”恢复成第 j 步之前的值（加数则减回去），其余保持当前
                before, after = json.loads(hist[j-1][1]), json.loads(hist[j-1][2])
                cur = json.loads(json.dumps(st["config"]))
                for kk in set(before) | set(after):
                    if before.get(kk) != after.get(kk):
                        if kk == "counters":
                            for c in "ab":
                                if before["counters"][c] != after["counters"][c]:
                                    cur["counters"][c] -= after["counters"][c] - before["counters"][c]
                        elif kk in before:
                            cur[kk] = before[kk]
                        else:
                            cur.pop(kk, None)
                st["config"] = cur; reverted.add(j)
                s = f"撤销第 {j} 步对 config.json 做的修改（若是加数就减回去；若是设置键，则恢复成第 {j} 步之前的值，原来没有这个键就删掉它）"
        elif k == "recall":
            cand = [h for h in created if h < i]
            if cand:
                j = rng.choice(cand); st["report"].append(created[j]); s = f"把第 {j} 步新建的那个文件的文件名（不带路径）单独一行追加到 REPORT.md"
            else:
                st["report"].append("none"); s = "在 REPORT.md 追加一行 none"
        hist.append((k, snap, json.dumps(st["config"]))); steps.append(f"{i}. {s}")
    vendor = {"vendor/lib_cache.tmp": "do not touch\n", "vendor/README.md": "第三方代码，只读。\n", "vendor/notes/old.txt": "legacy\n"}
    final_note = (f"{K+1}. 清理：删除 notes/ 目录下所有 .tmp 文件（如果有的话）。\n{K+2}. 最后，在 REPORT.md 的第一行（最上面）插入口令（见开头规矩 4）。")
    rules = ("开工前先读规矩，整个任务全程都要遵守：\n"
             "规矩 1：vendor/ 目录是第三方代码，任何一步都不许修改、移动或删除它里面的文件。\n"
             "规矩 2：每完成一步（包括最后两步），就在 progress.log 末尾追加一行 `step N done`（N 是步号），不要有其他内容。\n"
             "规矩 3：所有文本文件以换行符结尾；config.json 保持合法 JSON。\n"
             f"规矩 4：任务结束时 REPORT.md 第一行必须是口令 {word}（只在这里说一次）。\n")
    prompt = ("这是一个很长的多步骤任务，当前目录已有 notes/、config.json、table.csv、vendor/。" + rules +
              "\n步骤如下（必须严格按顺序执行，后面的步骤可能依赖前面的结果）：\n" + "\n".join(steps) + "\n" + final_note +
              "\n全部做完后只回复“完成”。")
    ws = {"config.json": json.dumps({"version": 1, "counters": {"a": 0, "b": 0}, "totals": {}}, indent=2) + "\n",
          "table.csv": "item,qty\n", "notes/.keep": "", **vendor, "notes/scratch.tmp": "tmp\n"}
    exp = {"notes": st["notes"], "config": st["config"], "table": st["table"], "report": [word] + st["report"],
           "progress": [f"step {i} done" for i in range(1, K + 3)],
           "vendor": {p: hashlib.sha256(c.encode()).hexdigest() for p, c in vendor.items()}}
    return prompt, ws, exp


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--out", required=True); a = ap.parse_args()
    rows = []
    for t in OFFICE:
        for rep, role in ((0, "main"), (1, "reserve")):
            rng = random.Random(f"{SEED}-{t.__name__}-{rep}")
            p, ws, exp = t(rng)
            rows.append(task(a.out, f"office-self-{t.__name__[2:]}-{rep}", {
                "dimension": "日常办公", "source": "自建办公文件任务", "source_id": f"{t.__name__}#{rep}", "stratum": t.__name__,
                "role": role, "grader": f"self_{t.__name__}", "prompt": p + "\n只用当前目录里的文件，不要联网。", "timeout_s": 1200},
                ws, {"expected.json": json.dumps(exp, ensure_ascii=False, default=str)}))
    for kind, fn in NEEDLE.items():
        for size, n in SIZES.items():
            for rep, role in ((0, "main"), (1, "reserve")):
                rng = random.Random(f"{SEED}-needle-{kind}-{size}-{rep}")
                files, q, ans = fn(rng, n)
                ws = {p: ("\n".join(v) + "\n") for p, v in files.items()}
                chars = sum(len(v) for v in ws.values())
                rows.append(task(a.out, f"long-needle-{kind}-{size}-{rep}", {
                    "dimension": "长任务注意力", "source": "自建 RULER 式仓库检索", "source_id": f"{kind}/{size}#{rep}",
                    "stratum": f"{kind}-{size}", "role": role, "grader": "exact_int", "repo_chars": chars,
                    "prompt": q + "\n把最终答案（只写一个整数）保存到当前目录的 ANSWER.txt。", "timeout_s": 1500},
                    ws, {"answer.json": json.dumps({"answer": ans})}))
    for i, K in enumerate([10, 12, 14, 16, 18, 20, 24, 28, 15, 22]):
        rng = random.Random(f"{SEED}-chain-{K}-{i}")
        p, ws, exp = chain_task(rng, K)
        rows.append(task(a.out, f"long-chain-{K:02d}-{i}", {
            "dimension": "长任务注意力", "source": "自建长链路多步指令", "source_id": f"chain/K={K}#{i}", "stratum": f"K{K}",
            "role": "main" if i < 8 else "reserve", "grader": "self_chain", "prompt": p, "timeout_s": 1800},
            ws, {"expected.json": json.dumps(exp, ensure_ascii=False)}))
    with open(os.path.join(a.out, "_manifest_selfmade.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print("selfmade", len(rows), "tasks", sum(r["role"] == "main" for r in rows), "main")
