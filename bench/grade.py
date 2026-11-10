#!/usr/bin/env python3
"""判分器：给一道题的“运行目录”（agent 跑完后的工作区）判 0/1，并给出诊断。

用法：python grade.py <task_dir> <run_dir>    -> 打印 JSON {"pass": bool, "frac": 0~1, "detail": ...}
原则：全部客观自动判分，不用大模型当裁判（裁判本身有噪声，会掩盖两臂的小差距）。
依赖：Python 3.10+；表格题需要 openpyxl；JS 题需要 Node 18+ 与 npm（首次会在缓存目录装一次 jest）。
"""
import csv, glob, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, uuid

PY = sys.executable


def decode_text(b):
    """按“最可能的编码”解码模型写出的文本文件（v1.7.1）：
    Windows 上 Python 不写 encoding 时默认 GBK，PowerShell 5.1 的 > / Out-File 默认 UTF-16LE。
    只认 UTF-8 会把内容完全正确的答卷判错；插件若改变默认编码还会造成臂间偏差。
    顺序：BOM → 无 BOM 的 UTF-16（先看 NUL 比例）→ 严格 UTF-8 →（UTF-8 成功但含控制字符时回退试 UTF-16）→ GB18030。
    v1.7.1：内容全是汉字的 UTF-16 没有任何 NUL 字节，会被 UTF-8 解成“看着像 ASCII”的乱码，这里补一条回退。"""
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8", "replace")
    if b.startswith((b"\xff\xfe", b"\xfe\xff")):
        return b.decode("utf-16", "replace")
    if len(b) >= 4:  # 无 BOM 的 UTF-16：看奇/偶位置上 NUL 的分布判断端序
        even_nul = b[0::2].count(0)
        odd_nul = b[1::2].count(0)
        if max(even_nul, odd_nul) * 4 >= len(b) / 2:  # 至少 1/4 的字符位置是 NUL
            return b.decode("utf-16-le" if odd_nul >= even_nul else "utf-16-be", "replace")
    try:
        s = b.decode("utf-8")
    except UnicodeDecodeError:
        s = b.decode("gb18030", "replace")
    # v1.7.1：解出来含 C0 控制字符（\t\n\r 除外）通常说明编码认错了（全汉字 UTF-16 既没有 NUL 也过不了严格 UTF-8，
    # 会被 GB18030 解成乱码），再试 UTF-16 两头，取控制字符/替换符最少的那个。
    if len(b) >= 4 and len(b) % 2 == 0 and any(ord(c) < 32 and c not in "\t\n\r" for c in s):
        cands = [s]
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                cands.append(b.decode(enc))
            except UnicodeDecodeError:
                pass
        def ctl(t):  # 控制字符越少越好，其次替换符越少越好
            return (sum(1 for c in t if ord(c) < 32 and c not in "\t\n\r"), t.count("\ufffd"))
        s = min(cands, key=ctl)
    return s

def read(p, default=None, limit=4 << 20):
    """读文本。拒绝设备/FIFO（/dev/zero 会把判分进程 OOM 掉，实测 exit 137），并限长。"""
    try:
        if not stat.S_ISREG(os.stat(p).st_mode):
            return default
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return default

def result(ok, frac=None, **detail):
    f = float(ok if frac is None else frac)
    return {"pass": bool(ok), "frac": max(0.0, min(1.0, f)), "detail": detail}  # 以前 g_budget 会给出 -0.119

# ------------------------------------------------------------ 选择题 / 整数


_MARK = ('答案', '正解', 'answer', 'Answer', 'ANSWER', '选项')  # 裸「选」会把「选择方案 2…」当成答案
_TRIM = set(" 	()[]{}<>*_`\"'.,。、:：;；-—【】（）")


def _strip_marks(s):
    s = s.strip()
    while s and s[0] in _TRIM:
        s = s[1:]
    while s and s[-1] in _TRIM:
        s = s[:-1]
    return s


def _cands(s, valid):
    # 字母候选：前后都不是 ASCII 字母才算独立选项，避免把英文单词里的 I、A 读成答案。
    out = set()
    for k, ch in enumerate(s):
        u = ch.upper()
        if u not in valid:
            continue
        prev = s[k - 1] if k else ' '
        nxt = s[k + 1] if k + 1 < len(s) else ' '
        if (prev.isascii() and prev.isalpha()) or (nxt.isascii() and nxt.isalpha()):
            continue
        out.add(u)
    return out


