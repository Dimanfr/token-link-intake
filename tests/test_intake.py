import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from intake.core import (  # noqa: E402
    IntakeError, connect, create_invitation, lookup, normalize_phone, parse_contacts, revoke, submit,
)
from intake.server import make_server  # noqa: E402

OK = {"data_ownership": True, "lawful_basis": True, "campaign_owner": True}
CSV = "company,phone\nExample GmbH (fictional),030 23125 301\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "intake.sqlite"
        self.connection = connect(self.db)
        self.addCleanup(self.connection.close)

    def status(self, action):
        with self.assertRaises(IntakeError) as ctx:
            action()
        return ctx.exception.status


class Lifecycle(Base):
    def test_token_is_not_stored(self):
        _, token = create_invitation(self.connection, "c1", "Camp")
        stored = self.connection.execute("SELECT token_sha256 FROM invitations").fetchone()[0]
        self.assertNotEqual(stored, token)
        self.assertEqual(len(stored), 64)
        dump = "\n".join(self.connection.iterdump())
        self.assertNotIn(token, dump)

    def test_submit_once(self):
        _, token = create_invitation(self.connection, "c1", "Camp")
        self.assertEqual(submit(self.connection, token, CSV, OK)["accepted"], 1)
        self.assertEqual(self.status(lambda: submit(self.connection, token, CSV, OK)), 409)
        self.assertEqual(self.status(lambda: lookup(self.connection, token)), 409)

    def test_confirmations_required_and_token_kept(self):
        _, token = create_invitation(self.connection, "c1", "Camp")
        for partial in [{}, {"data_ownership": True, "lawful_basis": True}, {**OK, "campaign_owner": "yes"}]:
            self.assertEqual(self.status(lambda: submit(self.connection, token, CSV, partial)), 400)
        submit(self.connection, token, CSV, OK)  # still usable

    def test_invalid_file_keeps_token(self):
        _, token = create_invitation(self.connection, "c1", "Camp")
        self.assertEqual(self.status(lambda: submit(self.connection, token, "name,tel\nA,1\n", OK)), 422)
        self.assertEqual(self.status(lambda: submit(self.connection, token, "company,phone\nA,214\n", OK)), 422)
        submit(self.connection, token, CSV, OK)

    def test_expired(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        _, token = create_invitation(self.connection, "c1", "Camp", days_valid=7, clock=lambda: start)
        later = lambda: start + timedelta(days=7)  # noqa: E731
        self.assertEqual(self.status(lambda: lookup(self.connection, token, clock=later)), 410)
        self.assertEqual(self.status(lambda: submit(self.connection, token, CSV, OK, clock=later)), 410)

    def test_revoke(self):
        invitation_id, token = create_invitation(self.connection, "c1", "Camp")
        revoke(self.connection, invitation_id)
        self.assertEqual(self.status(lambda: lookup(self.connection, token)), 410)
        self.assertEqual(self.status(lambda: revoke(self.connection, invitation_id)), 409)

    def test_unknown_token(self):
        self.assertEqual(self.status(lambda: lookup(self.connection, "nope")), 404)
        self.assertEqual(self.status(lambda: lookup(self.connection, "x" * 500)), 404)

    def test_second_connection_cannot_submit_twice(self):
        _, token = create_invitation(self.connection, "c1", "Camp")
        other = connect(self.db)
        self.addCleanup(other.close)
        submit(other, token, CSV, OK)
        self.assertEqual(self.status(lambda: submit(self.connection, token, CSV, OK)), 409)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM contacts").fetchone()[0], 1)

    def test_contacts_unique_per_campaign(self):
        _, first = create_invitation(self.connection, "c1", "Camp")
        _, second = create_invitation(self.connection, "c1", "Camp, second list")
        submit(self.connection, first, CSV, OK)
        result = submit(self.connection, second, "company,phone\nOther (fictional),+49 30 23125 301\n", OK)
        self.assertEqual((result["accepted"], result["already_in_campaign"]), (0, 1))

    def test_audit_log(self):
        invitation_id, token = create_invitation(self.connection, "c1", "Camp")
        lookup(self.connection, token)
        lookup(self.connection, token)  # second open is not logged again
        submit(self.connection, token, CSV, OK)
        events = [r[0] for r in self.connection.execute("SELECT event FROM audit_log ORDER BY id")]
        self.assertEqual(events, ["created", "opened", "submitted"])


class Parsing(unittest.TestCase):
    def test_semicolon_bom_and_case(self):
        parsed = parse_contacts("﻿Company;PHONE;Contact_Name\nA (fictional);040 66969 302;Desk\n")
        self.assertEqual(parsed.contacts[0]["phone_e164"], "+494066969302")
        self.assertEqual(parsed.contacts[0]["contact_name"], "Desk")

    def test_rejections_and_duplicates(self):
        text = "company,phone\nA,030 23125 301\nB,+49 30 23125 301\n,030 23125 302\nC,030 2312\n"
        parsed = parse_contacts(text)
        self.assertEqual(len(parsed.contacts), 1)
        self.assertEqual(parsed.duplicates, 1)
        self.assertEqual([r["row"] for r in parsed.rejected], [4, 5])

    def test_example_file(self):
        parsed = parse_contacts((ROOT / "examples" / "contacts.csv").read_text(encoding="utf-8"))
        self.assertEqual((len(parsed.contacts), len(parsed.rejected), parsed.duplicates), (3, 2, 1))

    def test_normalize(self):
        self.assertEqual(normalize_phone("0049 (30) 23125-303"), "+493023125303")
        for bad in ["", "214", "+49 030 23125303", "+43 0000 000000", "030 2312 abc"]:
            self.assertIsNone(normalize_phone(bad))


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.server = make_server(Path(self.tmp.name) / "i.sqlite", "test-operator-key", port=0)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, key=None):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["X-Operator-Key"] = key
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, dict(response.headers), raw
        except urllib.error.HTTPError as error:
            with error:
                return error.code, dict(error.headers), error.read()

    def test_full_flow(self):
        new = {"campaign_id": "c1", "campaign_name": "Camp"}
        self.assertEqual(self.call("POST", "/api/invitations", new)[0], 401)
        self.assertEqual(self.call("POST", "/api/invitations", new, key="wrong")[0], 401)
        status, _, raw = self.call("POST", "/api/invitations", new, key="test-operator-key")
        self.assertEqual(status, 201)
        link = json.loads(raw)["link"]
        token = link.rsplit("/", 1)[1]

        status, headers, raw = self.call("GET", link)
        self.assertEqual(status, 200)
        self.assertIn(b"<form", raw)
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

        self.assertEqual(self.call("GET", f"/api/intake/{token}")[0], 200)
        self.assertEqual(self.call("POST", f"/api/intake/{token}", {"csv": CSV, "confirmations": {}})[0], 400)
        status, _, raw = self.call("POST", f"/api/intake/{token}", {"csv": CSV, "confirmations": OK})
        self.assertEqual((status, json.loads(raw)["accepted"]), (201, 1))
        self.assertEqual(self.call("POST", f"/api/intake/{token}", {"csv": CSV, "confirmations": OK})[0], 409)

    def test_revoke_and_errors(self):
        status, _, raw = self.call("POST", "/api/invitations", {"campaign_id": "c", "campaign_name": "C"},
                                   key="test-operator-key")
        body = json.loads(raw)
        token = body["link"].rsplit("/", 1)[1]
        self.assertEqual(self.call("DELETE", f"/api/invitations/{body['id']}")[0], 401)
        self.assertEqual(self.call("DELETE", f"/api/invitations/{body['id']}", key="test-operator-key")[0], 204)
        self.assertEqual(self.call("GET", f"/api/intake/{token}")[0], 410)
        self.assertEqual(self.call("GET", "/api/intake/unknown")[0], 404)
        self.assertEqual(self.call("GET", "/nothing")[0], 404)

    def test_empty_operator_key_refused(self):
        with self.assertRaises(ValueError):
            make_server(Path(self.tmp.name) / "x.sqlite", "", port=0)


if __name__ == "__main__":
    unittest.main()
