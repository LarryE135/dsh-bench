#!/usr/bin/env python3
"""模拟 dsh 命令行（只在演练里用）：不启动真模型，但把 runner 依赖的每条契约都做出来。

  --version                                       打印版本
  --profile P --from-default-profile headless --dump-config   建 profile（DSH_HOME/profiles/P）并打印合成配置
  plugin --profile P add NAME                     把插件写进 profile 的 package.json
  --profile P --patch f... --json "<prompt>"      读覆盖层里的 baseURL 发一次请求，回写 --json 事件流，
                                                  并按题目类型把答案写进当前目录

演练用环境变量（runner 会清洗环境变量，所以这些都通过命令行传）：
  --repo <演练根目录> --mode right|wrong|mixed
"""
import hashlib, json, os, random, re, sys, urllib.request

OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def args_of(argv):
    o = {"patches": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--patch":
            o["patches"].append(argv[i + 1]); i += 2; continue
        if a in ("--profile", "--from-default-profile", "--json", "--repo", "--mode", "--plugin"):
            o[a.lstrip("-").replace("-", "_")] = argv[i + 1]; i += 2; continue
        if a.startswith("--"):
            o[a.lstrip("-").replace("-", "_")] = True; i += 1; continue
        o.setdefault("_pos", []).append(a); i += 1
    return o


def home_profiles():
    return os.path.join(os.environ.get("DSH_HOME", os.path.expanduser("~/.dsh")), "profiles")


def write_profile(prof, add_plugin=None):
    d = os.path.join(home_profiles(), prof); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "package.json")
    meta = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {"name": prof, "dependencies": {}}
    meta.setdefault("dependencies", {})
    if add_plugin:
        meta["dependencies"][add_plugin] = "0.2.5"
    json.dump(meta, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return meta


def merged_config(o):
    """把覆盖层拼成“合成配置”：真 dsh 打印的是合成后的 YAML，这里等价地把各层内容连起来。"""
    meta = write_profile(o.get("profile") or "default")
    out = ["# mock synthesized config (profile=%s)" % (o.get("profile") or "default"),
           json.dumps(meta, ensure_ascii=False)]
    for p in o["patches"]:
        out.append("# ---- patch: %s" % os.path.basename(p))
        out.append(open(p, encoding="utf-8").read())
    return "\n".join(out)


def base_url(o):
    for p in o["patches"]:
        m = re.search(r"baseURL:\s*(\S+)", open(p, encoding="utf-8").read())
        if m:
            return m.group(1).strip("'\"")
    return None


def find_task(repo, prompt):
    sys.path.insert(0, os.path.join(repo, "bench"))
    import prompting
    for tid in sorted(os.listdir(os.path.join(repo, "tasks"))):
        tp = os.path.join(repo, "tasks", tid, "task.json")
        if not os.path.isfile(tp):
            continue
        t = json.load(open(tp, encoding="utf-8"))
        if prompting.render(t) == prompt:
            return t
    return None


def write_answer(repo, task, mode):
    """按模式伪造一份答卷：right 全对、wrong 全错、mixed 按题目哈希分成易/中/难三档。
    标准答案由 selftest.oracle() 现造（和判分自检同一套逻辑），所以 mcq/整数/全部自建题型都能造出来。"""
    if task is None:
        return
    tid = task["task_id"]
    h = int(hashlib.sha256(tid.encode()).hexdigest(), 16)
    if mode == "wrong":
        right = False
    elif mode == "right":
        right = True
    else:
        b = h % 10  # 70% 稳定做对、20% 时对时错、10% 稳定做错（接近真实难度分布）
        right = b < 7 or (b in (7, 8) and random.random() < 0.5)
    if not right:
        return
    sys.path.insert(0, os.path.join(repo, "bench"))
    import answers, selftest
    with answers.view(os.path.join(repo, "tasks", tid)) as v:
        selftest.oracle(json.load(open(os.path.join(v, "task.json"), encoding="utf-8")), v, os.getcwd())


def call_model(url, prompt):
    body = json.dumps({"model": "deepseek-flash", "max_tokens": 512, "stream": True,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    usage = {}
    with OPEN.open(req, timeout=120) as r:
        for line in r:
            line = line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:])
            except Exception:
                continue
            u = (ev.get("message") or {}).get("usage") or ev.get("usage") or {}
            for k, v in u.items():
                if isinstance(v, int):
                    usage[k] = v
    return usage


def main():
    a = args_of(sys.argv[1:])
    if a.get("version"):
        print("mock-dsh 0.2.1-rc.5"); return 0
    pos = a.get("_pos") or []
    if pos and pos[0] == "plugin":  # dsh plugin --profile P add NAME
        prof = a.get("profile"); name = pos[-1]
        write_profile(prof, add_plugin=name); print(f"added {name} to {prof}"); return 0
    if a.get("dump_config"):
        print(merged_config(a)); return 0
    prompt = a.get("json")
    if not prompt:
        print("mock-dsh: nothing to do", file=sys.stderr); return 2
    repo = a.get("repo") or "."
    url = base_url(a)
    ev = [{"type": "status", "ts": 0}]
    if url:
        try:
            u = call_model(url, prompt)
        except Exception as ex:
            print(json.dumps({"type": "error", "error": repr(ex)[:200]})); return 1
        ev.append({"type": "status", "ts": 1})
        ev.append({"type": "final", "usage": {"inputTokens": u.get("input_tokens", 0),
                                              "cacheReadTokens": u.get("cache_read_input_tokens", 0),
                                              "outputTokens": u.get("output_tokens", 0),
                                              "totalTokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)}})
    for e in ev:
        print(json.dumps(e, ensure_ascii=False))
    write_answer(repo, find_task(repo, prompt), a.get("mode") or "mixed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