def _mcq_pick(txt, valid):
    '''按「越明确越优先」三级判定选项字母，返回 (字母或 None, 是否歧义)。

    v1.0.1 审计修正：原来要求「整份答卷里只出现一个合法大写字母」，于是
    `答案：D` 后面跟一句 `理由：其余选项 A、C 都不符` 就会因为理由里的 A、C 判错，
    小写 `d` 也不认 —— 41 道选择题测的是措辞而不是知识，而换预设恰好系统性地改变答题风格。
    现在：① 单独成行的字母；② 作答标志之后的字母；③ 全文唯一字母（`B 和 C 都不对` 仍判歧义）。
    '''
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    alone = []
    for l in lines:
        s = _strip_marks(l)
        if len(s) == 1 and s.upper() in valid:
            alone.append(s.upper())
    if len(set(alone)) == 1:
        return alone[0], False
    for l in lines:
        low = l.lower()
        best = -1
        for mk in _MARK:
            k = low.find(mk.lower())
            if k >= 0 and (best < 0 or k < best):
                best = k
                span = len(mk)
        if best < 0:
            continue
        tail = l[best + span:]
        cs = _cands(tail, valid)
        if len(cs) == 1:
            return cs.pop(), False
    cs = _cands(txt, valid)
    return (cs.pop(), False) if len(cs) == 1 else (None, bool(cs))


def g_mcq(task, tdir, rdir):
    gold = json.load(open(os.path.join(tdir, 'hidden', 'answer.json')))['answer'].strip().upper()
    n_opt = len(re.findall('^[A-J][.] ', task['prompt'], flags=re.M)) or 10
    valid = 'ABCDEFGHIJ'[:n_opt]
    txt = (read(os.path.join(rdir, 'ANSWER.txt'), '') or '').strip()
    got, amb = _mcq_pick(txt, valid)
    return result(got == gold, got=got, gold=gold, ambiguous=amb)


def g_exact_int(task, tdir, rdir):
    gold = json.load(open(os.path.join(tdir, 'hidden', 'answer.json')))['answer']
    raw = (read(os.path.join(rdir, 'ANSWER.txt'), '') or '').replace(',', '')
    # v1.0.1 审计修正：原来要求「整份答卷里只有一个整数」，于是 `答案是 342。（过程：500+734 得出）`
    # 会因为过程里的数判错。现在先看作答标志之后的第一个整数，没有标志才要求全篇唯一。
    picked = None
    low = raw.lower()
    pos = -1
    span = 0
    for mk in _MARK:
        k = low.find(mk.lower())
        if k >= 0 and (pos < 0 or k < pos):
            pos = k
            span = len(mk)
    if pos >= 0:
        after = re.findall('-?[0-9]+', raw[pos + span:])
        if after:
            picked = int(after[0])
    if picked is None:
        nums = {int(x) for x in re.findall('-?[0-9]+', raw)}
        picked = nums.pop() if len(nums) == 1 else None
    return result(picked == gold, got=picked, gold=gold)

# ------------------------------------------------------------ LiveCodeBench


def _run(cmd, cwd, inp=None, timeout=6):
    try:
        p = subprocess.run(cmd, cwd=cwd, input=inp, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)  # 显式 UTF-8：Windows 默认 GBK 解码会在中文输出上崩
        return p.returncode, p.stdout, p.stderr[-500:]
    except subprocess.TimeoutExpired:
        return -9, "", "TIMEOUT"


def _tok_eq(a, b):
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb):
        return False
    for x, y in zip(ta, tb):
        if x == y:
            continue
        try:
            fx, fy = float(x), float(y)
        except ValueError:
            return False
        if ("." not in x + y) or abs(fx - fy) > 1e-6 * max(1, abs(fy)):
            return False
    return True


