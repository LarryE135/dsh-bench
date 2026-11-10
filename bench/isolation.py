#!/usr/bin/env python3
"""隔离层：让被测 AI 只看到“一个普通任务”，看不到任何测试元信息，也尽量碰不到网络和答案。

两种模式（config.json 的 "isolation"）：
  env    —— 默认，Windows/macOS/Linux 都能用。软隔离 + 能做到的硬限制：
            中性随机目录、全新会话目录、专用 DSH_HOME、清洗过的环境变量、指向死端口的代理变量（pip/npm/curl/git 等联网会失败）、
            覆盖层改名去注释、启动前泄漏自检。读权限无法限制（DSH 的沙箱只管写；Windows 上读也不受限）。
  docker —— 严格模式（需要 Docker）。DSH 跑在容器里：只挂载本题工作目录、本臂 profile 和本次会话目录；
            容器只接入一个 internal 网络，网络里唯一能连的是记账代理容器，所以除了模型接口什么都连不上；
            测试包、答案、其它题完全不在容器里。注意：容器里是 Linux，ops 臂（Windows 工具层插件）测到的会是它的 Linux 行为。

所有名字都是中性的：工作目录 = <临时目录>/w/<12 位随机 ID>，profile = p + 6 位随机十六进制，
覆盖层文件 = 1.yml、2.yml……。随机 ID → (臂, 题, 第几次) 的映射只写在 runner 侧的 runs/_map.jsonl。
"""
import json, os, re, secrets, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEAD = "http://127.0.0.1:9"  # 端口 9（discard）上没有服务：走代理的联网请求会立刻失败
NET_BLOCK_ENV = {
    "HTTP_PROXY": DEAD, "HTTPS_PROXY": DEAD, "ALL_PROXY": DEAD, "http_proxy": DEAD, "https_proxy": DEAD, "all_proxy": DEAD,
    "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",  # 模型接口走本机代理，不受影响
    "PIP_NO_INDEX": "1", "PIP_INDEX_URL": DEAD + "/", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "NPM_CONFIG_OFFLINE": "true", "NPM_CONFIG_REGISTRY": DEAD + "/", "YARN_ENABLE_NETWORK": "0",
    "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0", "UV_OFFLINE": "1",
}
KEEP_ENV = {"DEEPSEEK_API_KEY", "WB_PROXY_API_KEY"}  # v1.7.2：走 WB Proxy 时用它鉴权


def host_platform(cfg=None):
    """v1.8：记录这次运行在哪个平台上跑（windows / wsl / linux / darwin）。
    同一个模型在 DSH 桌面端（Windows）和 WSL 上的编程题得分可能差很多（shell、编码、工具链都不同），
    所以平台是实验条件，不同平台的结果不能混在一起做配对比较。config 里 platform_label 可手动覆盖。"""
    if cfg and cfg.get("platform_label"):
        return str(cfg["platform_label"])
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        try:
            if "microsoft" in open("/proc/sys/kernel/osrelease", encoding="utf-8").read().lower():
                return "wsl"
        except OSError:
            pass
        return "linux"
    return sys.platform


def new_id():
    return secrets.token_hex(6)


def work_root(cfg):
    return os.path.expanduser(cfg.get("work_root") or os.path.join(tempfile.gettempdir(), "w"))


def state_root(cfg):
    """本次运行的会话/存储目录树。刻意**不**与工作目录共父目录：env 模式读权限不受限，
    若和 <临时目录>/w/<ID> 并排，agent 顺手 ls .. 就能看到别的运行的会话记录。"""
    base = cfg.get("state_root") or os.path.join("~", ".local", "share", "wh-state")
    return os.path.expanduser(base)


def dsh_home(cfg, docker=False):
    base = os.path.expanduser(cfg.get("dsh_home") or os.path.join("~", ".local", "share", "wh"))
    return base + ("-linux" if docker else "")


def profiles_file():
    return os.path.join(ROOT, "runs", "_setup", "profiles.json")


