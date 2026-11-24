"""冒烟用假 Langfuse 接收端：只记录请求头与字节数（不记录载荷），返回 200。

用法：python3 smoke_sink.py [port] [log_path] [bind_host]
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
LOG_PATH = sys.argv[2] if len(sys.argv) > 2 else "/tmp/lf_sink_headers.jsonl"
BIND = sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1"


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        rec = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "ingestion": self.headers.get("x-langfuse-ingestion-version"),
            "content_type": self.headers.get("Content-Type"),
            "bytes": n,
        }
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


HTTPServer((BIND, PORT), H).serve_forever()