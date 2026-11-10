#!/usr/bin/env python3
"""DSH-Bench 一键入口。所有步骤都从这里跑，按顺序：

    python bench/bench.py doctor      # 1. 体检：Python 包、Node、pytest、dsh 版本、API Key、端口
    python bench/bench.py setup       # 2. 建五个 profile、装插件、预设保真 + 金丝雀 + 启动探针
    python bench/bench.py smoke       # 3. 冒烟：每个臂跑 2 道小题，确认记账、判分、模型版本都对（几分钟、几分钱）
    python bench/bench.py pre         # 4. 预跑：标准臂把候选池跑 2 遍（选题用）
    python bench/bench.py calibrate   # 5. 选题：剔除坏题/全错题、候补补位，并按真实数据重算重复次数
    python bench/bench.py stage1      # 6. 正式第一段：两臂各跑 3 遍（并行）
    python bench/bench.py interim     # 7. 期中看一眼：99% 区间已能下结论就停，否则跑 stage2
    python bench/bench.py stage2      # 8. 第二段：两臂补到 6 遍（仍不显著再改 rep_to=10）
    python bench/bench.py report      # 9. 出报告 report.md + 评分卡 scorecard.png
其它：leakcheck（泄漏自检：扫描所有会交给模型的内容）、status（进度）、gpqa（下载并生成 GPQA 题，需要 HF_TOKEN）、manifest（按题目目录重建题单）。

所有命令都可以中断后重跑：已经跑过的 (臂, 题, 第几次) 自动跳过。
"""
import argparse, atexit, collections, csv, glob, json, os, platform, re, shutil, signal, socket, subprocess, sys, threading, time

for _s in (sys.stdout, sys.stderr):  # Windows 控制台默认 GBK：打印 ✔ 之类字符时不崩，最多显示成 ?
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import grade, isolation, leakcheck, run_bench, usage_proxy  # noqa: E402

TASKS = os.path.join(ROOT, "tasks")
CONFIG = os.path.join(ROOT, "config.json")
OK, BAD, WARN = "✔", "✘", "!"


def cfg_load():
    if not os.path.exists(CONFIG):
        shutil.copy(os.path.join(ROOT, "config.example.json"), CONFIG)
        print(f"{WARN} 没有 config.json，已从 config.example.json 复制一份（默认值通常就能用）。")
    return run_bench.load_cfg(CONFIG)


def active_arms(cfg, only=None):
    arms = [a for a, v in cfg["arms"].items() if v.get("enabled", True)]
    return [a for a in arms if not only or a in only]


def sh(cmd, timeout=600, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as ex:
        return 127, "", str(ex)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def port_free(port):
    with socket.socket() as s:
        if os.name == "nt":  # v1.5：Windows 的 SO_REUSEADDR 会允许“抢占”别人正在监听的端口，检测会误报空闲；改用独占绑定
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # 刚跑完留下的 TIME_WAIT 不该算占用
        try:
            s.bind(("127.0.0.1", port)); return True
        except OSError:
            return False


# ---------------------------------------------------------------- 1. doctor
def cmd_doctor(a):
    cfg = cfg_load(); bad = 0

    def row(ok, what, hint=""):
        nonlocal bad
        mark = OK if ok is True else (WARN if ok is None else BAD)
        bad += ok is False
        print(f" {mark} {what}" + (f"\n     → {hint}" if hint and ok is not True else ""))

    print("== 环境体检 ==")
    # v1.0.0：把基准自身的版本打出来，报告里对不上版本时一眼能看出来。
    try:
        _ver = open(os.path.join(HERE, "..", "VERSION"), encoding="utf-8").read().strip()
    except OSError:
        _ver = "unknown"
    print(f" DSH-Bench 版本：{_ver}")
    row(sys.version_info >= (3, 9), f"Python {platform.python_version()}", "需要 Python 3.9 或更新")
    # v1.8：平台是实验条件。桌面端（Windows）与 WSL 的编程题得分常有差距，主要来自 agent 手边的工具链
    # （有没有 python/pytest/node/git、默认 shell、默认编码），所以这里用“清洗后的评测环境”实际探一遍。
    host = isolation.host_platform(cfg)
    row(True, f"运行平台：{host}（写进每条结果的 host 字段；不同平台的结果请分开出报告：report --platform {host}）")
    try:
        env = isolation.clean_env(cfg, isolation.dsh_home(cfg), leakcheck.RX)
        for name, cmd in (("python", ["python", "--version"]), ("pytest", ["python", "-m", "pytest", "--version"]),
                          ("node", ["node", "--version"]), ("git", ["git", "--version"])):
            exe = shutil.which(cmd[0], path=env.get("PATH") or env.get("Path"))
            okv = False
            if exe:
                try:
                    okv = subprocess.run([exe] + cmd[1:], env=env, capture_output=True, timeout=30).returncode == 0
                except Exception:
                    okv = False
            row(okv or None, f"被测 agent 能用 {name}（评测环境的 PATH 里）",
                "agent 自己跑测试要用到；缺了编程题会系统性偏低。各臂一样缺，所以不影响公平，但会压低区分度，并让平台之间不可比")
    except Exception as ex:
        row(None, f"没能探测 agent 工具链：{ex!r}"[:120])
    for mod, why in (("openpyxl", "表格题判分"), ("pytest", "Python 编程题判分"), ("matplotlib", "评分卡画图（可选）")):
        try:
            __import__(mod); row(True, f"Python 包 {mod}")
        except ImportError:
            row(None if mod == "matplotlib" else False, f"缺 Python 包 {mod}（{why}）", "pip install -r requirements.txt")
    rc, out, _ = sh([shutil.which("node") or "node", "--version"])
    row(rc == 0, f"Node.js {out.strip()}", "安装 Node.js 20+（JS 编程题判分、DSH 本身都需要）")
    row(shutil.which("npm") is not None, "npm", "随 Node.js 一起安装")
    try:
        dsh = run_bench.resolve_dsh(cfg); rc, out, err = sh(dsh + ["--version"], timeout=120, env=locals().get("env"))
        ver = (out or err).strip().splitlines()[-1] if (out or err).strip() else "?"
        row(rc == 0, f"dsh {ver}  （命令：{' '.join(dsh)}）", "npm i -g @deepseek-ai/dsh")
        pin = cfg.get("dsh_version_pin", "")
        if pin and not pin.startswith("填") and pin not in ver:
            row(False, f"dsh 版本与 config 里锁定的 {pin} 不一致", "各臂必须用同一个 DSH 版本；要么升级/降级，要么改 dsh_version_pin")
    except SystemExit as ex:
        row(False, "dsh", str(ex))
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY")) or any(os.path.exists(p) for p in (
        os.path.join(os.path.expanduser(os.environ.get("DSH_HOME", "~/.dsh")), ".credentials.yaml"),
        os.path.join(os.path.expanduser(os.environ.get("DSH_HOME", "~/.dsh")), ".env")))
    row(True if has_key else None, "DeepSeek API Key（环境变量或 ~/.dsh/.credentials.yaml / .env）",
        "没找到也可能存在网页设置里；smoke 步骤会实际验证。设置方法：环境变量 DEEPSEEK_API_KEY=sk-...")
    soff = shutil.which("soffice") or shutil.which("libreoffice")
    row(True if soff else None, "LibreOffice（可选：表格题里 agent 只写公式没存结果时用来重算）", "不装也能跑；题面已要求写数值")
    ports = [v["proxy_port"] for v in cfg["arms"].values()]
    busy = [p for p in ports if not port_free(p)]
    row(not busy, f"记账代理端口 {ports} 空闲", f"被占用：{busy}，改 config.json 里的 proxy_port")
    iso = cfg.get("isolation", "env")
    if iso == "docker":
        rc, out, err = sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=60)
        row(rc == 0, f"隔离模式 docker（Docker {out.strip() or '?'}）", "安装并启动 Docker Desktop / Docker Engine；或把 isolation 改回 env")
    else:
        row(None, "隔离模式 env（软隔离 + 断网变量）。更严格：装 Docker 后把 config.json 的 isolation 改成 docker", "")
    # v1.7.6：工作目录的绝对路径也在启动前泄漏自检范围内；路径里带测试词会被拦下（曾经的 8/8 leak_blocked）
    # v1.9.1：覆盖层的绝对路径会原样出现在 dsh 的启动命令行里，包目录名一旦带测试关键词，
    # 每次运行都会被启动前自检拦成 leak_blocked（doctor 原来只查三个运行目录，没查包本身）。
    rootbad = []
    for p in (ROOT, os.path.join(ROOT, "overlays")):
        m = leakcheck.RX.search(os.path.abspath(p))
        if m:
            rootbad.append(f"{p}（命中“{m.group(0)}”）")
    # 包目录名（dsh-bench/ 之类）命中测试关键词是常态，而 0.2.2-rc.1 的 CLI 并不会因此拦运行
    # （实测在 dsh-bench-in/ 目录里 smoke 照常跑通）。所以只提示、不判失败；真正会拦的是下面的运行目录。
    row(None if rootbad else True, "测试包自身的绝对路径不含测试关键词",
        "覆盖层路径会进启动命令行，被启动前泄漏自检拦成 leak_blocked（每次运行都跑不成）：" + "；".join(rootbad) +
        "。把整个包解压到中性路径，例如 D:\\E1\\pkg（避开 bench / ops / presets / arms 等词）")
    badpaths = []
    for name in ("work_root", "dsh_home", "state_root"):
        v = cfg.get(name)
        if v and leakcheck.RX.search(os.path.abspath(os.path.expanduser(str(v)))):
            badpaths.append(f"{name}={v}")
    row(not badpaths, "运行目录不含测试关键词（work_root / dsh_home / state_root / 包目录）",
        "这些运行目录的路径命中了测试关键词，每次运行都会被启动前泄漏自检拦成 leak_blocked：" + "；".join(badpaths) +
        "。改成中性路径（例如 /home/<you>/smk/work），不要把运行目录设在 dsh-bench-* 下面")
    plain = glob.glob(os.path.join(TASKS, "*", "hidden"))   # v1.7.6：明文答案不能随包发出去
    hint = "" if not plain else (
        f"发现 {len(plain)} 个明文 hidden/ 目录（例如 {os.path.relpath(plain[0], ROOT)}）："
        "说明跑过 answers.unpack 但没 pack 回去，答案会随包泄露。修：python bench/answers.py pack tasks")
    row(not plain, "答案以打包形式存放（tasks/_answers.bin）", hint)
    n = len(run_bench.load_tasks(TASKS, "pool"))
    row(n > 0 and os.path.exists(os.path.join(TASKS, "_answers.bin")), f"题目目录：候选池 {n} 题，答案包就位")
    gp = glob.glob(os.path.join(TASKS, "know-gpqa-*"))
    row(True if gp else None, f"GPQA 题 {len(gp)} 道", "可选：设置 HF_TOKEN 后运行 python bench/bench.py gpqa（数据集需先在 HF 网页同意条款）")
    print("\n" + ("体检通过。下一步：python bench/bench.py setup" if not bad else f"有 {bad} 项必须先解决。"))
    return 1 if bad else 0