def profile_of(cfg, arm):
    """臂 → 中性 profile 名。config 里写了就用写的（不建议），否则用 setup 时随机生成并保存的名字。"""
    if cfg["arms"][arm].get("profile"):
        return cfg["arms"][arm]["profile"]
    m = json.load(open(profiles_file(), encoding="utf-8")) if os.path.exists(profiles_file()) else {}
    if arm not in m:
        raise SystemExit("还没有为各臂建 profile：请先运行 bench.py setup")
    return m[arm]


def assign_profiles(cfg):
    path = profiles_file(); os.makedirs(os.path.dirname(path), exist_ok=True)
    m = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    for arm in cfg["arms"]:
        m.setdefault(arm, "p" + secrets.token_hex(3))
    json.dump(m, open(path, "w", encoding="utf-8"), indent=1)
    return m


def _strip_comments(text):
    return "\n".join(l for l in text.splitlines() if not l.startswith("#")).strip() + "\n"


def _yq(p):
    return "'" + p.replace("\\", "/").replace("'", "''") + "'"


def materialize_patches(cfg, arm, sdir, base_url, sess_dir, store_dir):
    """把覆盖层复制成中性文件名（去掉注释），再加一层“本次运行”覆盖层：模型请求指向记账代理、会话与存储写到本次专属目录。"""
    cdir = os.path.join(sdir, "c"); os.makedirs(cdir, exist_ok=True)
    srcs = [p if os.path.isabs(p) else os.path.join(ROOT, p) for p in cfg.get("common_patches", []) + cfg["arms"][arm].get("patches", [])]
    out = []
    for i, p in enumerate(srcs, 1):
        dst = os.path.join(cdir, f"{i}.yml"); open(dst, "w", encoding="utf-8").write(_strip_comments(open(p, encoding="utf-8").read()))
        out.append(dst)
    run = os.path.join(cdir, f"{len(srcs) + 1}.yml")
    if (cfg.get("llm_provider") or "deepseek") == "wb":  # v1.7.2：OpenAI 兼容网关（WB Proxy）路线
        model = cfg.get("model") or "deepseek-v4.1-flash"
        open(run, "w", encoding="utf-8").write(
            f"- id: llm-pi-ai\n  config:\n    providers:\n      wb-proxy:\n        displayName: WB Proxy\n"
            f"        apiKeyEnv: WB_PROXY_API_KEY\n        api: openai-completions\n"
            f"        baseURL: {base_url}/v1\n        defaultInput: [text]\n        models:\n"
            f"          - id: {model}\n            name: {model}\n            contextWindow: 400000\n"
            f"            maxTokens: 65536\n            input: [text]\n"
            f"- id: agent-default-model\n  config:\n    provider: wb-proxy\n    model: {model}\n"
            f"- id: session-persistence-jsonl\n  config:\n    root: {_yq(sess_dir)}\n"
            f"- id: storage-json\n  config:\n    root: {_yq(store_dir)}\n")
        return out + [run]
    # 官方串：显式把 agent-default-model 也写进来。否则 overlays/common.patch.yml 里写死的
    # provider/model 会赢，config.model 被静默忽略（报告里的模型名就变假了）。
    open(run, "w", encoding="utf-8").write(
        f"- id: llm-deepseek\n  config:\n    baseURL: {base_url}\n"
        f"- id: agent-default-model\n  config:\n    provider: deepseek-official\n    model: {cfg.get('model') or 'deepseek-flash'}\n"
        f"- id: session-persistence-jsonl\n  config:\n    root: {_yq(sess_dir)}\n"
        f"- id: storage-json\n  config:\n    root: {_yq(store_dir)}\n")
    return out + [run]  # v1.7.2 修：splice 时漏了这一行，deepseek 路线会返回 None


def base_url(cfg, port, docker):
    # 模型请求先经过本机记账代理：deepseek 走 Anthropic 兼容路径，wb 走 OpenAI 兼容路径（v1.7.2）
    host = f"px{port}:8080" if docker else f"127.0.0.1:{port}"
    if (cfg.get("llm_provider") or "deepseek") == "wb":
        return f"http://{host}"
    return f"http://{host}/anthropic"