def g_lcb_stdio(task, tdir, rdir):
    tests = json.load(open(os.path.join(tdir, "hidden", "tests.json")))
    sol = os.path.join(rdir, "solution.py")
    for i, t in enumerate(tests):
        rc, out, err = _run([PY, sol], rdir, t["input"])
        if rc != 0 or not _tok_eq(out, t["output"]):  # 按空白分词比较；小数允许 1e-6 误差
            return result(False, i / len(tests), failed_case=i, n=len(tests), err=err)
    return result(True, n=len(tests))


LCB_HARNESS = r'''
import json, sys
sys.setrecursionlimit(1 << 20)
sys.path.insert(0, ".")
from solution import *  # noqa
args = [json.loads(l) for l in sys.stdin.read().split("\n") if l.strip()]
res = getattr(Solution(), FUNC)(*args)
print(json.dumps(res))
'''


def g_lcb_functional(task, tdir, rdir):
    tests = json.load(open(os.path.join(tdir, "hidden", "tests.json")))
    h = os.path.join(rdir, "_lcb_harness.py")
    _force_write(h, "FUNC = %r\n" % task["func_name"] + LCB_HARNESS)  # 防软链注入
    try:
        for i, t in enumerate(tests):
            rc, out, err = _run([PY, h], rdir, t["input"])
            try:
                ok = rc == 0 and json.loads(out.strip().splitlines()[-1]) == json.loads(t["output"])
            except Exception:
                ok = False
            if not ok:
                return result(False, i / len(tests), failed_case=i, n=len(tests), err=err,
                              got=(out or "")[-200:], expected=str(t["output"])[:120])  # 答错要能定位
        return result(True, n=len(tests))
    finally:
        os.remove(h)

# ------------------------------------------------------------ Aider Polyglot


PY_CONF = ('conftest.py', 'pytest.ini', 'tox.ini', 'setup.cfg', 'pyproject.toml', 'pytest.py')
JS_CONF = ('jest.config.js', 'jest.config.cjs', 'jest.config.mjs', 'jest.config.ts')


def _force_write(path, text):
    """写测试文件前先拆掉软链：把 _lcb_harness.py / 测试文件做成软链，就能让判分器写到工作目录之外"""
    """（审计 P1-4 实测：受害文件被覆盖成 harness 源码）。"""
    try:
        if os.path.islink(path):
            os.unlink(path)
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _neutralize(rdir, tdir, names):
    """把「模型自己写进工作目录的测试配置」改名，再跑判分。

    为什么要：rdir 是模型的工作目录副本，pytest 会自动加载 rdir/conftest.py，jest 会自动读
    rdir/jest.config.*。一个 pytest_collection_modifyitems 钩子就能把用例全清掉、exit 0、判满分，
    而模型根本不需要「想作弊」—— 顺手生成一份 conftest.py 就可能改变判分（审计 P2-2）。
    只动「题目原本没有」的文件，题目自带的配置一律保留，避免把正常工程改坏。
    """
    base = set()
    wdir = os.path.join(tdir, 'workspace')
    if os.path.isdir(wdir):
        base = set(os.listdir(wdir))
    moved = []
    for n in names:
        if n in base:
            continue
        f = os.path.join(rdir, n)
        if os.path.exists(f):
            try:
                os.replace(f, f + '.agent-side')
                moved.append(n)
            except OSError:
                pass
    return moved


def _pytest_counts(text):
    """从汇总行取 (passed, failed)。rc==0 不足以判定：os._exit(0)、工作目录里的影子 pytest.py、"""
    """pyproject.toml 的 addopts="--co -q" 都能让它 0 退出却一个测试都没跑（审计 P0-1）。"""
    gp = re.search(r"([0-9]+) passed", text)
    gf = re.search(r"([0-9]+) failed", text)
    return (int(gp.group(1)) if gp else 0), (int(gf.group(1)) if gf else 0)


def g_polyglot_python(task, tdir, rdir):
    for t in task["test_files"]:
        _force_write(os.path.join(rdir, t),
                     open(os.path.join(tdir, "hidden", t), encoding="utf-8", errors="replace").read())
    moved = _neutralize(rdir, tdir, PY_CONF)
    rc, out, err = _run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *task["test_files"]],
                        rdir, timeout=180)
    txt = out + err
    n_ok, n_bad = _pytest_counts(txt)
    ok = rc == 0 and n_ok > 0 and n_bad == 0 and "no tests ran" not in txt
    return result(ok, tail=txt[-400:], neutralized=moved, passed=n_ok, failed=n_bad, rc=rc)