# ---------------------------------------------------------------- 2. setup
def profiles_of(cfg):
    """profile → 插件列表。profile 名是 setup 随机生成的中性名字（p + 6 位十六进制），映射存在 runs/_setup/profiles.json。"""
    m = isolation.assign_profiles(cfg); out = {}
    for arm in active_arms(cfg):
        prof = cfg["arms"][arm].get("profile") or m[arm]
        prev = out.setdefault(prof, cfg["arms"][arm].get("plugins", []))
        if prev != cfg["arms"][arm].get("plugins", []):
            raise SystemExit(f"profile {prof} 被两个臂用但插件不同，请改名")
    return out


def dump_config(cfg, arm, sdir):
    """合成配置（不启动）：用和正式跑完全相同的覆盖层组合。"""
    docker = cfg.get("isolation", "env") == "docker"; port = cfg["arms"][arm]["proxy_port"]
    prof = isolation.profile_of(cfg, arm)
    if docker:
        patches = isolation.materialize_patches(cfg, arm, sdir, f"http://px{port}:8080/anthropic", isolation.C_STATE + "/s", isolation.C_STATE + "/t")
        env = isolation.clean_env(cfg, isolation.C_HOME, leakcheck.RX)
        cmd = isolation.docker_cmd(cfg, "dump-" + arm, prof, sdir, sdir, patches, env, "x")[:-2] + ["--dump-config"]
        return sh(cmd, timeout=300)
    patches = isolation.materialize_patches(cfg, arm, sdir, f"http://127.0.0.1:{port}/anthropic", os.path.join(sdir, "s"), os.path.join(sdir, "t"))
    cmd = run_bench.resolve_dsh(cfg) + ["--profile", prof] + sum((["--patch", p] for p in patches), []) + ["--dump-config"]
    return sh(cmd, timeout=300, env=isolation.clean_env(cfg, isolation.dsh_home(cfg), leakcheck.RX))


def row_block(text, rid):
    """从 --dump-config 文本里截出某一行的块（到下一个 "- id: " 为止）。"""
    lines = text.splitlines(); start = None
    for i, ln in enumerate(lines):
        if ln.strip() == f"- id: {rid}":
            start = i; break
    if start is None:
        return None
    out = [lines[start].strip()]
    for ln in lines[start + 1:]:
        if ln.strip().startswith("- id: "):
            break
        if ln.strip() and not ln.startswith(" "):
            break
        out.append(ln.rstrip())
    return "\n".join(out)


def ptc_on(text):
    """合成配置里工具面到底是不是 PTC。
    dsh 0.2.2-rc.1：headless base 有 tool-executor-native（默认开）和 tool-executor-ptc（默认 disabled）两行，
    开 PTC = 关原生、开 PTC；Web 侧预设走的是 tool-presentation 行（headless 里没有这一行），两种都认。
    v1.9.1：tools.config.mode: ptc 在 0.2.2-rc.1 已失效（启动即 ValidationError），不能再当判据。"""
    tp = row_block(text, "tool-presentation")
    if tp and re.search(r"^\s+mode:\s*ptc\s*$", tp, re.M):
        return True
    nat, ptc = row_block(text, "tool-executor-native"), row_block(text, "tool-executor-ptc")
    return bool(nat and ptc and "disabled: true" in nat and "disabled: false" in ptc)


MARKERS = ("FLASH OPERATING RULES", "CORE RULES")


def canary(cfg, arm, text, profile_pkg=""):
    """合成配置金丝雀：确认每个臂真的是我们以为的那个样子（插件仓库踩过“覆盖层被静默忽略、两臂一模一样”的坑）。

    v1.9.1：除了“有没有规则 / 有没有 PTC / 有没有插件”，还要查**参数指纹**——预设之间的差别有一半在压缩阈值、
    结果裁剪、搜索抽样这类参数上；只查规则文本会把“只对齐了规则”的臂当成对齐。
    expect.marker + expect.rules 定规则；expect.mustHave / expect.mustNot 逐行（row id → 必需/禁止出现的子串）。"""
    v = cfg["arms"][arm]; ex = v.get("expect", {}); checks = []
    marker = ex.get("marker"); want_rules = ex.get("rules", 0)
    seen = {m: (m in text) for m in MARKERS}
    n_rules = len(re.findall(r"^\s+[0-9]+\. ", text, flags=re.M))
    if want_rules:
        ok = bool(marker) and seen.get(marker, False) and n_rules == want_rules
        checks.append((ok, f"规则「{marker}」应有 {want_rules} 条（实际 {n_rules} 条，"
                           f"{'命中' if marker and seen.get(marker) else '未命中'}）"))
    else:
        hit = [m for m in MARKERS if seen[m]]
        checks.append((not hit, "不应有任何 persona 规则（实际命中：" + (", ".join(hit) or "无") + "）"))
    if "ptc" in ex:
        checks.append((ptc_on(text) == bool(ex["ptc"]), "工具面是 PTC（关原生执行器、开 PTC 执行器）"
                       if ex["ptc"] else "工具面不是 PTC（原生执行器）"))
    for rid, needle in (ex.get("mustHave") or {}).items():
        b = row_block(text, rid)
        checks.append((bool(b) and needle in b, f"{rid} 应有「{needle}」"))
    for rid, needle in (ex.get("mustNot") or {}).items():
        # mustNot 是「这一行不该带这个参数」：行本身不存在 = 这个参数确实没配，合规。
        # 「整行被漏掉」由 mustHave 与 preset_fidelity 兜（它们 fail-closed）；在这里判失败会误伤
        # 精简配置（演练实测：mock 的合成配置里没有 compaction-basic 行）。
        checks.append((b is None or needle not in b, f"{rid} 不应有「{needle}」"
                        + ("" if b is not None else "（该行不存在）")))
    port = v["proxy_port"]
    checks.append((f"127.0.0.1:{port}" in text or f"px{port}:8080" in text, f"模型请求指向记账代理 :{port}"))
    return checks


PROBE_BAD = ("invalid config", "startup failed", "did not activate", "patch: entry", "not found")


