#!/usr/bin/env python3
"""预设保真检查：某臂声称对齐的 agent preset，与它实际合成出来的 headless 配置逐行比对。

为什么需要它：预设（preset）只有 Web/桌面端能选中，headless 跑的是覆盖层翻译版本。翻译一旦漏行
（例如 std 臂漏了 tool-subagent 的 modelSelection/modelDiscovery，或 ptc 臂漏了停用 tool-workflow），
配置看起来一切正常、金丝雀也全绿，但那一臂已经不是它自称的配置了。这个脚本把"自称"和"实际"钉在一起。

怎么比：
  1. 取预设定义：优先用现网 profile 的 dump（默认 --profile web，因为它同时含 preset-standard/ptc/minimal/
     cordis 与四个 flash 预设；desktop profile 实测 dump 失败），也支持直接给 --preset-file。
  2. 取实际配置：该臂 --dump-config 的合成结果（setup 会写 runs/_setup/<臂>.dump.txt）。
  3. 逐行分类：
       aligned            —— 预设行与合成配置一致
       translated         —— 预设行的等价表达（persona → system-prompt，tool-presentation → 执行器开关）
       absent-in-headless —— 预设里有、headless base 没有（web 专用插件），只能记录
       allowed            —— 已知且写明理由的差异（见 DEFAULT_ALLOW）
       mismatch           —— 存在、不同、且没人管：**非零退出**，禁止开跑

用法：
  python preset_fidelity.py --arm std --preset standard --dump ../runs/_setup/std.dump.txt
  python preset_fidelity.py --arm v3 --preset flash-lean-ptc-v3 --preset-file <presets/flash-lean-ptc-v3.patch.yml> --dump ...
"""
import argparse, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import yaml  # noqa: E402


class L(yaml.SafeLoader):
    pass


L.add_constructor("tag:yaml.org,2002:js", lambda loader, node: "JS:" + loader.construct_scalar(node))

# 预设行 → headless 里的等价表达（不按同名行比）
EQUIV = {
    "persona": "system-prompt.personaSuffix / personaPrefix",
    "tool-presentation": "tool-executor-native/ptc 开关",
    "tool-executor-ptc": "tool-executor-native/ptc 开关",
    "tool-executor-native": "tool-executor-native/ptc 开关",
}

# 已知且写明理由的差异：不算 mismatch，但会打印出来
DEFAULT_ALLOW = {
    "tool-web": "各臂统一关闭联网工具（overlays/common.patch.yml，实验控制条件）",
    "tool-plugin-manager": "headless 无图形界面，插件市场管理器在 headless 出厂即停用（与 Web/桌面端不同，不影响任务）",
    "plan-mode": "预设 section 比 headless 出厂旧两处措辞，且只在计划模式生效；评测不进入计划模式",
}

# 出厂 system-prompt.personaPrefix（dsh 0.2.2-rc.1）。dump 只在"非默认"时打印 personaPrefix，
# 所以预设显式写了它、而 dump 没有 → 说明两者相同，属对齐而不是差异。
FACTORY_PERSONA_PREFIX = "You are a coding agent powered by the {{model}} model."

WEB_ONLY = {"time-context", "tool-schedule", "tool-ask-user", "present", "tool-plugin-market",
            "tool-ralph", "tool-subagent-codex", "tool-subagent-claude-code"}


def split_rows(text):
    """按列 0 的 "- id: " 切顶层行（注意：不能 strip 后再判，否则把缩进的子行也切了）。"""
    out, cur, buf = {}, None, []
    for ln in text.splitlines():
        m = re.match(r"^- id: (\S+)$", ln)
        if m:
            if cur is not None:
                out[cur] = buf
            cur, buf = m.group(1), []
        elif ln.strip().startswith("--- stderr"):
            break
        elif cur is not None:
            buf.append(ln)
    if cur is not None:
        out[cur] = buf
    return out


def parse_entry(lines):
    try:
        doc = yaml.load("- id: X\n" + "\n".join(lines), Loader=L)
        return doc[0] if isinstance(doc, list) and doc else {}
    except Exception as ex:  # 解析不了就当成空行，交给上层报 mismatch
        return {"__parse_error__": str(ex)[:120]}


