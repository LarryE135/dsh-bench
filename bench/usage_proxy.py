#!/usr/bin/env python3
"""记账代理：夹在 DSH 和 api.deepseek.com 之间，原样转发请求，顺手把每次调用的 usage 记下来。

为什么要它：不同插件/DSH 版本记日志的方式不一样，但 DeepSeek API 每次回包里的 usage 字段是统一的
（prompt_cache_hit_tokens 缓存命中、prompt_cache_miss_tokens 未命中、completion_tokens 输出）。
在网络这一层记账，四个对照臂用的是同一把尺子。

用法：python usage_proxy.py --port 8701 --log runs/usage_std.jsonl   （一般不用手动开：bench.py 会自动起停）
DSH 的 DeepSeek 适配器走 Anthropic Messages 协议，默认地址 https://api.deepseek.com/anthropic；
公共覆盖层 overlays/common.patch.yml 把它改成 http://127.0.0.1:<端口>/anthropic，请求就先经过这里。
runner 每跑一道题前会 POST /__tag 设置“当前题号”，日志里每行都带这个标签。
只用标准库，Windows/macOS/Linux 都能跑。一个臂一个端口、一个代理进程，臂内题目串行，所以标签不会串。
"""
import argparse, http.client, json, os, ssl, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LOCK = threading.Lock()