def clean_env(cfg, home, rx):
    """从当前环境出发：去掉名字或值里带测试关键词的变量、PATH 里带关键词的条目；再加上断网变量和 DSH 运行变量。"""
    env = {}
    for k, v in os.environ.items():
        if k in KEEP_ENV:
            env[k] = v; continue
        if k.upper() == "PATH":
            env[k] = os.pathsep.join(x for x in v.split(os.pathsep) if not rx.search(x)); continue
        if rx.search(k) or rx.search(v):
            continue
        env[k] = v
    if cfg.get("block_network_env", True):
        env.update(NET_BLOCK_ENV)
    # 官方串（deepseek）与 WB 串不能混：走 wb 时若环境里还留着 DEEPSEEK_API_KEY，DSH 会拿它走 wb 路由 → 401 AUTH。
    if (cfg.get("llm_provider") or "deepseek") == "wb":
        env.pop("DEEPSEEK_API_KEY", None)
    env["DSH_HOME"] = home
    env["DSH_PERMISSION_MODE"] = cfg.get("permission_mode", "danger-full-access")
    env["DSH_TELEMETRY_MODE"] = "DISABLED"
    env.update({k: str(v) for k, v in (cfg.get("env") or {}).items()})
    for k, v in load_credentials(home).items():   # v1.7.6：凭据文件里的 key 直接进子进程环境
        env.setdefault(k, v)                      # 否则 dsh 会以 MISSING_CREDENTIAL 退出（报错还很绕）
    return env


# 只注入「本批实验真正需要的」密钥。DEEPSEEK_API_KEY（官方渠道）绝不能注入：
# 它一旦出现在环境里，DSH 会拿它去走 wb-proxy 路由，代理再转发上游 → 401 AUTH（实测踩过）。
CRED_NAMES = ("WB_PROXY_API_KEY", "HF_TOKEN")


def load_credentials(home):
    """从专用 DSH_HOME 的 .credentials.yaml / .env 里取凭据（只取 CRED_NAMES，不打印、不落盘）。"""
    import re as _re
    got = {}
    pats = ((".credentials.yaml", r"^\s*([A-Za-z0-9_]+)\s*:\s*['\"]?([^'\"\s#]+)"),
            (".env", r"^\s*(?:export\s+)?([A-Za-z0-9_]+)\s*=\s*['\"]?([^'\"\s#]+)"))
    for fn, pat in pats:
        p = os.path.join(home, fn)
        if not os.path.exists(p):
            continue
        try:
            for line in open(p, encoding="utf-8", errors="replace"):
                m = _re.match(pat, line)
                if m and m.group(1) in CRED_NAMES and m.group(2):
                    got.setdefault(m.group(1), m.group(2))
        except Exception:
            continue
    return got


def link_node_modules(wdir, src):
    """JS 题：把共享的 jest/babel 挂进工作目录，模型在断网时也能自己跑测试（各臂一样）。"""
    import grade
    dst = os.path.join(wdir, "node_modules")
    if not os.path.exists(dst):
        grade._link_dir(src, dst)
    return dst


def unlink_node_modules(wdir):
    import grade
    p = os.path.join(wdir, "node_modules")
    if os.path.islink(p) or (os.name == "nt" and os.path.isdir(p) and _is_junction(p)):
        grade._unlink_dir(p)


