"""Mock OpenAI API server for testing."""
import json
from http.server import HTTPServer, BaseHTTPRequestHandler

FAKE_EMBEDDING = [0.01] * 1536

EXTRACTION_MEMORIES = [
    {"type": "fact", "key": "name", "value": "User's name is Alice", "confidence": 0.95},
    {"type": "fact", "key": "location", "value": "Lives in Berlin", "confidence": 0.9},
]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))

        if "/chat/completions" in self.path:
            content = json.dumps({"memories": EXTRACTION_MEMORIES})
            response = {"choices": [{"message": {"content": content}}]}
        elif "/embeddings" in self.path:
            inputs = body["input"]
            response = {"data": [{"embedding": FAKE_EMBEDDING} for _ in inputs]}
        else:
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode())

    def log_message(self, format, *args):
        print(f"[mock-openai] {args[0]}")


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", 9090), Handler)
    print("Mock OpenAI server running on port 9090")
    server.serve_forever()
