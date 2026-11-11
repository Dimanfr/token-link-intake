"""Small HTTP API and form around intake.core (standard library only).

Operator (needs the X-Operator-Key header):
    POST   /api/invitations            {"campaign_id", "campaign_name", "days_valid"?} -> 201 {id, link}
    DELETE /api/invitations/<id>                                                      -> 204
Client (only the token from the link):
    GET    /intake/<token>             HTML form
    GET    /api/intake/<token>         invitation info, or 404 / 409 / 410
    POST   /api/intake/<token>         {"csv": "...", "confirmations": {...}} -> 201, or 400/409/410/413/422
"""

from __future__ import annotations

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .core import IntakeError, connect, create_invitation, lookup, revoke, submit

__all__ = ["make_server"]

MAX_BODY = 2_500_000
FORM = (Path(__file__).with_name("form.html")).read_text(encoding="utf-8")


def make_server(db_path, operator_key: str, host="127.0.0.1", port=8790) -> ThreadingHTTPServer:
    if not operator_key:
        raise ValueError("operator key must not be empty")

    class Handler(BaseHTTPRequestHandler):
        server_version = "token-link-intake"

        def log_message(self, *args):  # keep tokens out of the console log
            pass

        def _send(self, status, payload=None, content_type="application/json"):
            body = b"" if payload is None else (
                payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")  # the token is in the URL
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise IntakeError(413, "request too large")
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise IntakeError(400, "body must be JSON") from None

        def _operator(self):
            given = self.headers.get("X-Operator-Key", "")
            if not hmac.compare_digest(given.encode(), operator_key.encode()):
                raise IntakeError(401, "operator key required")

        def _handle(self, method):
            connection = connect(db_path)
            try:
                path = self.path.split("?", 1)[0]
                if method == "GET" and (m := re.fullmatch(r"/intake/([\w-]+)", path)):
                    return self._send(200, FORM, "text/html; charset=utf-8")
                if m := re.fullmatch(r"/api/intake/([\w-]+)", path):
                    if method == "GET":
                        return self._send(200, lookup(connection, m.group(1)))
                    if method == "POST":
                        body = self._json_body()
                        result = submit(connection, m.group(1), str(body.get("csv", "")),
                                        body.get("confirmations") or {})
                        return self._send(201, result)
                if path == "/api/invitations" and method == "POST":
                    self._operator()
                    body = self._json_body()
                    if not body.get("campaign_id") or not body.get("campaign_name"):
                        raise IntakeError(400, "campaign_id and campaign_name are required")
                    invitation_id, token = create_invitation(
                        connection, str(body["campaign_id"]), str(body["campaign_name"]),
                        int(body.get("days_valid", 14)))
                    return self._send(201, {"id": invitation_id, "link": f"/intake/{token}"})
                if (m := re.fullmatch(r"/api/invitations/(\d+)", path)) and method == "DELETE":
                    self._operator()
                    revoke(connection, int(m.group(1)))
                    return self._send(204)
                raise IntakeError(404, "not found")
            except IntakeError as error:
                return self._send(error.status, {"error": error.message, "details": error.details})
            finally:
                connection.close()

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_DELETE(self):
            self._handle("DELETE")

    return ThreadingHTTPServer((host, port), Handler)
