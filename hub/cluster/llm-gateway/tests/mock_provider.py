"""Deterministic wire-protocol mock. No SDK, upstream accounts, or inference."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading

KEY = os.environ["CLIPROXY_API_KEY"]
TEXT = "mock answer"
REQUESTS = []
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, body, status=200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def events(self, events, done=False):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for name, body in events:
            if name:
                self.wfile.write(f"event: {name}\n".encode())
            self.wfile.write(f"data: {json.dumps(body)}\n\n".encode())
            self.wfile.flush()
        if done:
            self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True

    def do_GET(self):
        if self.path == "/__requests":
            with LOCK:
                self.reply(list(REQUESTS))
        elif self.path == "/v1/models":
            self.reply({"object": "list", "data": [
                {"id": "mock-openai", "object": "model", "owned_by": "mock"},
                {"id": "mock-anthropic", "object": "model", "owned_by": "mock"},
            ]})
        else:
            self.reply({"error": "unknown mock path"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        authorized = (
            self.headers.get("Authorization") == f"Bearer {KEY}"
            or self.headers.get("x-api-key") == KEY
        )
        with LOCK:
            REQUESTS.append({
                "path": self.path, "body": body, "authorized": authorized,
                "unapproved_header": self.headers.get("x-unapproved"),
            })
        if not authorized:
            self.reply({"error": "mock requires gateway-to-provider auth"}, 401)
            return
        model = body["model"]
        if self.path == "/v1/chat/completions":
            base = {"id": "chatcmpl-mock", "created": 1, "model": model}
            usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
            if body.get("stream"):
                chunks = [
                    {**base, "object": "chat.completion.chunk", "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": TEXT}, "finish_reason": None},
                    ]},
                    {**base, "object": "chat.completion.chunk", "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"},
                    ]},
                    {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage},
                ]
                self.events([(None, chunk) for chunk in chunks], done=True)
            else:
                self.reply({**base, "object": "chat.completion", "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": TEXT}, "finish_reason": "stop"},
                ], "usage": usage})
        elif self.path == "/v1/responses":
            content = {"type": "output_text", "text": TEXT, "annotations": []}
            item = {"id": "msg_mock", "type": "message", "role": "assistant", "status": "completed", "content": [content]}
            response = {
                "id": "resp_mock", "object": "response", "created_at": 1,
                "model": model, "status": "completed", "output": [item],
                "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            }
            if body.get("stream"):
                events = [
                    ("response.created", {"response": {**response, "status": "in_progress", "output": [], "usage": None}}),
                    ("response.output_item.added", {"output_index": 0, "item": {**item, "status": "in_progress", "content": []}}),
                    ("response.content_part.added", {"item_id": "msg_mock", "output_index": 0, "content_index": 0, "part": {**content, "text": ""}}),
                    ("response.output_text.delta", {"item_id": "msg_mock", "output_index": 0, "content_index": 0, "delta": TEXT}),
                    ("response.output_text.done", {"item_id": "msg_mock", "output_index": 0, "content_index": 0, "text": TEXT}),
                    ("response.content_part.done", {"item_id": "msg_mock", "output_index": 0, "content_index": 0, "part": content}),
                    ("response.output_item.done", {"output_index": 0, "item": item}),
                    ("response.completed", {"response": response}),
                ]
                self.events([(name, {"type": name, "sequence_number": i, **event}) for i, (name, event) in enumerate(events)])
            else:
                self.reply(response)
        elif self.path == "/v1/messages":
            message = {
                "id": "msg_mock", "type": "message", "role": "assistant",
                "model": model, "content": [{"type": "text", "text": TEXT}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 3},
            }
            if body.get("stream"):
                self.events([
                    ("message_start", {"type": "message_start", "message": {**message, "content": [], "stop_reason": None, "usage": {"input_tokens": 7, "output_tokens": 0}}}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": TEXT}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}}),
                    ("message_stop", {"type": "message_stop"}),
                ])
            else:
                self.reply(message)
        elif self.path == "/v1/messages/count_tokens":
            self.reply({"input_tokens": 7})
        else:
            self.reply({"error": "unknown mock path"}, 404)


ThreadingHTTPServer(("0.0.0.0", 8317), Handler).serve_forever()
