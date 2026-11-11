"""Walk through the intake lifecycle against a temporary database, no network needed.

    python3 demo.py                     # scripted walk-through
    INTAKE_OPERATOR_KEY=... python3 demo.py --serve   # run the HTTP API and form on 127.0.0.1:8790
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from intake.core import IntakeError, connect, create_invitation, lookup, revoke, submit  # noqa: E402

CONFIRMED = {"data_ownership": True, "lawful_basis": True, "campaign_owner": True}


def step(label, action):
    try:
        result = action()
        print(f"{label:<44} -> OK  {result if result is not None else ''}")
    except IntakeError as error:
        print(f"{label:<44} -> {error.status} {error.message}" + (f" {error.details}" if error.details else ""))


def walkthrough() -> None:
    csv_text = (HERE / "examples" / "contacts.csv").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        connection = connect(Path(tmp) / "intake.sqlite")
        invitation_id, token = create_invitation(connection, "camp-001", "Q4 outreach (fictional)")
        other_id, other_token = create_invitation(connection, "camp-002", "Second list (fictional)")
        print(f"created invitations {invitation_id} and {other_id}; the database keeps only token hashes\n")

        step("open the link", lambda: lookup(connection, token)["campaign_name"])
        step("submit without confirmations", lambda: submit(connection, token, csv_text, {}))
        step("submit the CSV", lambda: submit(connection, token, csv_text, CONFIRMED))
        step("submit the same link again", lambda: submit(connection, token, csv_text, CONFIRMED))
        step("open with a wrong token", lambda: lookup(connection, "not-a-real-token"))
        step("operator withdraws the second link", lambda: revoke(connection, other_id))
        step("open the withdrawn link", lambda: lookup(connection, other_token))

        print("\ncontacts stored:")
        for row in connection.execute("SELECT phone_e164, company FROM contacts ORDER BY source_row"):
            print(f"  {row['phone_e164']}  {row['company']}")
        print("\naudit log:")
        for row in connection.execute("SELECT invitation_id, event, detail FROM audit_log ORDER BY id"):
            print(f"  #{row['invitation_id']} {row['event']:<9} {row['detail'] or ''}")
        connection.close()


def serve() -> None:
    from intake.server import make_server

    key = os.environ.get("INTAKE_OPERATOR_KEY", "")
    Path("out").mkdir(exist_ok=True)
    server = make_server(Path("out") / "intake.sqlite", key)
    print("listening on http://127.0.0.1:8790  (create an invitation with POST /api/invitations)")
    server.serve_forever()


if __name__ == "__main__":
    serve() if "--serve" in sys.argv else walkthrough()