def eff_disabled(v):
    """预设里没写 disabled = 挂载（False）；写了 !!js 就按 win32 求值。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.startswith("JS:"):
        expr = v[3:].strip()
        if expr == "process.platform === 'win32'":
            return True
        if expr == "process.platform !== 'win32'":
            return False
    return False


def flat(items):
    for it in items:
        if it.get("name") == "cordis:group" and isinstance(it.get("config"), list):
            yield from flat(it["config"])
        else:
            yield it


def ptc_on(dump_text):
    def block(rid):
        rows = split_rows(dump_text)
        return "\n".join(rows.get(rid) or [])
    tp = block("tool-presentation")
    if tp and re.search(r"^\s+mode:\s*ptc\s*$", tp, re.M):
        return True
    return "disabled: true" in block("tool-executor-native") and "disabled: false" in block("tool-executor-ptc")


def presets_dump(cfg, path):
    """现网 profile 的 dump（含全部预设定义），带缓存。"""
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return open(path, encoding="utf-8").read()
    cmd = list(cfg.get("dsh_command") or ["dsh"]) + ["--profile", "web", "--dump-config"]
    env = dict(os.environ, **(cfg.get("env") or {}))
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=300)
    if p.returncode != 0 or len(p.stdout or "") < 1000:
        raise SystemExit("拿不到预设来源 dump（--profile web --dump-config 失败）：" + (p.stderr or "")[:300])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(p.stdout)
    return p.stdout


def find_preset_file(preset_id):
    """flash 预设由 dsh-flash-presets 插件随包提供，不在 web profile 的 dump 里（dump 只有 4 个内置预设）。
    到各 profile 的 node_modules 下找插件自带的 presets/<id>.patch.yml。"""
    import glob
    for pat in (os.path.expanduser("~/.dsh/profiles/*/node_modules/*/presets/"),
                os.path.expanduser("~/.local/share/wh/profiles/*/node_modules/*/presets/"),
                os.path.expanduser("~/.dsh/profiles/*/node_modules/*/presets/*/")):
        for d in glob.glob(pat):
            p = os.path.join(d, preset_id + ".patch.yml")
            if os.path.exists(p):
                return p
    return None


def preset_rows(preset_id, presets_text, preset_file):
    if not preset_file and presets_text and ("preset-" + preset_id) not in split_rows(presets_text):
        preset_file = find_preset_file(preset_id)   # flash 预设：回落到插件自带的预设文件
        if not preset_file:
            raise SystemExit(f"找不到预设 {preset_id}：既不在 profile dump 里，也没在 node_modules 下找到 presets/{preset_id}.patch.yml")
    if preset_file:
        doc = yaml.load(open(preset_file, encoding="utf-8"), Loader=L)
        cfg = next(x for blk in doc for x in blk.get("insert", []) if x.get("config", {}).get("id") == preset_id)["config"]
        return {r["id"]: r for r in flat(cfg["plugins"])}
    rows = split_rows(presets_text)
    key = "preset-" + preset_id
    if key not in rows:
        raise SystemExit(f"预设来源里没有 {key}（可用：" + ", ".join(k for k in rows if k.startswith("preset-")) + "）")
    cfg = parse_entry(rows[key]).get("config", {})
    return {r["id"]: r for r in flat(cfg.get("plugins", []))}


def compare(prows, dump_text, allow):
    drows = {rid: parse_entry(lines) for rid, lines in split_rows(dump_text).items()}
    buckets = {"aligned": [], "translated": [], "absent-in-headless": [], "allowed": [], "mismatch": []}
    ptc_checked = False
    for rid, pr in prows.items():
        if rid in EQUIV:
            if rid == "persona":
                suffix = (pr.get("config") or {}).get("suffix")
                sp = (drows.get("system-prompt") or {}).get("config") or {}
                ok = suffix is None or (sp.get("personaSuffix") or "").strip() == suffix.strip()
                prefix = (pr.get("config") or {}).get("prefix")
                ok_pre = prefix is None or (sp.get("personaPrefix") or FACTORY_PERSONA_PREFIX).strip() == prefix.strip()
                ok = ok and ok_pre
                buckets["translated" if ok else "mismatch"].append(
                    ("persona", EQUIV["persona"] + ("（prefix 与出厂默认相同，dump 省略）" if ok and prefix else "")
                     if ok else "personaSuffix/prefix 与预设不一致"))
                continue
            if not ptc_checked:
                ptc_checked = True
                buckets["aligned" if ptc_on(dump_text) else "mismatch"].append(
                    ("tool-presentation", "工具面已切 PTC" if ptc_on(dump_text) else "预设要求 PTC，实际不是"))
            continue
        dr = drows.get(rid)
        if dr is None:
            # 只有确认是「web 专用插件」的行才允许缺席；否则说明预设要求的这一行在合成配置里根本没有
            # —— 那正是「覆盖层被静默忽略 / 漏了一整行」的样子，必须判 mismatch（原来一律放行，等于门禁是空的）。
            if rid in WEB_ONLY:
                buckets["absent-in-headless"].append((rid, "headless base 没有这一行（web 专用插件）"))
            else:
                buckets["mismatch"].append((rid, "预设要求这一行，但合成配置里没有（覆盖层可能被忽略）"))
            continue
        diffs = []
        pdis = eff_disabled(pr.get("disabled"))
        hdis = eff_disabled(dr.get("disabled"))   # 合成配置里也可能留着 !!js（平台条件行），两侧都要按 win32 求值
        if pdis != hdis:
            diffs.append(f"disabled 预设={pdis} 实际={hdis}")
        pc = pr.get("config") if isinstance(pr.get("config"), dict) else {}
        hc = dr.get("config") if isinstance(dr.get("config"), dict) else {}
        for k, v in pc.items():
            if isinstance(v, (dict, list)):
                if hc.get(k) != v:
                    diffs.append(f"config.{k} 与预设不一致")
            elif k not in hc:
                diffs.append(f"缺 config.{k}（预设={v!r}）")
            elif hc.get(k) != v:
                diffs.append(f"config.{k} 预设={v!r} 实际={hc.get(k)!r}")
        if not diffs:
            buckets["aligned"].append((rid, "一致"))
        elif rid in allow:
            buckets["allowed"].append((rid, allow[rid] + "｜" + "; ".join(diffs)[:160]))
        else:
            buckets["mismatch"].append((rid, "; ".join(diffs)[:240]))
    return buckets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True)
    ap.add_argument("--preset", required=True, help="预设 id，如 standard / ptc / flash-lean-ptc-v3")
    ap.add_argument("--dump", required=True, help="该臂的 --dump-config 结果")
    ap.add_argument("--preset-file", default=None)
    ap.add_argument("--presets-dump", default=os.path.join(ROOT, "runs", "_setup", "presets.web.dump.txt"))
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    # 空/被截断的 dump 一律拒绝判定（P0-1）：必须在读预设之前拦，否则「预设来源也拿不到」时
    # 会先抛「找不到预设」，把「配置根本没合成出来」这个更要紧的事实盖掉（演练实测）。
    dump_text = open(a.dump, encoding="utf-8").read() if os.path.exists(a.dump) else ""
    if not dump_text.strip() or not split_rows(dump_text):
        print("dump 是空的或解析不出任何行：拒绝判定保真，先确认 --dump-config 真的跑通了")
        return 2
    cfg = json.load(open(a.config, encoding="utf-8")) if os.path.exists(a.config) else {}
    text = presets_dump(cfg, a.presets_dump) if not a.preset_file else ""
    prows = preset_rows(a.preset, text, a.preset_file)
    buckets = compare(prows, dump_text, DEFAULT_ALLOW)
    lines = [f"# 保真检查：臂 {a.arm} ↔ 预设 {a.preset}", f"# 预设行数 {len(prows)}；合成配置 {a.dump}", ""]
    for name, title in (("mismatch", "✘ MISMATCH（必须修，否则不许开跑）"), ("allowed", "! 已知差异（写明理由）"),
                        ("translated", "→ 等价翻译"), ("absent-in-headless", "· headless 口径缺失"), ("aligned", "✔ 一致")):
        if not buckets[name]:
            continue
        lines.append(f"== {title}：{len(buckets[name])}")
        for rid, msg in buckets[name]:
            lines.append(f"   {rid:26s} {msg}")
        lines.append("")
    out = "\n".join(lines)
    print(out)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(out + "\n")
    if a.json:
        print(json.dumps({k: [x[0] for x in v] for k, v in buckets.items()}, ensure_ascii=False))
    return 1 if buckets["mismatch"] else 0


if __name__ == "__main__":
    sys.exit(main())