def boot_probe(cfg, arm, sdir):
    """启动探针（v1.9.1）：真的把这一臂的覆盖层组合拉起一次，只让它走到“要连模型”那一步就失败。

    为什么需要它：--dump-config 只打印合成树，**不校验配置取值**。dsh 0.2.2-rc.1 对 tools.config.mode: ptc
    这种失效键是在启动时才抛 ValidationError（退出码 1），金丝雀照样全 ✔ —— 冒烟前发现不了，
    等于白跑。探针把 baseURL 指向没有服务在听的端口，因此不会真的调用模型、不花钱。
    返回 (ok, 说明)。"""
    port = cfg["arms"][arm]["proxy_port"]
    docker = cfg.get("isolation", "env") == "docker"
    base = isolation.base_url(cfg, port, docker)
    patches = isolation.materialize_patches(cfg, arm, sdir, base, os.path.join(sdir, "s"), os.path.join(sdir, "t"))
    cmd = run_bench.resolve_dsh(cfg) + ["--profile", isolation.profile_of(cfg, arm)] + \
        sum((["--patch", p] for p in patches), []) + ["--json", "ok"]
    rc, out, err = sh(cmd, timeout=180, env=isolation.clean_env(cfg, isolation.dsh_home(cfg, docker), leakcheck.RX))
    blob = (err or "") + "\n" + (out or "")
    bad = [k for k in PROBE_BAD if k in blob]
    if bad:
        first = next((l.strip() for l in blob.splitlines() if any(k in l for k in bad)), "")
        return False, f"启动失败（{', '.join(bad)}）：{first[:300]}"
    return True, "启动到模型调用前正常（配置合法）"


def copy_credentials(home):
    """专用 DSH_HOME：把你平时 DSH 里保存的 API Key 文件复制过来（只复制凭据，不复制会话、技能、设置）。"""
    src = os.path.expanduser(os.environ.get("DSH_HOME", "~/.dsh"))
    for f in (".credentials.yaml", ".env"):
        if os.path.exists(os.path.join(src, f)) and not os.path.exists(os.path.join(home, f)):
            shutil.copy2(os.path.join(src, f), os.path.join(home, f))


