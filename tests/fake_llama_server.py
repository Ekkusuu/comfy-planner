"""Stand-in for llama-server in tests: answers /health on the given port after a short delay. Run as a script."""
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0)
    HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
