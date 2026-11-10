#!/usr/bin/env python3
"""模拟 DeepSeek Anthropic 接口（只在演练里用）：按 Anthropic Messages 的 SSE 格式回包，
带 message_start.usage（输入 + 缓存命中）与 message_delta.usage（输出），让使用量代理能记账。

用法：python fake_api.py --port 8799 [--model DeepSeek-V4.1-Flash-1017] [--tools 2]
"""
import argparse, json, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "DeepSeek-V4.1-Flash-1017"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = json.dumps({"ok": True, "mock": "api"}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        big = len(json.dumps(req))
        hit = min(40000, big // 3)          # 假装大部分输入命中缓存
        miss = max(1, big // 200)
        out = 220 + (big % 180)
        n_tools = self.server.tools
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(obj):
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data)); self.wfile.flush()

        send({"type": "message_start", "message": {"id": "msg_mock", "model": MODEL,
             "usage": {"input_tokens": miss, "cache_read_input_tokens": hit, "cache_creation_input_tokens": 0, "output_tokens": 0}}})
        for i in range(n_tools):
            send({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": f"t{i}", "name": "shell"}})
            send({"type": "content_block_stop", "index": i})
        send({"type": "content_block_start", "index": n_tools, "content_block": {"type": "text", "text": ""}})
        send({"type": "content_block_delta", "index": n_tools, "delta": {"type": "text_delta", "text": "done"}})
        send({"type": "message_delta", "delta": {"stop_reason": "end_turn"},
              "usage": {"output_tokens": out, "input_tokens": miss, "cache_read_input_tokens": hit}})
        send({"type": "message_stop"})
        self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()


def serve(port=8799, tools=2):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True; srv.tools = tools
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--tools", type=int, default=2)
    a = ap.parse_args()
    print(f"fake api on :{a.port}")
    serve(a.port, a.tools).serve_forever()
