#!/usr/bin/env python3
"""隐藏答案打包：把每道题的 hidden/（标准答案、隐藏测试、金标准表格）合进一个混淆过的 tasks/_answers.bin。

为什么：DSH 以 danger-full-access 运行时，agent 能读整台电脑。答案若以明文放在 tasks/<题>/hidden/，
它“顺手 grep 一下”就能看到。打包 + 简单异或混淆后，grep/unzip 都找不到；判分时才临时解出一题的答案，判完立即删除。
这只是防“顺手看到”，不是加密；真想作弊的人读本文件就能解开。

用法：python answers.py pack   <tasks_dir>    # 把各题 hidden/ 收进 _answers.bin 并删除 hidden/
      python answers.py unpack <tasks_dir>    # 反过来（维护题目时用）
"""
import contextlib, io, json, os, shutil, sys, tempfile, zipfile

# 答案包默认随仓库发布（和 SWE-bench 把 test_patch 一起发是同一套逻辑：被测 agent 在 docker 隔离下
# 看不到这个包）。要让答案离开代码树：DSHB_ANSWERS 指向外部副本；要换混淆键：DSHB_KEY（换键后需重打包）。
KEY = (os.environ.get("DSHB_KEY") or "DSH-Bench-v1|20261103|not-a-secret").encode()
PACK = "_answers.bin"


def _xor(b):
    k = KEY; n = len(k)
    return bytes(x ^ k[i % n] for i, x in enumerate(b))


_CACHE = {}


def _load(tasks_dir):
    # 22.7 MB 的包整体 XOR 一次要 1.2 s（纯 Python 逐字节），而判分是「每题一次 view()」。
    # 660 次作答的复评原来要花约 13 分钟在解密上（审计 P3-1）。按 (路径, mtime, 大小) 缓存。
    p = os.environ.get("DSHB_ANSWERS") or os.path.join(tasks_dir, PACK)
    if not os.path.exists(p):
        return None
    st = os.stat(p)
    key = (os.path.abspath(p), st.st_mtime_ns, st.st_size)
    z = _CACHE.get(key)
    if z is None:
        try:
            z = zipfile.ZipFile(io.BytesIO(_xor(open(p, "rb").read())))
        except zipfile.BadZipFile:
            raise SystemExit("答案包解不开：DSHB_KEY 与打包时用的键不一致，或 _answers.bin 损坏。" + chr(10) +
                             "  用 `python bench/answers.py pack tasks` 按当前键重新打包（会覆盖原文件）。")
        _CACHE.clear()
        _CACHE[key] = z
    return z


def pack(tasks_dir):
    buf = io.BytesIO(); n = 0
    old = _load(tasks_dir)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if old:  # 保留已打包的题（增量加入新题，例如 GPQA）
            for info in old.infolist():
                tid = info.filename.split("/", 1)[0]
                if not os.path.isdir(os.path.join(tasks_dir, tid)):
                    continue  # 题已从磁盘删掉（例如查出许可问题）：答案也要一起消失
                if not os.path.isdir(os.path.join(tasks_dir, tid, "hidden")):
                    z.writestr(info, old.read(info))
        for tid in sorted(os.listdir(tasks_dir)):
            hid = os.path.join(tasks_dir, tid, "hidden")
            if not os.path.isdir(hid):
                continue
            for root, _, files in os.walk(hid):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, f"{tid}/" + os.path.relpath(full, hid).replace(os.sep, "/"))
            shutil.rmtree(hid); n += 1
    open(os.path.join(tasks_dir, PACK), "wb").write(_xor(buf.getvalue()))
    print(f"packed {n} tasks -> {PACK}")


def unpack(tasks_dir):
    z = _load(tasks_dir)
    for info in z.infolist():
        tid, rel = info.filename.split("/", 1)
        dst = os.path.join(tasks_dir, tid, "hidden", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True); open(dst, "wb").write(z.read(info))
    os.remove(os.path.join(tasks_dir, PACK))
    print("unpacked —— 注意：_answers.bin 已被删除、答案现在是 tasks/<题>/hidden/ 明文；"
          "改完题务必 python bench/answers.py pack <tasks_dir> 收回，否则会把答案一起发出去")


@contextlib.contextmanager
def view(tdir):
    """给判分器一个“带 hidden/ 的题目目录”。明文 hidden/ 已存在就直接用；否则临时解出这一题，用完删掉。"""
    tdir = os.path.abspath(tdir)
    if os.path.isdir(os.path.join(tdir, "hidden")):
        yield tdir; return
    z = _load(os.path.dirname(tdir)); tid = os.path.basename(tdir)
    # 权限收到 0700：判分用的明文 hidden/ 不能给同机其他用户看（同用户的被测代码仍能读，
    # 这一条只有容器/沙箱才挡得住，见 docs/审计-v1.0.0.md 的 known issue）
    _base = os.path.join(tempfile.gettempdir(), "dshb_grade")
    os.makedirs(_base, exist_ok=True)
    try:
        os.chmod(_base, 0o700)
    except OSError:
        pass
    tmp = tempfile.mkdtemp(prefix="g_", dir=_base)
    try:
        shutil.copy(os.path.join(tdir, "task.json"), tmp)
        if os.path.isdir(os.path.join(tdir, "workspace")):  # 少数判分器要对照原始输入
            shutil.copytree(os.path.join(tdir, "workspace"), os.path.join(tmp, "workspace"))
        if z:
            for info in z.infolist():
                if info.filename.startswith(tid + "/"):
                    dst = os.path.join(tmp, "hidden", info.filename[len(tid) + 1:])
                    os.makedirs(os.path.dirname(dst), exist_ok=True); open(dst, "wb").write(z.read(info))
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法：python answers.py pack|unpack <tasks_dir>")
        sys.exit(2)
    {"pack": pack, "unpack": unpack}[sys.argv[1]](sys.argv[2])
