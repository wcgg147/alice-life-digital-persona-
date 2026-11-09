"""Authenticated loopback bridge; SQLite remains inside the Linux runtime."""
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit

from .social import SocialState, permitted
from .state import Ledger


@contextmanager
def serve(state_path, bridge_file, config_loader):
    settings = json.loads(Path(bridge_file).read_text(encoding="utf-8-sig"))
    endpoint = urlsplit(settings["url"])
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or endpoint.path not in {"", "/"} or not endpoint.port:
        raise ValueError("bridge_must_bind_loopback")
    token = settings.get("token", "")
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError("bridge_token_required")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log authorization or private request bodies.

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.respond(401, {"error": "unauthorized"})
                return
            ledger = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("request_size_invalid")
                self.connection.settimeout(2)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("request_object_required")
                config = config_loader()
                scope, actor = str(data.get("scope", "")), str(data.get("actor", ""))
                if not permitted(config, scope, actor):
                    raise PermissionError("scope_not_authorized")
                ledger = Ledger(state_path)
                social = SocialState(ledger)
                if self.path == "/v1/event":
                    event_id, inserted = social.ingest(data, config)
                    result = {"event_id": event_id, "inserted": inserted}
                elif self.path == "/v1/context":
                    result = social.reply_packet(scope, actor)
                else:
                    self.respond(404, {"error": "unknown_endpoint"})
                    return
                self.respond(200, result)
            except PermissionError:
                self.respond(403, {"error": "scope_not_authorized"})
            except (ValueError, KeyError, TypeError):
                self.respond(400, {"error": "invalid_request"})
            except Exception:
                self.respond(503, {"error": "bridge_unavailable"})
            finally:
                if ledger:
                    ledger.close()

        def respond(self, status, value):
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", endpoint.port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