def _is_junction(p):
    try:
        return bool(os.lstat(p).st_file_attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except Exception:
        return False


# ------------------------------------------------------------------ docker 严格模式
IMAGE = "localhost/wk-agent:1"
NET = "wk-int"
C_WORK, C_HOME, C_STATE = "/srv/work", "/srv/h", "/srv/x"


def docker(*args, timeout=600, check=False):
    p = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and p.returncode != 0:
        raise SystemExit(f"docker {' '.join(args[:3])} 失败：{(p.stderr or p.stdout)[-600:]}")
    return p


def docker_build(cfg):
    ver = cfg.get("dsh_version_pin", "")
    ver = ver if ver and not ver.startswith("填") else "latest"
    docker("build", "-t", IMAGE, "--build-arg", f"DSH_VERSION={ver}", os.path.join(ROOT, "docker"), timeout=3600, check=True)
    if docker("network", "inspect", NET).returncode != 0:
        docker("network", "create", "--internal", NET, check=True)  # internal：这个网络里的容器出不了外网


def docker_user():
    return [] if os.name == "nt" else ["--user", f"{os.getuid()}:{os.getgid()}"]


def docker_setup_profile(cfg, prof, plugins):
    """在一个“有网”的一次性容器里建 profile、装插件（装插件需要下载）；之后跑题的容器都没有外网。"""
    home = dsh_home(cfg, docker=True); os.makedirs(home, exist_ok=True)
    cmd = f"dsh --profile {prof} --from-default-profile headless --dump-config >/dev/null"
    for pl in plugins:
        cmd += f" && dsh plugin --profile {prof} add {pl}"
    return docker("run", "--rm", *docker_user(), "-e", f"DSH_HOME={C_HOME}", "-e", "HOME=/tmp/u", "--tmpfs", "/tmp",
                  "-v", f"{home}:{C_HOME}", IMAGE, "sh", "-c", cmd, timeout=1800)


def docker_start_proxy(port, log_dir, log_name, upstream, inject_usage=False):
    """记账代理容器：同时接入默认网络（出外网）和 internal 网络（给 DSH 容器用，别名 px<端口>）；端口发布到本机，runner 用它打标签。"""
    name = f"wk-px-{port}"
    docker("rm", "-f", name)
    docker("run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:8080", "-v", f"{HERE}:/app:ro", "-v", f"{log_dir}:/logs",
           "--entrypoint", "python3", IMAGE, "/app/usage_proxy.py", "--port", "8080", "--bind", "0.0.0.0",
           "--log", f"/logs/{log_name}", "--upstream", upstream, *(["--inject-usage"] if inject_usage else []), check=True)
    docker("network", "connect", "--alias", f"px{port}", NET, name, check=True)
    return name


def docker_stop_proxy(name):
    docker("rm", "-f", name)


def docker_cmd(cfg, rid, prof, wdir, sdir, patches, env, prompt):
    """把本机路径换成容器内路径，拼出 docker run 命令。容器内只有：/srv/work（本题）、/srv/h（profile）、/srv/x（本次会话与覆盖层）。"""
    cpatches = [C_STATE + "/c/" + os.path.basename(p) for p in patches]
    envs = []
    # WB_PROXY_API_KEY 必须在白名单里：docker 模式默认走 wb 路由，漏了它容器里没有任何凭据，
    # 每条都是 MISSING_CREDENTIAL/401 ——「严格隔离」等于跑不成（第七轮审计 P0-2）。
    for k in list(NET_BLOCK_ENV) + ["WB_PROXY_API_KEY", "DEEPSEEK_API_KEY", "DSH_PERMISSION_MODE", "DSH_TELEMETRY_MODE"] + list((cfg.get("env") or {})):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            continue  # v1.7：容器网络本身是 --internal（真断网）；再塞死代理反而可能把发往 px<端口> 记账代理的请求也带进死代理
        if k == "DEEPSEEK_API_KEY" and (cfg.get("llm_provider") or "deepseek") == "wb":
            continue  # 走 wb 时不要带官方 key（会被 DSH 拿去走 wb 路由 → 401）
        if k == "DEEPSEEK_API_KEY":
            # v1.7：只写变量名，值从 docker 客户端进程继承，不出现在命令行（ps/docker inspect 可见）里
            v = (cfg.get("env") or {}).get(k)  # v1.7.1：若 key 只写在 config 的 env 里，继承不到，只能显式传值
            envs += ["-e", f"{k}={v}"] if v else ["-e", k]
            continue
        if k in env:
            envs += ["-e", f"{k}={env[k]}"]
    cmd = ["docker", "run", "--rm", "--name", f"wk-{rid}", "--network", NET, *docker_user(),
           "--tmpfs", "/tmp", "-e", "HOME=/tmp/u", "-e", f"DSH_HOME={C_HOME}", *envs,
           "-v", f"{wdir}:{C_WORK}", "-v", f"{dsh_home(cfg, docker=True)}:{C_HOME}", "-v", f"{sdir}:{C_STATE}",
           "-w", C_WORK, IMAGE, "dsh", "--profile", prof]
    for p in cpatches:
        cmd += ["--patch", p]
    return cmd + ["--json", prompt]


def docker_kill(rid):
    docker("kill", f"wk-{rid}", timeout=60)