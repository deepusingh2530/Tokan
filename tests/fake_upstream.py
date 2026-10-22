"""Fake upstream speaking BOTH wire formats, for manual + automated tests.

    python tests/fake_upstream.py 8901 &

  * POST *​/chat/completions  → OpenAI chat shape
  * POST *​/v1/messages       → Anthropic message shape
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HITS = [0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length))
        HITS[0] += 1
        n = HITS[0]

        if self.path.endswith("/messages"):
            body = json.dumps(
                {
                    "id": f"msg_fake{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": payload.get("model", "fake"),
                    "content": [{"type": "text", "text": f"anthropic answer #{n}"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 70, "output_tokens": 12},
                }
            ).encode()
        else:
            body = json.dumps(
                {
                    "id": f"chatcmpl-fake{n}",
                    "object": "chat.completion",
                    "model": payload.get("model", "fake"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": f"openai answer #{n}"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                }
            ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        sys.stderr.write(f"[fake upstream] {self.path} hit #{n}\n")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    print(f"fake upstream on http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