def cmd_setup(a):
    cfg = cfg_load(); out = os.path.join(ROOT, "runs", "_setup"); os.makedirs(out, exist_ok=True)
    docker = cfg.get("isolation", "env") == "docker"; info = {"isolation": "docker" if docker else "env", "profiles": {}, "arms": {}}
    home = isolation.dsh_home(cfg, docker); os.makedirs(home, exist_ok=True)
    print(f"隔离模式：{info['isolation']}；专用 DSH_HOME：{home}")
    if docker:
        print("== 构建镜像、创建 internal 网络（第一次要几分钟）"); isolation.docker_build(cfg)
        rc, ver, err = sh(["docker", "run", "--rm", isolation.IMAGE, "dsh", "--version"])
    else:
        copy_credentials(home); dsh = run_bench.resolve_dsh(cfg)
        env = isolation.clean_env(cfg, home, leakcheck.RX)
        for k in isolation.NET_BLOCK_ENV:  # 建 profile、装插件需要联网：这一步恢复你原来的网络设置
            env.pop(k, None)
            if k in os.environ:
                env[k] = os.environ[k]
        rc, ver, err = sh(dsh + ["--version"], env=env)   # v1.9.1：走同一份清洗后环境（含 config.env，例如 ELECTRON_RUN_AS_NODE）
    info["dsh_version"] = (ver or err).strip(); print(f"DSH {info['dsh_version']}")
    for prof, plugins in profiles_of(cfg).items():
        print(f"== profile {prof}：从 headless 模板新建" + (f"，安装 {', '.join(plugins)}" if plugins else ""))
        if docker:
            p = isolation.docker_setup_profile(cfg, prof, plugins)
            if p.returncode != 0:
                print(f" ✘ {(p.stderr or p.stdout)[-800:]}"); return 1
        else:
            rc, o, e = sh(dsh + ["--profile", prof, "--from-default-profile", "headless", "--dump-config"], timeout=600, env=env)
            if rc != 0:
                print(f" {BAD} 新建失败：{(e or o)[-800:]}"); return 1
            for pl in plugins:
                rc, o, e = sh(dsh + ["plugin", "--profile", prof, "add", pl], timeout=1200, env=env)
                print(f" {OK if rc == 0 else BAD} 安装 {pl}" + ("" if rc == 0 else f"\n{(e or o)[-800:]}"))
                if rc != 0:
                    return 1
        pj = os.path.join(home, "profiles", prof, "package.json")
        info["profiles"][prof] = json.load(open(pj, encoding="utf-8")) if os.path.exists(pj) else None
    has_js = any(t["grader"] == "polyglot_javascript" for t in run_bench.load_tasks(TASKS, "pool"))
    if not docker and has_js and not os.environ.get("DSHBENCH_SKIP_JS_CACHE"):
        print("== 预装 JS 测试工具（断网跑题时给模型自测用，判分也用它）")
        grade.js_cache(os.path.join(HERE, "..", "docker", "nodekit-package.json"))
    elif not has_js:
        print("== 题包里没有 JS 题，跳过 JS 测试工具预装")
    print("== 合成配置金丝雀")
    allok = True
    for arm in active_arms(cfg):
        sdir = os.path.join(isolation.state_root(cfg), "setup-" + isolation.new_id())
        rc, o, e = dump_config(cfg, arm, sdir); shutil.rmtree(sdir, ignore_errors=True)
        open(os.path.join(out, f"{arm}.dump.txt"), "w", encoding="utf-8").write(o + "\n--- stderr ---\n" + e)
        if "patch: entry" in e:   # v1.7.6：静默丢弃 = 该臂其实没被测到，必须当场失败
            miss = sorted(set(re.findall(r'patch: entry "([^"]+)" not found', e)))
            print(f" {BAD} {arm}: 覆盖层里有 dsh 不认识的插件 id：{', '.join(miss)}（这些条目被丢弃了）")
            print("     → 覆盖层与当前 dsh 版本不匹配，详见 FIXES.md 的 P5；改好再跑，否则该臂的数据无效")
            allok = False
        if rc != 0:
            print(f" {BAD} {arm}: --dump-config 失败（覆盖层可能有当前 DSH 不认识的行）：{(e or o)[-600:]}"); allok = False; continue
        res = canary(cfg, arm, o, json.dumps(info["profiles"].get(isolation.profile_of(cfg, arm)) or {}))
        # v1.9.1：预设保真检查——该臂自称对齐的 agent preset，与实际合成配置逐行比对（漏行=这一臂名不副实）
        preset = cfg["arms"][arm].get("preset")
        if preset and not docker:
            fid_out = os.path.join(out, f"fidelity_{arm}.txt")
            rc2, o2, e2 = sh([sys.executable, os.path.join(HERE, "preset_fidelity.py"), "--arm", arm,
                              "--preset", preset, "--dump", os.path.join(out, f"{arm}.dump.txt"),
                              "--out", fid_out], timeout=300, env=env)
            info.setdefault("fidelity", {})[arm] = {"preset": preset, "rc": rc2, "file": os.path.relpath(fid_out, ROOT)}
            mm = re.search(r"MISMATCH[^：]*：\s*(\d+)", o2 or "")
            nmis = int(mm.group(1)) if mm else 0
            res.append((rc2 == 0, f"保真检查（preset {preset}）：" + ("与合成配置一致" if rc2 == 0 else
                       f"有 {nmis} 处 mismatch → {os.path.relpath(fid_out, ROOT)}")))
        elif preset and docker:
            print(f" {WARN} {arm}: docker 模式下跳过保真检查（需要现网 profile 的预设 dump）")
        sdir2 = os.path.join(isolation.state_root(cfg), "probe-" + isolation.new_id())
        try:
            pok, pmsg = boot_probe(cfg, arm, sdir2)
        finally:
            shutil.rmtree(sdir2, ignore_errors=True)
        res.append((pok, "启动探针：" + pmsg)); info["arms"][arm] = res
        for ok, msg in res:
            print(f" {OK if ok else BAD} {arm}: {msg}"); allok &= ok
    print("== 泄漏自检（全部题目的任务文本与工作目录）")
    rc = subprocess.call([sys.executable, os.path.join(HERE, "leakcheck.py"), "--tasks", TASKS])
    allok &= rc == 0
    json.dump(info, open(os.path.join(out, "setup.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("\n" + ("设置完成。下一步：python bench/bench.py smoke" if allok else "有 ✘：先别跑正式评测，把上面的输出发给我。"))
    return 0 if allok else 1


# ---------------------------------------------------------------- 上游健康度（v1.9.2）
def upstream_key(cfg):
    docker = cfg.get("isolation", "env") == "docker"
    try:
        cred = isolation.load_credentials(isolation.dsh_home(cfg, docker)) or {}
    except Exception:
        cred = {}
    return cred.get("WB_PROXY_API_KEY") or os.environ.get("WB_PROXY_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")


def probe_upstream(cfg, timeout=90):
    """打一发 max_tokens=1 的探测请求：只关心“上游能不能出 token”。
    不用 DSH 自身探测——启动一次要几秒、还会写会话与状态；这里只要一个 HTTP 响应码。"""
    import urllib.error, urllib.request
    up = (cfg.get("upstream") or "https://api.deepseek.com").rstrip("/")
    # 路线要和正式跑完全一致：wb 路线是 OpenAI 兼容（isolation.materialize_patches 里 api: openai-completions、
    # baseURL: <upstream>/v1），所以探测请求打 /v1/chat/completions 而不是 Anthropic 的 /anthropic/v1/messages。
    if (cfg.get("llm_provider") or "deepseek") == "wb":
        url = up + "/v1/chat/completions"
        body = json.dumps({"model": cfg.get("model") or "deepseek-v4.1-flash", "max_tokens": 1, "stream": False,
                           "messages": [{"role": "user", "content": "ping"}]}).encode()
        headers = {"content-type": "application/json", "authorization": "Bearer " + (upstream_key(cfg) or "")}
    else:
        url = up + "/anthropic/v1/messages"
        body = json.dumps({"model": cfg.get("model") or "deepseek-chat", "max_tokens": 1,
                           "messages": [{"role": "user", "content": "ping"}]}).encode()
        headers = {"content-type": "application/json", "anthropic-version": "2023-06-01",
                   "x-api-key": upstream_key(cfg) or ""}
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机地址不能被系统代理劫走
    try:
        with opener.open(req, timeout=timeout) as r:
            return True, getattr(r, "status", 200), ""
    except urllib.error.HTTPError as ex:
        try:
            msg = ex.read()[:300].decode("utf-8", "replace")
        except Exception:
            msg = ""
        return False, ex.code, msg
    except Exception as ex:
        return False, None, repr(ex)[:200]


def health_gate(cfg):
    """开跑前健康闸门：连续 provider_health_probes 次探测都通才放行。
    不通就**不启动**——2026-11-07 的教训是，上游账户池打空时开跑只会把数据烧成失败样本。"""
    need = max(1, int(cfg.get("provider_health_probes", 3))); streak = 0; last = None
    for _ in range(need * 6):
        ok, code, msg = probe_upstream(cfg)
        if ok:
            streak += 1
            if streak >= need:
                print(f" {OK} 上游健康闸门通过（连续 {need} 次 2xx）")
                return True
        else:
            streak = 0; last = f"{code} {msg[:160]}"
            print(f" {WARN} 上游探测失败：{last}")
        time.sleep(2 if ok else 10)
    print(f" {BAD} 上游健康闸门未通过（最后一次：{last}）。这一轮不启动：上游不可用时开跑只会烧数据。")
    return False


class Gate:
    """上游熔断 + 全局并发上限（v1.9.2）。

    2026-11-07 事故：wb-proxy 账户池被打空，9 槽满速连跑约 10 分钟，把 990 次里的 844 次烧成 exit1。
    这里做三件事：①运行中每 provider_probe_s 秒探测一次，连续失败就暂停所有槽（不烧数据）；
    ②暂停期间各槽在任务开始前排队；③最近 10 分钟供应商错误占比 > 20% 时把并发减半，回落再恢复。
    所有状态变化写 <runs>/_gate.jsonl，事后可对账。"""
    def __init__(self, cfg, out_dir):
        self.cfg = cfg
        self.maxp = max(1, int(cfg.get("max_parallel", 4)))
        self.sem = threading.Semaphore(self.maxp); self.held = 0
        self.paused = threading.Event(); self.paused_since = None; self.paused_total = 0.0
        self.probe_s = float(cfg.get("provider_probe_s", 30))
        self.fail_n = max(1, int(cfg.get("provider_fail_threshold", 3)))
        os.makedirs(out_dir, exist_ok=True)
        self.path = os.path.join(out_dir, "_gate.jsonl")
        self._stop = threading.Event(); self._fails = 0; self._streak = 0
        self._ev = []; self._lock = threading.Lock(); self._held = {}
        self._th = threading.Thread(target=self._loop, daemon=True); self._th.start()

    def _log(self, event, **kw):
        rec = {"ts": round(time.time(), 1), "iso": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **kw}
        try:
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _me(self):
        return self._held.setdefault(threading.get_ident(), {"held": False})

    def wait(self):
        """槽侧：拿到并发许可并且不处于暂停态才返回。"""
        while not self._stop.is_set():
            if self.paused.is_set():
                time.sleep(2); continue
            if self.sem.acquire(timeout=2):
                self._me()["held"] = True
                return

    def done(self):
        st = self._me()
        if st.get("held"):
            st["held"] = False
            try:
                self.sem.release()
            except ValueError:
                pass

    def note(self, ok):
        """槽侧：一次运行结束（ok=False 表示供应商错误）。"""
        with self._lock:
            self._ev.append((time.time(), not ok))

    def _loop(self):
        while not self._stop.wait(self.probe_s):
            ok, code, msg = probe_upstream(self.cfg)
            if ok:
                self._streak += 1; self._fails = 0
            else:
                self._fails += 1; self._streak = 0
                self._log("probe_fail", status=code, msg=(msg or "")[:200])
            if self._fails >= self.fail_n and not self.paused.is_set():
                self.paused.set(); self.paused_since = time.time()
                self._log("pause", fails=self._fails, status=code)
                print(f" {WARN} 上游连续 {self._fails} 次不可用（{code}）：暂停所有槽，恢复后自动继续（{(msg or '')[:120]}）")
            elif self.paused.is_set() and self._streak >= 2:
                self.paused.clear()
                if self.paused_since:
                    self.paused_total += time.time() - self.paused_since; self.paused_since = None
                self._log("resume", streak=self._streak)
                print(f" {OK} 上游恢复：继续跑（累计暂停 {self.paused_total/60:.1f} 分钟）")
            self._adapt()

    def _adapt(self):
        if not self.cfg.get("adaptive_slots", True):
            return
        now = time.time()
        with self._lock:
            self._ev = [(t, e) for t, e in self._ev if now - t < 900]
            recent = [(t, e) for t, e in self._ev if now - t < 600]
        if len(recent) < 20:
            return
        ratio = sum(1 for _, e in recent if e) / len(recent)
        if ratio > 0.2 and self.held == 0:
            got = 0
            for _ in range(self.maxp - max(1, self.maxp // 2)):
                if self.sem.acquire(timeout=5):
                    got += 1
                else:
                    break
            self.held = got
            self._log("slowdown", ratio=round(ratio, 3), capacity=self.maxp - got)
            print(f" {WARN} 近 10 分钟供应商错误占 {ratio*100:.0f}%：并发 {self.maxp} → {self.maxp - got}")
        elif ratio < 0.05 and self.held:
            for _ in range(self.held):
                try:
                    self.sem.release()
                except ValueError:
                    pass
            self.held = 0
            self._log("restore", ratio=round(ratio, 3), capacity=self.maxp)
            print(f" {OK} 供应商错误回落到 {ratio*100:.0f}%：并发恢复 {self.maxp}")

    def close(self):
        self._stop.set()
        if self.paused_since:
            self.paused_total += time.time() - self.paused_since; self.paused_since = None
        self._log("close", paused_s=round(self.paused_total, 1))


# ---------------------------------------------------------------- 跑题（多槽并行）
def run_slots(cfg, plan, split, out_dir, only=None):
    """plan: [(arm, [reps...])]。每个槽一个记账代理 + 一个线程；同一臂内题目串行。

    cfg.slot_shards > 1 时，每个 (臂, 重复组) 再按题目下标切成 k 份，各占一个槽（并发 = 槽数），
    用于单臂跑满并发；切片按题目原始顺序 i::k，槽之间题目不重叠。"""
    os.makedirs(out_dir, exist_ok=True); docker = cfg.get("isolation", "env") == "docker"
    stop = threading.Event(); servers, threads, used = [], [], set(); counts = {}
    gate = Gate(cfg, out_dir); run_bench.GATE = gate  # v1.9.2：熔断 + 全局并发上限（max_parallel）
    shards = max(1, int(cfg.get("slot_shards", 1) or 1))
    if shards > 1:
        ids_all = [t["task_id"] for t in run_bench.load_tasks(TASKS, split)]
        if only:
            ids_all = [i for i in ids_all if i in only]
        groups = [set(ids_all[j::shards]) for j in range(shards)]
        expanded = []
        for arm, reps in plan:
            for g in groups:
                if g:
                    expanded.append((arm, reps, g))
        plan = expanded

    def worker(key, arm, reps, port, slot_only=None):  # v1.6：槽内异常也要能对账，不能安静地少跑
        try:
            counts[key] = run_bench.run_slot(cfg, arm, reps, split, TASKS, out_dir, port,
                                             only=only if slot_only is None else slot_only, stop=stop)
        except KeyboardInterrupt:
            raise
        except BaseException as ex:  # v1.7.2：连 SystemExit 也要接住——配置不全时 run_slot 会 raise SystemExit，
            counts[key] = 0           # 原来它会把整个槽悄悄带走（无记录、无提示），正是最难查的那类“少跑”
            print(f" {BAD} 槽 {arm} r{reps} 异常退出：{type(ex).__name__}: {str(ex)[:200]}")
    base = cfg.get("proxy_base_port", 8700); upstream = cfg.get("upstream", "https://api.deepseek.com")
    first_slot = {}
    for i, entry in enumerate(plan):
        arm, reps = entry[0], entry[1]
        slot_only = entry[2] if len(entry) > 2 else None
        first_slot.setdefault(arm, i)  # 每个臂的第一个槽用它配置的端口，同臂后续槽用 base+50+i
        port = cfg["arms"][arm]["proxy_port"] if first_slot[arm] == i else base + 50 + i
        while port in used or not port_free(port):
            port += 1
        used.add(port)
        log_name = f"usage_{arm}_r{reps[0]}.jsonl" if slot_only is None else f"usage_{arm}_r{reps[0]}_s{i}.jsonl"
        if docker:
            servers.append(("docker", isolation.docker_start_proxy(port, os.path.abspath(out_dir), log_name, upstream,
                                                             inject_usage=(cfg.get("llm_provider") or "deepseek") == "wb")))
        else:
            servers.append(("local", usage_proxy.start(port, os.path.join(out_dir, log_name), upstream,
                                                       inject_usage=(cfg.get("llm_provider") or "deepseek") == "wb")))
        th = threading.Thread(target=worker, daemon=True, args=((arm, tuple(reps), i), arm, reps, port, slot_only))
        threads.append(th); th.start(); time.sleep(2)  # 错开启动
    print(f"已启动 {len(threads)} 个并行槽：" + "；".join(f"{e[0]} 重复{e[1]}" + (f" 片{e[2] and len(e[2])}" if len(e) > 2 else "") for e in plan)
          + f"。Ctrl+C 可中断，重跑同一命令即续跑（slot_shards={shards}）。")
    def _sig(_n, _f):
        raise KeyboardInterrupt  # SIGTERM/SIGHUP 复用同一条收尾路径（审计 P1-2）
    _prev = {}
    for _s in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
        try:
            _prev[_s] = signal.getsignal(_s)
            signal.signal(_s, _sig)
        except (ValueError, OSError):
            pass
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print(chr(10) + "收到中断：正在结束当前题目的 DSH 进程，并清理还活着的评测容器……")
        stop.set()
        for _p in list(run_bench.ACTIVE):
            run_bench.kill_tree(_p)
        _kill_eval_containers()
        for _t in threads:
            _t.join(timeout=30)
    finally:
        for _s, _h in _prev.items():
            try:
                signal.signal(_s, _h)
            except (ValueError, OSError):
                pass
    gate.close(); run_bench.GATE = None
    if gate.paused_total > 1:
        print(f"   （本轮上游熔断累计暂停 {gate.paused_total/60:.1f} 分钟，明细见 {os.path.relpath(gate.path, ROOT)}）")
    dropped = 0
    for kind, s in servers:
        isolation.docker_stop_proxy(s) if kind == "docker" else s.shutdown()
        dropped += getattr(s, "dropped", 0) or 0
    if dropped:  # v1.7.3：客户端中途断开很常见（重试/取消），别当错误刷屏
        print(f"   （记账代理丢了 {dropped} 次连接：DSH 主动断开，不是错误；对应运行仍按已收到的部分记账）")
    # v1.9.2：先对账，再决定是否评分。上一轮先花 25 分钟把 844 份空答卷评成“错”才报错，纯属浪费。
    mode = run_bench.grading_mode(cfg)
    # v1.6 对账：计划跑多少次、实际完成多少次。少跑（崩溃/丢目录/异常）不能当“跑完了”
    tasks = run_bench.load_tasks(TASKS, split)
    if only:
        tasks = [t for t in tasks if t["task_id"] in only]
    # v1.7：按结果文件对账，而不是按“这一次调用跑了几道”。
    # v1.6 用本次计数，续跑时（大部分题已完成被跳过）必然误报“少跑”；历史上的 crash 行修好重跑后也会永久报错；
    # 而 leak_blocked/dry 也被算成“完成”。这里以每个 (臂, 题, 重复) 的**最后一条**记录为准，只有真正判过分才算完成。
    last = {}
    # v1.9.2：评分已挪到对账之后，所以“跑完了没有”必须看作答记录（attempts）；只有 inline 模式是跑完一题就写 results。
    pat = "results_*.jsonl" if mode == "inline" else "attempts_*.jsonl"
    for fp in glob.glob(os.path.join(out_dir, pat)) + glob.glob(os.path.join(out_dir, "results_*.jsonl")):
        for l in open(fp, encoding="utf-8"):
            try:
                r = json.loads(l)
            except Exception:
                continue
            k = (r.get("arm"), r.get("task_id"), r.get("rep"))
            if k in last:
                keep = last[k]
                # v1.7.1：不靠文件/行顺序，按记录里的 ts 取最新（两次运行的槽分组不同时，同一键可能落在不同文件里）
                if keep.get("graded", True) is not False and r.get("graded", True) is False:
                    continue  # 已有判过分的结果，后来的未判分记录不覆盖它
                if keep.get("graded", True) is not False and (r.get("ts") or 0) < (keep.get("ts") or 0):
                    continue  # 旧记录，不覆盖新的
            last[k] = r
    want = [(e[0], t["task_id"], rep) for e in plan for rep in e[1] for t in tasks]  # 兼容分片后的 3 元组槽
    missing = [k for k in want if k not in last]
    bad = {}
    for k in want:
        r = last.get(k)
        if r is not None and r.get("graded", True) is False:
            bad[r.get("status")] = bad.get(r.get("status"), 0) + 1
    if not missing and not bad:
        # v1.9：测试与评分隔离。整段作答全部结束、且对账通过后才进入评分（after）；manual 只作答。
        if mode == "after":
            print("\n== 作答阶段结束，开始统一评分（答案此时才逐题临时解出）")
            grade_runs_dir(out_dir)
        elif mode == "manual":
            print(f"\n作答完成，尚未评分。评分：python bench/bench.py grade --runs {os.path.basename(out_dir)}（可以在另一台工具齐全的机器上做）")
        return True
    if bad:
        print(f" {BAD} 有运行没跑成：" + "，".join(f"{s} {n} 次" for s, n in bad.items()) +
              "（provider_error 看 _gate.jsonl 与 stderr；crash 看 traceback；leak_blocked 看 _logs/<ID>/preflight_leaks.json）")
        if "provider_error" in bad:
            print("   供应商不可用的运行**不算答案**：重跑同一条命令会自动补这些（它们不占位）。")
    if missing:
        print(f" {BAD} 计划 {len(want)} 次运行，还有 {len(missing)} 次没有结果（例如 {missing[0]}）。")
    print("   修好原因后重跑同一命令即可只补这些。")
    return False


def grade_runs_dir(out_dir, regrade=False, prefix=None):
    return subprocess.call([sys.executable, os.path.join(HERE, "grade_runs.py"), "--runs", out_dir, "--tasks", TASKS]
                           + (["--regrade"] if regrade else []) + (["--prefix"] + list(prefix) if prefix else []))


def cmd_grade(a):
    """v1.9：评分阶段单独命令。默认把 runs_smoke / runs_pre / runs 里所有待评答卷都评掉。"""
    dirs = [a.runs] if a.runs else ["runs_smoke", "runs_pre", "runs"]
    rc = 0
    for d in dirs:
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            rc |= grade_runs_dir(p, a.regrade, a.prefix)
    return rc


def split_reps(reps, k):
    k = max(1, min(k, len(reps)))
    return [reps[i::k] for i in range(k)]


TOOL_NAME_RX = re.compile(r'"(?:name|toolName|tool_name|tool)"\s*:\s*"([A-Za-z_][A-Za-z0-9_]{1,40})"')


def tool_surface(out_dir):
    """v1.9.1：从会话记录里数每个臂**实际调到的工具名**。
    金丝雀只能证明配置树长得对；跑起来看到什么工具，才能证明这一臂真的是“原生 / ops_* / run_code”。
    （插件升级后最容易出的问题就是：配置检查全绿，实际工具面已经换了或没了。）"""
    surf = {}
    mp = os.path.join(out_dir, "_map.jsonl")
    if not os.path.exists(mp):
        return surf
    for l in open(mp, encoding="utf-8"):
        try:
            m = json.loads(l)
        except Exception:
            continue
        arm = m.get("arm"); d = os.path.join(out_dir, "_logs", m.get("rid", ""))
        c = surf.setdefault(arm, {})
        for p in glob.glob(os.path.join(d, "**", "*"), recursive=True):
            try:
                if not os.path.isfile(p) or os.path.getsize(p) > 20_000_000:
                    continue
                text = open(p, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for n in TOOL_NAME_RX.findall(text):
                if n.endswith("Error"):  # 会话里也会出现错误类名（如 ReplacementError），不是工具
                    continue
                c[n] = c.get(n, 0) + 1
    return surf


def cmd_smoke(a):
    cfg = cfg_load(); out = os.path.join(ROOT, "runs_smoke")
    ids = cfg.get("smoke_tasks") or []
    if not ids:  # 空列表会让 run_slot 的 if only: 失效 → 一次跑满全部题目，这里直接拦下
        print(f" {BAD} config.json 的 smoke_tasks 为空：冒烟必须指定 1~3 道题（否则会跑满全部题目）。")
        return 1
    plan = [(arm, [0]) for arm in active_arms(cfg, a.arms)]
    if not getattr(a, "no_gate", False) and not health_gate(cfg):
        return 2
    ok = run_slots(cfg, plan, "all", out, only=ids)
    if run_bench.grading_mode(cfg) == "manual":  # 冒烟要验证评分也通，manual 模式下这里也顺手评掉
        grade_runs_dir(out)
    import analyze
    price = analyze.load_price(CONFIG)  # v1.7.5：跟随 currency/价表，不再写死 usd 键
    res = analyze.load(out, price); bad = 0
    models = set()
    for p in glob.glob(os.path.join(out, "usage_*.jsonl")):
        for l in open(p, encoding="utf-8"):
            u = json.loads(l); models.add(u.get("resp_model") or u.get("model"))
    print("\n== 冒烟结果")
    for arm in active_arms(cfg, a.arms):
        for (tid, rep), r in sorted(res.get(arm, {}).items()):
            u = r.get("usage", {}); tok = u.get("hit", 0) + u.get("miss", 0) + u.get("out", 0)
            good = r["status"] == "ok" and tok > 0 and not r.get("leak_suspect")  # v1.7.6：先算全再记账
            bad += not good
            print(f" {OK if good else BAD} {arm:6s} {tid:32s} 判分{'对' if r['pass'] else '错'}  状态 {r['status']}  "
                  f"代理记到 {u.get('requests', 0):.0f} 次请求 / {tok/1e3:.1f}k token  事件自报 {r.get('events', {}).get('ev_total', 0)/1e3:.1f}k")
    want_model = cfg.get("model_version_expected")
    print(f" 服务端模型名：{', '.join(sorted(m for m in models if m)) or '（未返回）'}；期望 {want_model}")
    if want_model and models and want_model not in models:
        bad += 1
        print(f" {BAD} 服务端模型名与期望不一致（{sorted(m for m in models if m)} vs {want_model}）"
              "—— 两臂若跑在不同模型上，结论不成立")
    surf = tool_surface(out)
    print("\n== 工具面实测（会话里真正调到的工具；这是各臂“处理”是否真的生效的直接证据）")
    for arm in active_arms(cfg, a.arms):
        names = surf.get(arm, {})
        exm = cfg["arms"][arm].get("expect", {})

        def hit(pat):
            return sorted(n for n in names if (n.startswith(pat[:-1]) if pat.endswith("*") else n == pat))

        print(f" {arm:9s} " + (", ".join(f"{n}×{c}" for n, c in sorted(names.items(), key=lambda kv: -kv[1])[:8])
                               or "（会话里没看到工具调用）"))
        anyw = exm.get("toolsAny") or []
        if anyw:
            got = sorted({n for pat in anyw for n in hit(pat)})
            print(f" {'✔' if got else '✘'} {arm}: 应至少出现其中一种工具（{'/'.join(anyw)}），实际 " + (", ".join(got) or "无"))
            bad += not got
        for pat in exm.get("toolsNone") or []:
            got = hit(pat)
            print(f" {'✔' if not got else '✘'} {arm}: 不应出现工具「{pat}」（实际 " + (", ".join(got) or "无") + "）")
            bad += bool(got)
    if analyze.BLOCKED:   # v1.7.6：BLOCKED 里既有“泄漏拦下”，也有“跑崩/没判分”，必须分开报
        bad += sum(analyze.BLOCKED.values())
        leak = crash = 0
        for pp in glob.glob(os.path.join(out, "results_*.jsonl")):
            for l in open(pp, encoding="utf-8"):
                r = json.loads(l)
                if r.get("status") in ("leak_blocked", "dry"):
                    leak += 1
                elif r.get("graded") is False:
                    crash += 1
        if leak:
            print(f" {BAD} 启动前泄漏自检拦下 {leak} 次运行（原因见 runs_smoke/_logs/<ID>/preflight_leaks.json）")
        if crash:
            print(f" {BAD} {crash} 次运行没跑成也没判分（看 runs_smoke/_logs/<ID>/stderr.txt，常见是配置/凭据问题）")
    if not any(res.get(arm) for arm in active_arms(cfg, a.arms)):
        bad += 1; print(f" {BAD} 一次都没跑成")
    print("\n" + ("冒烟通过。下一步：python bench/bench.py pre" if not bad else
                  "有 ✘：常见原因是 API Key 没配、覆盖层没生效（代理记到 0 次请求）、DSH 报错。每次运行的日志在 runs_smoke/_logs/<随机ID>/ 下，ID 对应哪道题见 runs_smoke/_map.jsonl。"))
    return 1 if bad else 0


def cmd_forget(a):
    """v1.8：把某些题的旧结果移出结果文件（存到 <runs>/_forgotten.jsonl），之后 pre/stage1/stage2 会把它们当没跑过重新跑。
    用途：v1.7.8 包里长链路题缺 config.json，那批结果要作废：python bench/bench.py forget long-chain-"""
    n = 0; out_dir = os.path.join(ROOT, a.runs)
    b = batch_ids(getattr(a, "batch", None))  # v1.8.2：同前缀的题可能分属两批（long-lb2- 新旧都有），可用 --batch 限定
    for fp in glob.glob(os.path.join(out_dir, "results_*.jsonl")) + glob.glob(os.path.join(out_dir, "attempts_*.jsonl")):  # v1.9：作答记录也要一起作废
        keep, drop = [], []
        for l in open(fp, encoding="utf-8"):
            try:
                tid = json.loads(l).get("task_id", "")
            except Exception:
                keep.append(l); continue
            hit = any(tid.startswith(p) for p in a.prefix) and (b is None or tid in b)
            (drop if hit else keep).append(l)
        if drop:
            with open(os.path.join(out_dir, "_forgotten.jsonl"), "a", encoding="utf-8") as f:
                f.writelines(drop)
            # 原地重写最怕「写到一半被杀」：先写 tmp 再原子替换（审计 P2-1）
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, fp)
            n += len(drop)
    print(f"已移出 {n} 条记录（作答与评分记录合计）" + (f"（备份在 {a.runs}/_forgotten.jsonl）" if n else "") + "。重跑同一阶段命令即可补上。")


def batch_ids(batch):
    """v1.8：题目的 batch 字段（原 144 题没有该字段，视为 base）。"""
    if not batch:
        return None
    ids = set()
    for tid in os.listdir(TASKS):
        p = os.path.join(TASKS, tid, "task.json")
        if os.path.isfile(p) and (json.load(open(p, encoding="utf-8")).get("batch") or "base") == batch:
            ids.add(tid)
    if not ids:
        print(f" {BAD} 没有 batch={batch} 的题"); sys.exit(1)
    return ids


def cmd_pre(a):
    cfg = cfg_load(); reps = list(range(cfg.get("pre_repeats", 2)))
    only = batch_ids(getattr(a, "batch", None))
    if not getattr(a, "no_gate", False) and not health_gate(cfg):
        return 2
    return 0 if run_slots(cfg, [("std", r) for r in split_reps(reps, a.slots or len(reps))], "pool", os.path.join(ROOT, "runs_pre"), only=only) else 1


def cmd_calibrate(a):
    cfg = cfg_load()
    # 没有 results 时 calibrate 会「110 题全是 unpiloted」却退出 0，坏题照样进正式跑（审计 P1-3）
    if not glob.glob(os.path.join(ROOT, "runs_pre", "results_std_r*.jsonl")):
        print(f" {BAD} runs_pre 里没有 results_std_r*.jsonl：先跑 `bench.py pre`，并确认**评分过**"
              "（python bench/bench.py grade --runs runs_pre）。没有预跑结果就没法校准题目、也剔不掉坏题。")
        return 2
    rc = subprocess.call([sys.executable, os.path.join(HERE, "calibrate.py"), "--tasks", TASKS, "--runs", os.path.join(ROOT, "runs_pre"),
                          "--arm", "std", "--out", os.path.join(ROOT, "manifest", "final_ids.json")] + (["--apply"] if not a.dry else []))
    if rc == 0 and not a.dry:
        cmd_manifest(a)
        rc2 = subprocess.call([sys.executable, os.path.join(HERE, "sample_size.py"),
                               "--pre", os.path.join(ROOT, "runs_pre", "results_std_r*.jsonl")])
        if rc2 != 0:  # 功效估计失败不能吞掉：它决定「这一批数据够不够用来下结论」
            print(f" {WARN} 功效估计（sample_size.py）返回 {rc2}：这不影响已校准的题单，但别忽视")
    return rc


def latest_records(out_dir, arms=None):
    """同一 (臂, 题, 重复) 只取最新一条（按 ts），attempts 与 results 一起看。"""
    last = {}
    for fp in glob.glob(os.path.join(out_dir, "attempts_*.jsonl")) + glob.glob(os.path.join(out_dir, "results_*.jsonl")):
        for l in open(fp, encoding="utf-8"):
            try:
                r = json.loads(l)
            except Exception:
                continue
            if arms and r.get("arm") not in arms:
                continue
            k = (r.get("arm"), r.get("task_id"), r.get("rep"))
            if k not in last or (r.get("ts") or 0) >= (last[k].get("ts") or 0):
                last[k] = r
    return last


def _kill_eval_containers():
    """把还在跑评测的容器（名字 wk-*）停掉。Ctrl+C / SIGTERM 时 kill_tree 只杀 docker CLI，
    容器本身会活着继续对着已经关掉的代理重试（审计 P1-2 实测）。"""
    if not shutil.which("docker"):
        return 0
    n = 0
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True,
                             text=True, timeout=30).stdout
        for name in out.split():
            if name.startswith("wk-") and not name.startswith("wk-px-"):
                isolation.docker_kill(name[3:])
                n += 1
    except Exception:
        pass
    return n


atexit.register(_kill_eval_containers)  # 非正常退出也尽量不留容器

def stage_summary(cfg, plan, out_dir, only=None):
    """v1.9.2 收尾硬校验：这一轮到底跑成了没有。
    三件事：每个臂有没有 plan 要求的那么多 ok；有没有 provider_error 残留；上游错误率是多少。
    任一不达标 → 返回 False（不进入 interim，避免对着 146/990 的数据算区间）。"""
    # --only / --batch 是「抽查/补遍数」，期望值要按筛选后的题数算，否则跑得再成功也必报「不完整」（审计 R11）
    _all = run_bench.load_tasks(TASKS, "main")
    tasks = len([t for t in _all if not only or t["task_id"] in only])
    if only:
        print("（--only 抽查：只跑 %d 题，对账按这个数算）" % tasks)
    exp = {}
    for arm, reps in plan:
        exp[arm] = exp.get(arm, 0) + len(reps) * tasks
    recs = latest_records(out_dir, set(exp))
    have = collections.Counter(); bad = collections.Counter(); other = collections.Counter()
    for (arm, _t, _r), r in recs.items():
        st = str(r.get("status") or "")
        if st == "ok":
            have[arm] += 1
        elif st == "provider_error":
            bad[arm] += 1
        elif r.get("graded") is False or st not in ("ok", "timeout"):
            other[st] += 1
    print("\n== 本轮对账（v1.9.2：供应商错误不算答案）")
    ok = True
    for arm in sorted(exp):
        line = f" {OK if have[arm] >= exp[arm] else BAD} {arm:9s} ok {have[arm]}/{exp[arm]}"
        if bad[arm]:
            line += f"；provider_error {bad[arm]}"
        print(line)
        ok &= have[arm] >= exp[arm] and bad[arm] == 0
    if other:
        print(f" {WARN} 其它未完成状态：" + "，".join(f"{k} {v}" for k, v in other.items()))
    # 上游错误率：代理按状态码记过，这里汇总；再加熔断日志里的暂停时长
    st = collections.Counter()
    for fp in glob.glob(os.path.join(out_dir, "usage_*.jsonl")):
        for l in open(fp, encoding="utf-8"):
            try:
                u = json.loads(l)
            except Exception:
                continue
            st[u.get("status")] += 1
    if st:
        tot = sum(st.values()); errs = sum(v for k, v in st.items() if k not in (200, 204))
        print(f"   上游：{tot} 次请求，非 2xx {errs} 次（{errs/max(tot,1)*100:.1f}%）；" +
              "，".join(f"{k}×{v}" for k, v in sorted(st.items(), key=lambda kv: -kv[1])[:4]))
        ok &= errs == 0 or errs / max(tot, 1) < 0.02
    gp = os.path.join(out_dir, "_gate.jsonl")
    if os.path.exists(gp):
        ev = collections.Counter()
        for l in open(gp, encoding="utf-8"):
            try:
                ev[json.loads(l).get("event")] += 1
            except Exception:
                pass
        print("   熔断：" + "，".join(f"{k} {v}" for k, v in ev.items()))
    if not ok:
        print(f" {BAD} 这一轮不完整：补齐上面缺的运行（重跑同一条命令会只补缺的），别拿它算结论。")
    return ok


def paired_task_count(cfg, out_dir):
    arms = cfg["stages"]["stage1"].get("arms") or []
    sets = {}
    for (arm, tid, _rep), r in latest_records(out_dir, set(arms)).items():
        if str(r.get("status")) == "ok" and r.get("graded", True) is not False:
            sets.setdefault(arm, set()).add(tid)
    if not arms or any(a not in sets for a in arms):
        return 0
    return len(set.intersection(*[sets[a] for a in arms]))


def cmd_retry(a):
    """v1.9.2：把指定状态的记录移出结果文件（存 <runs>/_forgotten.jsonl），之后重跑同一条命令会补这些。

    事故恢复入口（2026-11-07 上游 503 风暴烧掉 844 次运行）：
        python bench/bench.py retry --status provider_error,exit1 --dry-run   # 先看有多少
        python bench/bench.py retry --status provider_error,exit1             # 再执行
    注意：provider_error 本身不占位（写了 graded=False，重跑会自动补），所以平时用不到这条命令。"""
    d = os.path.join(ROOT, a.runs)
    want = {s.strip() for s in (a.status or "provider_error,exit1").split(",") if s.strip()}
    if "all" in want:
        want |= {"crash", "provider_error"}
    files = glob.glob(os.path.join(d, "attempts_*.jsonl")) + glob.glob(os.path.join(d, "results_*.jsonl"))
    jobs, n_att, n_res, rids = [], 0, 0, []
    for fp in files:
        keep, drop = [], []
        for l in open(fp, encoding="utf-8"):
            try:
                r = json.loads(l)
            except Exception:
                keep.append(l); continue
            st = str(r.get("status") or "")
            hit = st in want or ("exit1" in want and st.startswith("exit")) or ("all" in a.status.split(",") and st != "ok")
            (drop if hit else keep).append(l)
            if hit:
                rids.append(r.get("rid"))
        if drop:
            jobs.append((fp, keep, drop))
            if os.path.basename(fp).startswith("attempts_"):
                n_att += len(drop)
            else:
                n_res += len(drop)
    print(f"待清理：attempts {n_att} 条、results {n_res} 条（状态 {sorted(want)}）")
    if not jobs:
        print("没有匹配的记录。")
        return 0
    if a.dry_run:
        print("（--dry-run：没有改动任何文件）")
        return 0
    forg = os.path.join(d, "_forgotten.jsonl")
    with open(forg, "a", encoding="utf-8") as f:
        for fp, keep, drop in jobs:
            f.writelines(l if l.endswith("\n") else l + "\n" for l in drop)
            with open(fp, "w", encoding="utf-8") as g:
                g.writelines(keep)
    print(f"已移入 {os.path.relpath(forg, ROOT)}；重跑同一条命令会补这些。")
    if a.purge_logs:
        n = 0
        for rid in {r for r in rids if r}:
            p = os.path.join(d, "_logs", rid)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True); n += 1
        print(f"已删除 {n} 个运行日志目录（--purge-logs）")
    return 0


def cmd_stage(a, which):
    cfg = cfg_load(); st = cfg["stages"][which]; plan = []
    for arm in active_arms(cfg, st.get("arms")):
        for r in split_reps(list(range(st["rep_from"], st["rep_to"])), st.get("slots_per_arm", 1)):
            plan.append((arm, r))
    only = set(a.only) if getattr(a, "only", None) else None
    b = batch_ids(getattr(a, "batch", None))
    if b is not None:
        only = b if only is None else (only & b)
        # --only 拼错题号时原来会「✔ ok 0/0」退出 0（审计 P2-2）：空集合 / 未知题号都要报错
        if only is not None:
            _have = {t["task_id"] for t in run_bench.load_tasks(TASKS, "main")}
            if not only:
                print(f" {BAD} --only/--batch 过滤后没有剩下任何题目")
                return 2
            _miss = sorted(set(only) - _have)
            if _miss:
                print(f" {BAD} 这些题号不存在：{', '.join(_miss[:5])}" + ("…" if len(_miss) > 5 else ""))
                return 2
    print(f"计划 {sum(len(r) for _, r in plan)} 个槽 × {len(run_bench.load_tasks(TASKS, 'main'))} 题；"
          f"全局并发上限 {cfg.get('max_parallel', 4)}")
    if not getattr(a, "no_gate", False) and not health_gate(cfg):
        return 2
    out_dir = os.path.join(ROOT, "runs")
    ok = run_slots(cfg, plan, "main", out_dir, only=only)
    return 0 if (ok and stage_summary(cfg, plan, out_dir, only=only)) else 1


def cmd_interim(a):
    cfg = cfg_load(); out_dir = os.path.join(ROOT, "runs")
    n = paired_task_count(cfg, out_dir)   # v1.9.2：数据不足时说清楚，别对着 4 道配对题算 99% 区间
    # 演练（bench/mock/rehearse.py）只有十道题，用 DSHB_MIN_PAIRS 放宽；正式跑不要设这个变量
    need = int(os.environ.get("DSHB_MIN_PAIRS") or 30)
    if n < need:
        print(f" {BAD} 只有 {n} 道题在各臂上都有有效结果（至少要 {need} 道才值得看区间）。")
        print("    先补齐 stage1：重跑 python bench/bench.py stage1（只补缺的），或用 python bench/bench.py retry 清掉坏记录再跑。")
        return 2
    print(f"配对任务 {n} 道")
    return subprocess.call([sys.executable, os.path.join(HERE, "analyze.py"), "--runs", out_dir, "--base", "std",
                            "--margin", str(cfg.get("margin", 5)), "--conf", "0.99", "--out", os.path.join(ROOT, "interim.md")])


def cmd_report(a):
    cfg = cfg_load(); m = str(cfg.get("margin", 5))
    rc1 = subprocess.call([sys.executable, os.path.join(HERE, "analyze.py"), "--runs", os.path.join(ROOT, "runs"), "--base", "std",
                     "--margin", m, "--out", os.path.join(ROOT, "report.md")]
                    + (["--platform", a.platform] if getattr(a, "platform", None) else [])
                    + (["--batch", a.batch] if getattr(a, "batch", None) else []))
    if rc1 != 0:   # v1.7.6：子进程失败必须传出去，原来会“报错但退出码 0”
        print(f" {BAD} 报告生成失败（analyze.py 退出码 {rc1}）")
        return 1
    if getattr(a, "platform", None) or getattr(a, "batch", None):
        print(f" {WARN} 评分卡不支持 --platform/--batch：它是全量混池的，而 report.md 是过滤后的；两处数字不一致时以 report.md 为准。")
    rc2 = subprocess.call([sys.executable, os.path.join(HERE, "scorecard.py"), "--runs", os.path.join(ROOT, "runs"), "--base", "std",
                     "--out", os.path.join(ROOT, "scorecard")])
    if rc2 != 0:
        print(f" {BAD} 评分卡生成失败（scorecard.py 退出码 {rc2}）")
        return 1
    print(f"报告：{os.path.join(ROOT, 'report.md')}，评分卡：{os.path.join(ROOT, 'scorecard.png')}")
    return 0


def cmd_status(a):
    cfg = cfg_load()
    for d in ("runs_smoke", "runs_pre", "runs"):
        p = os.path.join(ROOT, d)
        if not os.path.isdir(p):
            continue
        cnt = {}
        for f in glob.glob(os.path.join(p, "results_*.jsonl")):
            for l in open(f, encoding="utf-8"):
                try:
                    r = json.loads(l)
                except Exception:
                    continue  # 截断的行跳过，别让 status 整个崩掉（审计 P2-1）
                c = cnt.setdefault(r["arm"], [0, 0, 0, 0]); c[0] += 1; c[1] += r["pass"]; c[2] += r.get("leak_suspect", False)
                c[3] += r["status"] == "leak_blocked"
        pend = 0
        rids = {json.loads(l).get("rid") for f in glob.glob(os.path.join(p, "results_*.jsonl")) for l in open(f, encoding="utf-8")}
        for f in glob.glob(os.path.join(p, "attempts_*.jsonl")):
            pend += sum(1 for l in open(f, encoding="utf-8") if json.loads(l).get("rid") not in rids)
        if pend:
            print(f"{d}: 还有 {pend} 份答卷待评分（python bench/bench.py grade）")
        print(f"{d}: " + "；".join(f"{k} 已跑 {v[0]} 次、对 {v[1]}" + (f"、疑似偷看 {v[2]}" if v[2] else "") +
                                  (f"、自检拦下 {v[3]}" if v[3] else "") for k, v in sorted(cnt.items())))


def cmd_manifest(a=None):
    rows = []
    for tid in sorted(os.listdir(TASKS)):
        p = os.path.join(TASKS, tid, "task.json")
        if os.path.isfile(p):
            t = json.load(open(p, encoding="utf-8")); t.setdefault("batch", "base"); rows.append({k: v for k, v in t.items() if k != "prompt"})
    os.makedirs(os.path.join(ROOT, "manifest"), exist_ok=True)
    with open(os.path.join(ROOT, "manifest", "manifest.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cols = ["task_id", "role", "batch", "dimension", "source", "source_id", "stratum", "grader", "license", "timeout_s"]
    with open(os.path.join(ROOT, "manifest", "manifest.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    from collections import Counter
    print("题单已更新：", dict(Counter(r.get("role") for r in rows)))


def cmd_gpqa(a):
    tok = os.environ.get("HF_TOKEN")
    raw = os.path.join(ROOT, "raw"); os.makedirs(raw, exist_ok=True); dst = os.path.join(raw, "gpqa_diamond.csv")
    if not os.path.exists(dst):
        if not tok:
            print("需要 Hugging Face 令牌：先在 https://huggingface.co/datasets/Idavidrein/gpqa 网页登录并同意条款，"
                  "再到 Settings → Access Tokens 建一个 read 令牌，然后设置环境变量 HF_TOKEN 后重跑。"); return 1
        import urllib.request
        req = urllib.request.Request("https://huggingface.co/datasets/Idavidrein/gpqa/resolve/main/gpqa_diamond.csv",
                                     headers={"Authorization": f"Bearer {tok}"})
        open(dst, "wb").write(urllib.request.urlopen(req, timeout=120).read())
    subprocess.check_call([sys.executable, os.path.join(HERE, "build_public.py"), "--raw", raw, "--out", TASKS, "--only", "gpqa_diamond"])
    import answers
    answers.pack(TASKS); cmd_manifest()
    print("GPQA 已加入。注意：GPQA 作者要求不要公开转载题目，tasks/ 目录不要推到公开仓库。")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("grade", "forget", "doctor", "setup", "pre", "stage1", "stage2", "interim", "report", "status", "manifest", "gpqa", "calibrate", "smoke", "leakcheck", "retry"):
        sp = sub.add_parser(name)
        if name in ("pre", "smoke", "stage1", "stage2"):
            sp.add_argument("--no-gate", action="store_true", help="跳过上游健康闸门（只用于离线调试）")
        if name == "retry":
            sp.add_argument("--status", default="provider_error,exit1",
                            help="要清掉的状态，逗号分隔；all = 除 ok 以外全清（默认 provider_error,exit1）")
            sp.add_argument("--runs", default="runs")
            sp.add_argument("--dry-run", action="store_true", help="只报条数，不改文件")
            sp.add_argument("--purge-logs", action="store_true", help="顺手删掉这些运行的 _logs 目录")
        if name == "smoke":
            sp.add_argument("--arms", nargs="*")
        if name == "pre":
            sp.add_argument("--slots", type=int, default=0, help="并行槽数，默认每个重复一个槽")
        if name in ("stage1", "stage2"):
            sp.add_argument("--only", nargs="*", help="只跑这些题号（补遍数用），例如 --only code-poly-python-paasio")
        if name in ("pre", "stage1", "stage2"):
            sp.add_argument("--batch", default=None, help="v1.8：只跑某一批题，例如 --batch hard2 只跑新加的难题")
        if name == "grade":
            sp.add_argument("--runs", default=None, help="只评某个目录：runs_smoke / runs_pre / runs；默认三个都评")
            sp.add_argument("--regrade", action="store_true", help="已评过的也重新评（判分器修好后用，旧结果自动备份）")
            sp.add_argument("--prefix", nargs="*", help="只评这些题号前缀")
        if name == "forget":
            sp.add_argument("prefix", nargs="+", help="题号前缀，例如 long-chain-")
            sp.add_argument("--runs", default="runs", help="runs 或 runs_pre")
            sp.add_argument("--batch", default=None, help="只作废某一批的题，例如 long-lb2- --batch base 只作废原批长文题")
        if name == "report":
            sp.add_argument("--platform", default=None, help="只统计某个平台：windows / wsl / linux")
            sp.add_argument("--batch", default=None, help="只统计某一批题：base / hard2")
        if name == "calibrate":
            sp.add_argument("--dry", action="store_true", help="只看结果，不改题单")
    a = ap.parse_args()
    fn = {"grade": cmd_grade, "forget": cmd_forget, "doctor": cmd_doctor, "setup": cmd_setup, "smoke": cmd_smoke, "pre": cmd_pre, "calibrate": cmd_calibrate,
          "stage1": lambda x: cmd_stage(x, "stage1"), "stage2": lambda x: cmd_stage(x, "stage2"), "interim": cmd_interim,
          "report": cmd_report, "status": cmd_status, "manifest": cmd_manifest, "gpqa": cmd_gpqa, "retry": cmd_retry,
          "leakcheck": lambda x: subprocess.call([sys.executable, os.path.join(HERE, "leakcheck.py"), "--tasks", TASKS])}[a.cmd]
    sys.exit(fn(a) or 0)


if __name__ == "__main__":
    main()