def _link_dir(src, dst):
    """把共享的 node_modules 挂进答题目录：POSIX 用软链接；Windows 用目录联接（mklink /J 不需要管理员权限）。"""
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", dst, src], capture_output=True)
    else:
        os.symlink(src, dst)
    if not os.path.exists(dst):  # 联接失败时退回复制（慢，但能用）
        shutil.copytree(src, dst, symlinks=True)


def _unlink_dir(p):
    try:
        if os.path.islink(p):
            os.unlink(p)
        else:
            os.rmdir(p)  # Windows 目录联接：rmdir 只删联接本身，不碰共享目录里的文件
    except OSError:
        shutil.rmtree(p, ignore_errors=True)  # 退回复制的情况：删掉这份拷贝


def js_cache(pkg_json):
    """所有 JS 题共用一份 jest/babel（约 300 MB），只在第一次判 JS 题时安装。"""
    cache = os.path.join(os.path.expanduser("~"), ".cache", "nodekit")  # 中性名字：工作目录里的 node_modules 链接会指向这里
    jest = os.path.join(cache, "node_modules", "jest", "bin", "jest.js")
    if not os.path.isfile(jest):
        os.makedirs(cache, exist_ok=True)
        shutil.copy2(pkg_json, os.path.join(cache, "package.json"))
        npm = shutil.which("npm") or "npm"
        try:  # v1.7.1：离线机器上 npm 会长时间重试，这里限 300 s 并给一句人能看懂的提示
            subprocess.run([npm, "install", "--prefer-offline", "--no-audit", "--no-fund", "--loglevel=error"],
                           cwd=cache, timeout=300)
        except subprocess.TimeoutExpired:
            raise RuntimeError("判分器缺 jest：首次判 JS 题要联网装一次，但 300 s 内没装完。"
                               "离线机器请先在有网环境跑一次判分，或设 SKIP_JS=1 跳过 7 道 JS 题")
        if not os.path.isfile(jest):
            raise RuntimeError("判分器缺 jest：npm install 没装成（离线机器请先在有网环境装一次，或 SKIP_JS=1 跳过 JS 题）")
    return cache, jest

def g_polyglot_javascript(task, tdir, rdir):
    cache, jest = js_cache(os.path.join(tdir, "workspace", "package.json"))
    nm = os.path.join(rdir, "node_modules"); linked = False
    if not os.path.exists(nm):  # agent 自己 npm install 过就用它的
        _link_dir(os.path.join(cache, "node_modules"), nm); linked = True
    for t in task["test_files"]:
        s = open(os.path.join(tdir, "hidden", t), encoding="utf-8").read()
        _force_write(os.path.join(rdir, t), re.sub(r"\bxtest\(", "test(", s))  # Exercism 用 xtest 跳过，判分时全跑
    # 报告名随机 + 先清掉同名前缀：否则 agent 自己摆一个 numFailedTests=0 的报告就能骗过判据
    for _stale in glob.glob(os.path.join(rdir, ".jest-report*.json")):
        try:
            os.unlink(_stale)
        except OSError:
            pass
    rep = os.path.join(rdir, ".jest-report-%s.json" % uuid.uuid4().hex[:8])
    try:  # 直接用 node 跑 jest 的入口脚本：不经过 npx.cmd / shell，Windows 与 POSIX 同一条命令
        moved = _neutralize(rdir, tdir, JS_CONF)
        rc, out, err = _run([shutil.which("node") or "node", jest, "--ci", "--rootDir", rdir,
                             "--json", "--outputFile", rep, *task["test_files"]], rdir, timeout=300)
    finally:
        if linked:
            _unlink_dir(nm)
    total = passed = failed = None
    try:  # process.exit(0) 会让 jest 提前收场：报告缺失/不完整必须判失败（审计 P0-1）
        j = json.load(open(rep, encoding="utf-8"))
        total, passed, failed = j.get("numTotalTests"), j.get("numPassedTests"), j.get("numFailedTests")
    except Exception:
        pass
    ok = (rc == 0 and isinstance(total, int) and total > 0
          and isinstance(passed, int) and passed > 0 and failed == 0)
    return result(ok, tail=(out + err)[-400:], neutralized=moved,
                  total=total, passed=passed, failed=failed, rc=rc)

