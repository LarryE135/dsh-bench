#!/usr/bin/env python3
"""跑题器（外部 runner）：对一个臂，把每道题 × 每次重复跑一遍 DSH headless，跑完在外部判分、记账。

一般不直接用它，用 bench.py（自动起记账代理、并行跑各臂）。单独调试：
    python run_bench.py --arm std --reps 0 --only know-mmlupro-8263 --proxy-port 8701
（单独跑时需先另开窗口：python usage_proxy.py --port 8701 --log runs/usage_std_r0.jsonl）

每次运行（隔离设计详见 isolation.py 与 BENCHMARK.md 第 13 节）：
  1. 生成随机 ID，工作目录 = <临时目录>/w/<ID>，从题目 workspace/ 复制一份干净的；
  2. 组装任务文本（prompting.render，四臂一致）；覆盖层复制成 1.yml、2.yml……，再加本次运行层
     （模型请求指向记账代理、会话/存储写到本次专属目录 → 每次都是全新会话，也看不到以前的会话）；
  3. 环境变量清洗 + 断网变量；启动前泄漏自检（任务文本、目录树、环境变量、命令行、覆盖层），命中就不跑；
  4. 启动 DSH（env 模式直接起进程；docker 模式起一次性容器）；超时连同子进程/容器一起结束；
  5. 外部判分（答案平时混淆打包，判分时才临时解出）；记账按随机 ID 打标签；
  6. 结果写 runs/results_<臂>_r<首个重复号>.jsonl；ID→(臂,题,第几次) 只记在 runs/_map.jsonl；
     DSH 事件流、stderr、会话记录移到 runs/_logs/<ID>/，供事后复查。
"""
import argparse, glob, json, os, random, re, shutil, signal, subprocess, sys, time, traceback, urllib.request
import grade, isolation, leakcheck, prompting

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ACTIVE = set()  # 正在运行的 DSH 进程（bench.py 收到 Ctrl+C 时用来结束它们）
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 访问本机代理时绕过系统代理（开着 Clash 等工具时 urllib 会把 127.0.0.1 也转出去）

# v1.9.2：供应商侧不可用必须和“这一臂没答出来”分开。
# 2026-11-07 stage1 被 wb-proxy 账户池打空烧掉 844/990 次：exactly 这些错误的特征串（503 / 429 / 账户池耗尽），
# 当时统统记成 exit1 并当成失败样本，等于把上游故障算成模型能力差异。现在单独记 provider_error。
PROV_RX = re.compile(r"PI_AI_ERROR|Provider finish_reason: error|too many requests|no usable account|\"status\"\s*:\s*(?:429|5\d\d)")
GATE = None  # bench.py 注入的熔断/并发控制器（有 wait() 与 paused_total 属性）；单独跑 run_bench.py 时为 None


def gate_wait():
    if GATE is not None:
        try:
            GATE.wait()
        except Exception:
            pass


def gate_done():
    if GATE is not None:
        try:
            GATE.done()
        except Exception:
            pass


def gate_note(ok):
    """把“这一跑是不是供应商错误”告诉熔断器，用来判断要不要降并发。"""
    if GATE is not None:
        try:
            GATE.note(ok)
        except Exception:
            pass


