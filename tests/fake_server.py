"""A stand-in inference server for the server tests, started by a provider's `start` command.

It keeps a child process in its process group, as basal-serve keeps its llama-server, writes
the child's PID to a file, and lists models at any GET path in the shape asked for.
"""

import argparse
import json
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("port", type=int)
parser.add_argument("child_pid_file")
parser.add_argument("--shape", choices=["openai", "basal", "other"], default="openai")
parser.add_argument("--models", nargs="*", default=[])
parser.add_argument("--delay", type=float, default=0, help="seconds before it answers")
parser.add_argument("--ignore-term", action="store_true", help="survive SIGTERM")
args = parser.parse_args()

if args.ignore_term:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(args.child_pid_file, "w") as file:
    file.write(str(child.pid))

if args.shape == "openai":
    body = {"object": "list", "data": [{"id": name, "object": "model"} for name in args.models]}
elif args.shape == "basal":
    body = {"models": [{"name": name} for name in args.models]}
else:
    body = {"status": "ok"}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


time.sleep(args.delay)
HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