def extract_usage(obj, acc):
    """从一个 JSON 对象里找 usage（OpenAI 兼容格式 + Anthropic 格式都认），以及工具调用数。
    Anthropic 流式：message_start 事件的 usage 在 obj["message"]["usage"]（输入 token），
    message_delta 事件的 usage 在顶层（输出 token）。两处都要读。"""
    if isinstance(obj, dict) and isinstance(obj.get("message"), dict):
        if isinstance(obj["message"].get("model"), str):
            acc["resp_model"] = obj["message"]["model"]  # 服务端实际使用的模型名，用来核对版本
        if isinstance(obj["message"].get("usage"), dict):
            extract_usage({"usage": obj["message"]["usage"]}, acc)
    if isinstance(obj, dict) and isinstance(obj.get("model"), str) and "choices" in obj:
        acc["resp_model"] = obj["model"]
    u = obj.get("usage") if isinstance(obj, dict) else None
    if isinstance(u, dict):
        for k in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                  "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            if isinstance(u.get(k), int):
                acc[k] = u[k]  # 流式时最后一块才带完整 usage，取最后值
        st = u.get("server_tool_use") or {}
        if isinstance(st.get("web_search_requests"), int):
            acc["web_search_requests"] = st["web_search_requests"]  # 服务端联网搜索次数（可能按次计费）
        pd = u.get("prompt_tokens_details") or {}
        if isinstance(pd.get("cached_tokens"), int):
            acc["prompt_cache_hit_tokens"] = pd["cached_tokens"]  # v1.7.2：OpenAI 口径的缓存命中
        d = u.get("completion_tokens_details") or {}
        if isinstance(d.get("reasoning_tokens"), int):
            acc["reasoning_tokens"] = d["reasoning_tokens"]
    for ch in (obj.get("choices") or []) if isinstance(obj, dict) else []:
        tc = (ch.get("delta") or ch.get("message") or {}).get("tool_calls") or []
        for t in tc:
            acc.setdefault("_tool_idx", set()).add(t.get("index", t.get("id")))
    if isinstance(obj, dict) and obj.get("type") == "content_block_start" and (obj.get("content_block") or {}).get("type") == "tool_use":
        acc.setdefault("_tool_idx", set()).add(obj.get("index"))


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # v1.7.3：DSH 有时会主动断开（重试/取消），这里安静收场并计数，不再往日志里刷 traceback
        # v1.9.1：Windows 上客户端中途断开抛的是 ConnectionAbortedError(10053)，它和 ConnectionResetError 同属
        # ConnectionError 但互不继承；只写前者的结果是正式跑 1300+ 次时 stderr 被 traceback 刷屏（实测冒烟已复现）。
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError, OSError):
            self.close_connection = True; self.server.dropped = getattr(self.server, "dropped", 0) + 1

    def handle_error(self, *a):
        self.close_connection = True; self.server.dropped = getattr(self.server, "dropped", 0) + 1

    def do_POST(self):
        if self.path == "/__tag":
            n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or b"{}")
            self.server.tag = body.get("tag"); self.send_response(204); self.send_header("Content-Length", "0"); self.end_headers(); return
        self._forward("POST")

    def do_GET(self):
        if self.path == "/__health":
            b = json.dumps({"ok": True, "tag": self.server.tag}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        self._forward("GET")

    def _forward(self, method):
        up = urlsplit(self.server.upstream)
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n) if n else None
        cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
        kw = {"context": ssl.create_default_context()} if up.scheme == "https" else {}
        conn = cls(up.hostname, up.port or (443 if up.scheme == "https" else 80), timeout=900, **kw)
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "accept-encoding", "connection")}
        hdrs["Host"] = up.netloc
        if getattr(self.server, "inject_usage", False) and body and "/chat/completions" in self.path:
            try:  # v1.7.2：走 OpenAI 兼容网关时，流式默认不带 usage；不注入就无法记账
                o = json.loads(body)
                if o.get("stream") and not o.get("stream_options"):
                    o["stream_options"] = {"include_usage": True}
                    body = json.dumps(o).encode()
            except Exception:
                pass
        t0 = time.time(); tag = self.server.tag
        try:
            conn.request(method, (up.path.rstrip("/") + self.path), body=body, headers=hdrs)
            r = conn.getresponse()
        except Exception as ex:  # 上游连不上：回 502 让 DSH 自己按重试策略处理，并记一笔
            b = json.dumps({"error": {"type": "proxy_upstream_error", "message": repr(ex)}}).encode()
            self.send_response(502); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
            with LOCK, open(self.server.log, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": t0, "tag": tag, "path": self.path, "status": 502, "error": repr(ex)[:200]}) + "\n")
            return
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length", "content-encoding"):
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        acc, buf, full = {}, b"", []
        gone = False  # v1.7.3：客户端提前断了不算错，记账照做，只是不再往它那儿写
        while True:
            chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
            if not chunk:
                break
            if not gone:
                try:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk)); self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    gone = True; self.server.dropped = getattr(self.server, "dropped", 0) + 1
            buf += chunk
            if sum(map(len, full)) < 8_000_000:
                full.append(chunk)
            while b"\n" in buf:  # SSE：逐行解析 data: {...}
                line, buf = buf.split(b"\n", 1); line = line.strip()
                if line.startswith(b"data:"):
                    try:
                        extract_usage(json.loads(line[5:].strip()), acc)
                    except Exception:
                        pass
        if not any(k in acc for k in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens")) and full:
            try:  # v1.5：非流式回包（可能是多行缩进的 JSON）按整包解析；原来只解析最后一行，会漏记
                extract_usage(json.loads(b"".join(full)), acc)
            except Exception:
                pass
        if not gone:
            try:
                self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                gone = True; self.server.dropped = getattr(self.server, "dropped", 0) + 1
        model = None
        try:
            model = json.loads(body or b"{}").get("model")
        except Exception:
            pass
        rec = {"ts": t0, "dur_s": round(time.time() - t0, 3), "tag": tag, "path": self.path, "status": r.status,
               "model": model, "tool_calls": len(acc.pop("_tool_idx", ())), **acc}
        with LOCK, open(self.server.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

class Server(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"  # v1.5：Windows 上地址复用 = 允许两个进程抢同一端口，必须关掉
    daemon_threads = True

    def handle_error(self, request, client_address):
        # v1.9.1：连接级异常（客户端断开/取消）由服务端兜底静默计数；真正的转发错误在 _forward 里已经记进日志
        self.dropped = getattr(self, "dropped", 0) + 1


def start(port, log, upstream="https://api.deepseek.com", inject_usage=False):
    """在后台线程里起一个代理（bench.py 用）。每个代理有自己的“当前题号”，互不串。返回 server，用完调 shutdown()。"""
    srv = Server(("127.0.0.1", port), H)
    srv.upstream = upstream; srv.log = log; srv.tag = None; srv.inject_usage = inject_usage
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True); ap.add_argument("--log", required=True)
    ap.add_argument("--upstream", default="https://api.deepseek.com")
    ap.add_argument("--bind", default="127.0.0.1", help="监听地址；docker 严格模式在容器里用 0.0.0.0")
    ap.add_argument("--inject-usage", action="store_true", help="OpenAI 兼容流式：自动加 stream_options.include_usage 才能记账")
    a = ap.parse_args()
    srv = Server((a.bind, a.port), H); srv.upstream = a.upstream; srv.log = a.log; srv.tag = None; srv.inject_usage = a.inject_usage
    print(f"usage proxy :{a.port} -> {a.upstream}, log {a.log}"); srv.serve_forever()