def read_tail(path, limit=2000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[-limit:]
    except OSError:
        return ""


def prov_code_of(text):
    m = re.search(r"\b(429|5\d\d)\b", text or "")
    return int(m.group(1)) if m else None


def t_sleep(sec, stop=None):
    """可被打断的退避睡眠；stop 置位时提前返回。"""
    end = time.time() + sec
    while time.time() < end:
        if stop is not None and stop.is_set():
            return
        time.sleep(min(2.0, max(0.0, end - time.time())))


def load_cfg(path):
    cfg = json.load(open(path, encoding="utf-8"))
    cfg["_dir"] = os.path.dirname(os.path.abspath(path))
    return cfg


def resolve_dsh(cfg):
    """找到能直接用 subprocess 启动的 dsh 命令（列表形式，不经过 shell）。
    Windows 上 npm 装的 dsh 是 dsh.cmd 包装脚本：经 cmd.exe 传多行题面会被截断、引号会被吃掉，
    所以这里读 npm 包的 package.json 找到真正的入口 JS，改用 `node 入口.js` 启动。"""
    if cfg.get("dsh_command"):
        return list(cfg["dsh_command"])
    exe = shutil.which("dsh")
    if not exe:
        raise SystemExit("找不到 dsh 命令：请先 npm i -g @deepseek-ai/dsh，或在 config.json 里写 dsh_command")
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat", ".ps1")):
        pkg = os.path.join(os.path.dirname(exe), "node_modules", "@deepseek-ai", "dsh")
        try:
            meta = json.load(open(os.path.join(pkg, "package.json"), encoding="utf-8"))
            b = meta.get("bin"); b = b if isinstance(b, str) else (b or {}).get("dsh") or next(iter((b or {}).values()))
            return [shutil.which("node") or "node", os.path.normpath(os.path.join(pkg, b))]
        except Exception as ex:
            raise SystemExit(f"无法从 {exe} 找到 dsh 的入口 JS（{ex}）。请在 config.json 写 dsh_command，例如 "
                             "[\"node\", \"C:/Users/你/AppData/Roaming/npm/node_modules/@deepseek-ai/dsh/<bin>.js\"]")
    return [exe]


def kill_tree(p):
    """超时后连同 DSH 拉起的子进程（shell、node、python）一起结束，避免残留进程占 CPU、改文件。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        pass


def run_proc(cmd, cwd, env, timeout, out_path, err_path, on_timeout=None):
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with open(out_path, "wb") as fo, open(err_path, "wb") as fe:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=fo, stderr=fe, **kw)
        ACTIVE.add(p)
        try:
            rc = p.wait(timeout=timeout)
            # rc 为负 = 被信号杀掉（Ctrl+C / SIGTERM 走的 kill_tree）。那不是模型的错：
            # 既不能判「答错」，也不能让续跑跳过它（审计 P1-1）。
            if rc in (-9, -15):
                return "interrupted"
            return "ok" if rc == 0 else f"exit{rc}"
        except subprocess.TimeoutExpired:
            if on_timeout:
                on_timeout()
            kill_tree(p); p.wait(); return "timeout"
        finally:
            ACTIVE.discard(p)


def events_summary(path):
    """读 DSH --json 事件流：是否正常结束、轮数，以及事件里自报的 token（只作代理记账的交叉核对）。"""
    s = {"final": False, "status_events": 0, "ev_input": 0, "ev_cache_read": 0, "ev_output": 0, "ev_total": 0}
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            u = e.get("usage") if isinstance(e, dict) else None
            if isinstance(u, dict):
                for k, kk in (("inputTokens", "ev_input"), ("cacheReadTokens", "ev_cache_read"), ("outputTokens", "ev_output"), ("totalTokens", "ev_total")):
                    if isinstance(u.get(k), (int, float)):
                        s[kk] += u[k]
            if e.get("type") == "status":
                s["status_events"] += 1
            if e.get("type") == "final":
                s["final"] = True
    except FileNotFoundError:
        pass
    return s


def tag(port, value):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/__tag", data=json.dumps({"tag": value}).encode(), method="POST")
        LOCAL.open(req, timeout=5)
    except Exception as ex:
        print("  ! 记账代理没响应，这次的 token 将无法计入：", ex)


def load_tasks(tdir, split):
    out = []
    for tid in sorted(os.listdir(tdir)):
        p = os.path.join(tdir, tid, "task.json")
        if os.path.isfile(p):
            t = json.load(open(p, encoding="utf-8"))
            role = t.get("role", "main")
            if split == "all" or (split == "pool" and role in ("main", "reserve")) or role == split:
                out.append(t)
    return out


def leak_patterns():
    """事后筛查：模型的事件流/会话里出现这些，说明它碰到了 runner 侧的东西。"""
    pats = [re.escape(os.path.abspath(ROOT)), re.escape(os.path.abspath(ROOT).replace("\\", "/")),
            r"_answers\.bin", r"[\\/]hidden[\\/]", r"answer\.json", r"expected\.json", r"golden\.xls", r"_map\.jsonl", r"task\.json"]
    return re.compile("|".join(pats), re.I)


def preflight(prompt, wdir, env, cmd, patches):
    """启动前泄漏自检：任何一处命中关键词都不启动（被测模型一个字也看不到）。"""
    found = []
    leakcheck.hits(prompt, "prompt", found)
    leakcheck.hits(os.path.abspath(wdir), "path:<工作目录>", found)
    for root, dirs, files in os.walk(wdir):
        if "node_modules" in dirs:
            dirs.remove("node_modules")
        for n in dirs + files:
            leakcheck.hits(os.path.relpath(os.path.join(root, n), wdir), "path", found)
        if ".git" in dirs:
            found.append({"where": "git", "term": ".git", "context": root})
    leakcheck.scan_env({k: v for k, v in env.items() if k not in isolation.KEEP_ENV}, found)
    for a in cmd:
        if a != prompt:
            leakcheck.hits(a, "launch:argv", found)
    for p in patches:
        leakcheck.hits(open(p, encoding="utf-8").read(), f"launch:{os.path.basename(p)}", found)
    return found


def grading_mode(cfg):
    m = str(cfg.get("grading", "after")).lower()
    return m if m in ("after", "manual", "inline") else "after"


def run_slot(cfg, arm_name, reps, split, tasks_dir, out_dir, proxy_port, only=None, dry_run=False, log=print, stop=None):
    """一个“槽”= 一个臂 + 一组重复号 + 一个记账代理端口，题目串行跑。返回本槽跑了几次。"""
    os.makedirs(out_dir, exist_ok=True)
    mode = cfg.get("isolation", "env"); docker = mode == "docker"
    # v1.9：测试与评分隔离。grading = after（默认，整段跑完再统一评分）/ manual（只跑不评，之后手动 grade，可换机器评）/ inline（旧行为，跑完一题马上评）。
    # 非 inline 时，本阶段只写“作答记录” attempts_*.jsonl，不解答案、不调判分器；评分由 grade_runs.py 单独完成，写 results_*.jsonl。
    deferred = grading_mode(cfg) != "inline"
    res_path = os.path.join(out_dir, f"{'attempts' if deferred else 'results'}_{arm_name}_r{reps[0]}_p{proxy_port}.jsonl")  # 每个槽一个文件，避免并发写同一文件
    map_path = os.path.join(out_dir, "_map.jsonl")
    done = set()
    for p in glob.glob(os.path.join(out_dir, f"results_{arm_name}_r*.jsonl")) + glob.glob(os.path.join(out_dir, f"attempts_{arm_name}_r*.jsonl")):
        for l in open(p, encoding="utf-8"):
            r = json.loads(l)
            if r.get("graded", True) is not False:  # 被拦下/没真正跑的不算“已完成”：修好原因后重跑会补上（v1.5）
                done.add((r["task_id"], r["rep"]))
    tasks = load_tasks(tasks_dir, split)
    if only:
        tasks = [t for t in tasks if t["task_id"] in only]
    prof = isolation.profile_of(cfg, arm_name); home = isolation.dsh_home(cfg, docker)
    env = isolation.clean_env(cfg, home, leakcheck.RX); dsh = None if docker else resolve_dsh(cfg)
    wroot, sroot = isolation.work_root(cfg), isolation.state_root(cfg); LEAK = leak_patterns(); n = 0
    for rep in reps:
        order = tasks[:]; random.Random(f"{cfg['seed']}-order-{rep}").shuffle(order)  # 每轮题序随机，各臂用同一个随机序
        if cfg.get("stagger_arms", True) and order:
            # v1.5：各臂在同一序列上错开起点。否则各臂几乎同时做同一道题——env 模式下读权限不受限，
            # 一个臂能在隔壁目录看到另一个臂正在写的答案；同一时刻的相同前缀也会互相“预热”模型接口的缓存。
            arms = list(cfg["arms"]); k = arms.index(arm_name) * len(order) // max(1, len(arms))
            order = order[k:] + order[:k]
        for t in order:
            rid = wdir = sdir = ldir = None  # v1.7：每题重置，出错时不会把上一题的目录/ID 当成这一题的
            retried = 0; prov_code = None; prov_msg = ""  # v1.9.2：供应商不可用的重试与证据
            if stop is not None and stop.is_set():
                return n
            tid = t["task_id"]
            if (tid, rep) in done:
                continue
            try:  # v1.6：单题出错不能把整个槽静默停机（v1.5 打包丢空目录就是这样丢了 24 道题）
                rid = isolation.new_id(); tdir = os.path.join(tasks_dir, tid)
                # 每题独占一层父目录：原来是 <tmp>/w/<rid>，所有 run 互为兄弟目录 —— env 模式下读权限不受限，
                # 一句 `ls ..` 就能看到别的臂/别的遍已经写好的 ANSWER.txt / output.xlsx（P0-1）。
                wdir = os.path.join(wroot, rid, "work")
                sdir, ldir = os.path.join(sroot, rid), os.path.join(out_dir, "_logs", rid)
                shutil.copytree(os.path.join(tdir, "workspace"), wdir); os.makedirs(ldir, exist_ok=True)
                with open(map_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"rid": rid, "arm": arm_name, "task_id": tid, "rep": rep, "ts": time.time()}, ensure_ascii=False) + "\n")
                if docker:
                    base_url, sess, store = isolation.base_url(cfg, proxy_port, True), isolation.C_STATE + "/s", isolation.C_STATE + "/t"
                else:
                    base_url, sess, store = isolation.base_url(cfg, proxy_port, False), os.path.join(sdir, "s"), os.path.join(sdir, "t")
                patches = isolation.materialize_patches(cfg, arm_name, sdir, base_url, sess, store)
                prompt = prompting.render(t)
                if t["grader"] == "polyglot_javascript":
                    if docker:
                        if os.name != "nt":
                            os.symlink("/opt/nodekit/node_modules", os.path.join(wdir, "node_modules"))
                    else:
                        cache, _ = grade.js_cache(os.path.join(tdir, "workspace", "package.json"))
                        isolation.link_node_modules(wdir, os.path.join(cache, "node_modules"))
                cmd = isolation.docker_cmd(cfg, rid, prof, wdir, sdir, patches, env, prompt) if docker else \
                    dsh + ["--profile", prof] + sum((["--patch", p] for p in patches), []) + ["--json", prompt]
                # 臂自己的覆盖层（例如 v3 规则原文）是对照臂的“处理”本身，按插件原样交给模型，不参与拦截；公共层和本次运行层要查
                n_common = len(cfg.get("common_patches", []))
                leaks = leakcheck.filter_allowed(tid, preflight(prompt, wdir, env, cmd, patches[:n_common] + patches[-1:]), leakcheck.load_allow())
                ev_path, err_path = os.path.join(ldir, "events.jsonl"), os.path.join(ldir, "stderr.txt")
                t0 = time.time(); tag(proxy_port, rid)
                if leaks:
                    status = "leak_blocked"
                    json.dump(leaks, open(os.path.join(ldir, "preflight_leaks.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
                    log(f"  ✘ 泄漏自检命中，跳过：{tid} → {leaks[0]['where']} “{leaks[0]['term']}”（详见 _logs/{rid}/preflight_leaks.json）")
                elif dry_run:
                    status = "dry"
                else:
                    # v1.9.2：供应商抖动（503/429/账户池耗尽）不是这一臂答不出来，不能当失败样本。
                    # 先按 provider_retry 退避重试（每次重来都换一份干净工作目录），仍失败才记 provider_error。
                    max_retry = int(cfg.get("provider_retry", 3)); backoff = [30, 90, 180]
                    while True:
                        gate_wait()  # 熔断期间在这里排队，不白白烧运行
                        status = run_proc(cmd, wdir, None if docker else env, t.get("timeout_s", 1200), ev_path, err_path,
                                          on_timeout=(lambda: isolation.docker_kill(rid)) if docker else None)
                        if status != "exit1":
                            break
                        ptext = read_tail(err_path, 2000) + read_tail(ev_path, 2000)
                        if not PROV_RX.search(ptext):
                            break
                        if retried >= max_retry or (stop is not None and stop.is_set()):
                            status = "provider_error"
                            prov_code = prov_code_of(ptext)
                            prov_msg = next((ln.strip() for ln in ptext.splitlines() if PROV_RX.search(ln)), "")[:200]
                            break
                        retried += 1
                        wait_s = backoff[min(retried - 1, len(backoff) - 1)] * (0.8 + 0.4 * random.random())
                        prov_code = prov_code_of(ptext)
                        log(f"  ! [{arm_name} r{rep}] {tid:40s} 供应商不可用（{prov_code or '?'}），{wait_s:.0f}s 后第 {retried}/{max_retry} 次重试")
                        t_sleep(wait_s, stop)
                        shutil.rmtree(wdir, ignore_errors=True)
                        shutil.copytree(os.path.join(tdir, "workspace"), wdir)
                        for _p in (ev_path, err_path):
                            try: open(_p, "w").close()
                            except OSError: pass
                        gate_done()   # 重试会重新 gate_wait：不先还许可，每次供应商重试都永久泄漏一个并发位
                wall = time.time() - t0; tag(proxy_port, None)
                isolation.unlink_node_modules(wdir)  # 先摘掉预装库的链接，判分器自己会挂
                # 没真正跑过的（启动前被拦 / dry-run / 供应商不可用）不判分：否则会以“空答卷判错”的名义混进正确率
                graded = (status in ("ok", "timeout") or str(status).startswith("exit")) and status not in ("provider_error", "interrupted")
                if deferred:  # v1.9：这里不评分；答案在整个作答阶段都不会被解出来
                    g = {"pass": None, "frac": None, "detail": {"pending": True} if graded else {"skipped": status}}
                else:
                    g = grade.grade(tdir, wdir) if graded else {"pass": False, "frac": 0.0, "detail": {"skipped": status}}
                ev = events_summary(ev_path)
                for sub_ in ("s", "t"):  # 会话记录移到 runner 侧日志，工作区外的临时状态删掉
                    if os.path.isdir(os.path.join(sdir, sub_)):
                        shutil.move(os.path.join(sdir, sub_), os.path.join(ldir, "session" if sub_ == "s" else "storage"))
                shutil.rmtree(sdir, ignore_errors=True)
                kw = cfg.get("keep_workdirs", "move")
                kw = {True: "move", False: "keep", None: "move"}.get(kw, kw) if not isinstance(kw, str) else kw  # v1.7.9：true 曾被当成“留在原地”
                if deferred:  # v1.9：答卷要留给评分阶段，只能挪走，不能删也不能留在原地（留在原地会被后面的运行看到）
                    kw = "move"
                if kw == "move":  # 跑完把工作目录挪到 runner 侧，免得被后面的运行在隔壁目录看到
                    shutil.move(wdir, os.path.join(ldir, "work"))
                elif kw == "delete":
                    shutil.rmtree(wdir, ignore_errors=True)
                # 事后筛查：碰到别的运行的工作目录/状态目录（随机 ID 不是自己）也算疑似偷看
                sib = re.compile("(?:%s)[\\\\/](?!%s)[0-9a-f]{12}" % ("|".join(re.escape(x) for x in {wroot, wroot.replace("\\", "/"), sroot, sroot.replace("\\", "/")}), rid), re.I)
                text = ""
                for pth in [ev_path, err_path] + glob.glob(os.path.join(ldir, "session", "**", "*"), recursive=True):
                    try:
                        if os.path.isfile(pth):
                            text += open(pth, encoding="utf-8", errors="replace").read()[-2_000_000:]
                    except OSError:
                        pass
                rec = {"arm": arm_name, "task_id": tid, "rep": rep, "rid": rid, "dimension": t["dimension"], "source": t["source"],
                       "stratum": t.get("stratum"), "role": t.get("role"), "status": status, "wall_s": round(wall, 1),
                       "graded": graded, "pass": g["pass"], "frac": g["frac"], "detail": g["detail"], "tag": rid, "ts": t0, "isolation": mode, "host": isolation.host_platform(cfg),
                       "events": ev, "leak_suspect": bool(LEAK.search(text) or sib.search(text)),
                       "retried": retried, "provider_status": prov_code, "provider_msg": prov_msg}
                with open(res_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                gate_note(status != "provider_error")  # v1.9.2：供应商错误率过高时熔断器会降并发
                n += 1
                mark = ("·" if graded else "✘") if deferred else ("✔" if g["pass"] else "✘")  # · = 已作答，待评分
                log(f"[{arm_name} r{rep}] {tid:40s} {mark} {wall:6.0f}s {status}")
            except Exception as ex:  # 单题异常：记一条 crash，继续跑下一题；不静默停机，也不假装成功
                if wdir and os.path.isdir(wdir):
                    try:
                        isolation.unlink_node_modules(wdir)
                    except Exception:
                        pass
                    try:  # 留给事后排查（判分器崩溃时，模型的产出就是证据），不留在工作区旁边
                        if ldir:
                            os.makedirs(ldir, exist_ok=True); shutil.move(wdir, os.path.join(ldir, "work"))
                    except Exception:
                        pass
                    shutil.rmtree(wdir, ignore_errors=True)
                try:
                    tag(proxy_port, None)  # v1.7：出错时把代理的标签摘掉，免得后续零散请求记到这次运行头上
                    if sdir:
                        shutil.rmtree(sdir, ignore_errors=True)
                except Exception:
                    pass
                rid = rid or isolation.new_id()
                rec = {"arm": arm_name, "task_id": tid, "rep": rep, "rid": rid, "dimension": t.get("dimension"),
                       "source": t.get("source"), "stratum": t.get("stratum"), "role": t.get("role"),
                       "status": "crash", "wall_s": 0.0, "graded": False, "pass": False, "frac": 0.0,
                       "detail": {"error": repr(ex)[:300], "traceback": traceback.format_exc()[-1500:]},
                       "tag": rid, "ts": time.time(), "isolation": mode, "host": isolation.host_platform(cfg), "events": {}, "leak_suspect": False,
                       "retried": retried, "provider_status": None, "provider_msg": ""}
                try:
                    with open(res_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except OSError:
                    pass
                n += 1
                log(f"[{arm_name} r{rep}] {tid:40s} ✘ CRASH {type(ex).__name__}: {str(ex)[:120]}")
            finally:
                gate_done()  # v1.9.2：无论这道题成功、失败还是崩了，都要把并发许可还回去
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True); ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--tasks", default=os.path.join(ROOT, "tasks"))
    ap.add_argument("--split", default="main", choices=["main", "reserve", "pool", "all"])
    ap.add_argument("--reps", type=int, nargs="+", default=[0], help="要跑的重复号，例如 --reps 0 1 2")
    ap.add_argument("--out", default=os.path.join(ROOT, "runs")); ap.add_argument("--only", nargs="*")
    ap.add_argument("--proxy-port", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true", help="不调 DSH，只检查流程")
    a = ap.parse_args()
    cfg = load_cfg(a.config)
    run_slot(cfg, a.arm, a.reps, a.split, a.tasks, a.out, a.proxy_port, a.only, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