# ------------------------------------------------------------ SpreadsheetBench


def _ranges(pos, default_sheet):
    out = []
    for part in re.split(r",(?=(?:[^']*'[^']*')*[^']*$)", pos):
        part = part.strip()
        if "!" in part:
            sh, rng = part.rsplit("!", 1); sh = sh.strip("'")
        else:
            sh, rng = default_sheet, part
        out.append((sh, rng.replace("$", "")))
    return out


def _norm(v):
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    if hasattr(v, "isoformat"):
        return v.isoformat()[:19]
    s = str(v).strip()
    try:
        return round(float(s), 2)
    except ValueError:
        return s


def _formula_without_cache(path, wb_values):
    import openpyxl
    wf = openpyxl.load_workbook(path)
    for ws in wf.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("=") and wb_values[ws.title][c.coordinate].value is None:
                    return True
    return False


def _recalc(path, ext):
    import openpyxl
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    tmp = tempfile.mkdtemp()
    subprocess.run([soffice, "--headless", "--calc", "--convert-to", ext, "--outdir", tmp, path], capture_output=True, timeout=180)
    p = os.path.join(tmp, os.path.basename(path))
    return openpyxl.load_workbook(p, data_only=True) if os.path.exists(p) else None


def g_ssb_cells(task, tdir, rdir):
    import openpyxl
    ext = task.get("ext", "xlsx")
    out = os.path.join(rdir, f"output.{ext}")
    if not os.path.exists(out):
        return result(False, reason="no output file")
    wg = openpyxl.load_workbook(os.path.join(tdir, "hidden", f"golden.{ext}"), data_only=True)
    wo = openpyxl.load_workbook(out, data_only=True)
    if _formula_without_cache(out, wo):  # 审计修正：公式没缓存值时，若装了 LibreOffice 就先重算再判
        wo = _recalc(out, ext) or wo
    bad, total = [], 0
    for sh, rng in _ranges(task["answer_position"], task.get("answer_sheet") or wg.sheetnames[0]):
        if sh not in wo.sheetnames:
            return result(False, reason=f"missing sheet {sh}")
        for rg, ro in zip(wg[sh][rng] if ":" in rng else ((wg[sh][rng],),), wo[sh][rng] if ":" in rng else ((wo[sh][rng],),)):
            for cg, co in zip(rg, ro):
                total += 1
                if _norm(cg.value) != _norm(co.value):
                    bad.append((sh, cg.coordinate, str(cg.value)[:30], str(co.value)[:30]))
    # 注：若 agent 写了公式但文件里没有缓存值，openpyxl 读到 None 会判错——题面已要求保存数值；
    #     需要时可先用 LibreOffice 无头模式重算：soffice --headless --convert-to xlsx output.xlsx
    # 只比答案区时，「交一个空工作簿」也能对上「应该有空格」的区域（审计 P0-2）：再要求非空格数没被整体清掉
    g_n = sum(1 for sh in wg.sheetnames for row in wg[sh].iter_rows() for c in row if _norm(c.value) != "")
    o_n = sum(1 for sh in wo.sheetnames for row in wo[sh].iter_rows() for c in row if _norm(c.value) != "")
    if g_n and o_n < g_n * 0.5:
        return result(False, reason=f"整表内容被清空（非空格 {o_n} / 应有 {g_n}）", cells=total, mismatches=bad[:5])
    return result(not bad, 1 - len(bad) / max(total, 1), mismatches=bad[:5], cells=total)

# ------------------------------------------------------------ 自建办公


def _exp(tdir):
    return json.load(open(os.path.join(tdir, "hidden", "expected.json"), encoding="utf-8"))


def _csv(p):
    t = read(p)
    return None if t is None else [r for r in csv.reader(t.splitlines()) if any(c.strip() for c in r)]


def g_csv_rows(task, tdir, rdir):
    e = _exp(tdir); rows = _csv(os.path.join(rdir, e["file"]))
    if not rows:
        return result(False, reason="missing/empty " + e["file"])
    hdr = [c.strip() for c in rows[0]]; body = [[c.strip() for c in r] for r in rows[1:]]
    exp = [[str(c) for c in r] for r in e["rows"]]
    def canon(r):
        return [f"{float(c):.2f}" if re.fullmatch(r"-?\d+\.\d+", c) else c for c in r]
    body, exp = [canon(r) for r in body], [canon(r) for r in exp]
    hit = sum(1 for r in exp if r in body) / max(len(exp), 1)
    ok = hdr == e["header"] and (body == exp if e.get("ordered") else sorted(body) == sorted(exp))
    return result(ok, hit if not ok else 1, header_ok=hdr == e["header"], n_exp=len(exp), n_got=len(body))


def g_organize(task, tdir, rdir):
    e = _exp(tdir); root = os.path.join(rdir, e["root"]); got = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            got[os.path.relpath(os.path.join(dp, f), root).replace(os.sep, "/")] = read(os.path.join(dp, f))
    hit = sum(got.get(k) == v for k, v in e["files"].items())
    return result(got == e["files"], hit / len(e["files"]), missing=[k for k in e["files"] if k not in got][:5])


def g_minutes(task, tdir, rdir):
    e = _exp(tdir)
    try:
        items = json.loads(read(os.path.join(rdir, e["file"])))
        got = sorted([str(i["owner"]).strip(), str(i["due"]).strip()] for i in items)
    except Exception as ex:
        return result(False, reason=str(ex)[:100])
    hit = sum(p in got for p in e["pairs"]) / len(e["pairs"])
    return result(got == e["pairs"], hit, got=len(got), exp=len(e["pairs"]))


def g_config(task, tdir, rdir):
    import configparser
    e = _exp(tdir); cp = configparser.ConfigParser()
    try:
        cp.read_string(read(os.path.join(rdir, e["file"])) or "")  # 与其它判分一致：容忍 GBK/UTF-16
    except Exception as ex:
        return result(False, reason=str(ex)[:100])
    got = {s: {k: v.strip().lower() if k == "debug" else v.strip() for k, v in cp[s].items()} for s in cp.sections()}
    keys = [(s, k) for s in e["ini"] for k in e["ini"][s]]
    hit = sum(got.get(s, {}).get(k) == e["ini"][s][k] for s, k in keys) / len(keys)
    return result(got == e["ini"], hit)


def g_budget(task, tdir, rdir):
    import openpyxl
    e = _exp(tdir)
    try:
        ws = openpyxl.load_workbook(os.path.join(rdir, e["file"]), data_only=True)[e["sheet"]]
    except Exception as ex:
        return result(False, reason=str(ex)[:100])
    tot = {(c, m): v for c, m, v in e["tot"]}; errs, n = [], 0
    def chk(r, c, want):
        nonlocal n; n += 1; v = ws.cell(r, c).value
        if not isinstance(v, (int, float)) or abs(v - want) > 0.011:
            errs.append((r, c, v, round(want, 2)))
    for i, cat in enumerate(e["cats"]):
        if ws.cell(i + 2, 1).value != cat:
            errs.append((i + 2, 1, ws.cell(i + 2, 1).value, cat))
        for m in range(1, 7):
            chk(i + 2, m + 1, tot.get((cat, m), 0))
        chk(i + 2, 8, sum(tot.get((cat, m), 0) for m in range(1, 7)))
    last = len(e["cats"]) + 2
    for m in range(1, 7):
        chk(last, m + 1, sum(tot.get((c, m), 0) for c in e["cats"]))
    chk(last, 8, sum(tot.values()))
    return result(not errs, 1 - len(errs) / n, errs=errs[:5])


def g_json_eq(task, tdir, rdir):
    e = _exp(tdir)
    try:
        got = json.loads(read(os.path.join(rdir, e["file"])))
    except Exception as ex:
        return result(False, reason=str(ex)[:100])
    keys = list(e["json"]); hit = sum(got.get(k) == e["json"][k] for k in keys) / len(keys)
    return result(hit == 1, hit, got=got)


def g_units(task, tdir, rdir):
    e = _exp(tdir); rows = _csv(os.path.join(rdir, e["file"]))
    if not rows:
        return result(False, reason="missing")
    ok_hdr = [c.strip() for c in rows[0]] == ["sku", "weight_kg", "price_per_kg"]; good = 0
    for want, got in zip(e["units"], rows[1:]):
        try:
            good += got[0].strip() == want[0] and abs(float(got[1]) - want[1]) <= 0.0011 and abs(float(got[2]) - want[2]) <= 0.011
        except Exception:
            pass
    return result(ok_hdr and good == len(e["units"]) == len(rows) - 1, good / len(e["units"]))


def g_lines(task, tdir, rdir):
    e = _exp(tdir); t = read(os.path.join(rdir, e["file"]))
    got = [l.strip().replace(" ", "") for l in (t or "").splitlines() if l.strip()]
    hit = sum(l in got for l in e["lines"]) / max(len(e["lines"]), 1)
    return result(got == e["lines"], hit if got != e["lines"] else 1, n_exp=len(e["lines"]), n_got=len(got))

# ------------------------------------------------------------ 自建长链路


def g_chain(task, tdir, rdir):
    e = _exp(tdir); checks = {}
    nd = os.path.join(rdir, "notes")
    got = {f: (read(os.path.join(nd, f)) or "") for f in os.listdir(nd) if not f.startswith(".")} if os.path.isdir(nd) else {}
    checks["notes"] = {k: v.strip() for k, v in got.items()} == e["notes"]
    texts = list(got.values()) + [read(os.path.join(rdir, f)) or "" for f in ("REPORT.md", "progress.log", "table.csv", "config.json")]
    checks["newline_end"] = all(v.endswith("\n") for v in texts if v)
    try:
        checks["config"] = json.loads(read(os.path.join(rdir, "config.json"))) == e["config"]
    except Exception:
        checks["config"] = False
    tb = _csv(os.path.join(rdir, "table.csv")) or []
    checks["table"] = [[c.strip() for c in r] for r in tb] == e["table"]
    rep = [l.strip() for l in (read(os.path.join(rdir, "REPORT.md"), "") or "").splitlines() if l.strip()]
    checks["passcode_first_line"] = bool(rep) and rep[0] == e["report"][0]
    checks["report_recall"] = rep[1:] == e["report"][1:]
    prog = [l.strip().strip("`") for l in (read(os.path.join(rdir, "progress.log"), "") or "").splitlines() if l.strip()]
    checks["progress_log"] = prog == e["progress"]
    checks["vendor_untouched"] = all(os.path.exists(os.path.join(rdir, p)) and
                                     hashlib.sha256(open(os.path.join(rdir, p), "rb").read()).hexdigest() == h
                                     for p, h in e["vendor"].items())
    frac = sum(checks.values()) / len(checks)
    return result(all(checks.values()), frac, **checks)


GRADERS = {"mcq": g_mcq, "exact_int": g_exact_int, "lcb_stdio": g_lcb_stdio, "lcb_functional": g_lcb_functional,
           "polyglot_python": g_polyglot_python, "polyglot_javascript": g_polyglot_javascript, "ssb_cells": g_ssb_cells,
           "self_o_region": g_csv_rows, "self_o_invoices": g_csv_rows, "self_o_dedupe": g_csv_rows,
           "self_o_organize": g_organize, "self_o_minutes": g_minutes, "self_o_config": g_config,
           "self_o_budget": g_budget, "self_o_logs": g_json_eq, "self_o_units": g_units,
           "self_o_calendar": g_lines, "self_chain": g_chain}


def grade(tdir, rdir):
    import answers  # 答案平时打包混淆存放（见 answers.py），判分时才临时解出这一题
    with answers.view(tdir) as v:
        task = json.load(open(os.path.join(v, "task.json"), encoding="utf-8"))
        try:
            return GRADERS[task["grader"]](task, v, rdir)
        except Exception as ex:  # 判分器异常记为错，并留下原因，便于发现“坏题”
            return result(False, grader_error=repr(ex)[:300])


if __name__ == "__main__":
    print(json.dumps(grade(sys.argv[1], sys.argv[2]), ensure_ascii=False))
