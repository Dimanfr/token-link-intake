# token-link-intake

Lets a client hand over a contact list for a campaign through a **one-time link**. The client needs no account and no database access. The operator creates a link; the client opens it, uploads a CSV and confirms three legal points. The server then validates and de-duplicates the contacts and stores them, and the link stops working.

```
operator ── create ──► link with random token ──► client opens form ──► CSV + 3 confirmations
                         (only its hash is stored)                         │
                                         server: validate, normalize, de-duplicate,
                                         consume token + store contacts in ONE transaction
                                                                           │
                                     201 accepted / rejected rows ◄────────┘   second submit → 409
```

## Why
Without a defined handover, contact lists travel as e-mail attachments in different shapes, with duplicates and numbers in every format. A link per campaign gives:
- one clear handover point and the legal confirmations at the moment of upload;
- validation on the server, not in the client's spreadsheet;
- an audit trail;
- no way to submit the same list twice by accident.

## Try it
```bash
python3 demo.py                              # scripted walk-through, temporary database
python3 -m unittest discover -s tests        # 17 tests, including the HTTP API on localhost
```
```text
open the link                                -> OK  Q4 outreach (fictional)
submit without confirmations                 -> 400 all confirmations must be accepted ['data_ownership', 'lawful_basis', 'campaign_owner']
submit the CSV                               -> OK  {'accepted': 3, 'rejected': [{'row': 6, 'reason': 'phone is not a dialable German number'}, {'row': 7, 'reason': 'phone is not a dialable German number'}], 'duplicates_in_file': 1, 'already_in_campaign': 0}
submit the same link again                   -> 409 this form has already been submitted
open with a wrong token                      -> 404 invitation not found
operator withdraws the second link           -> OK
open the withdrawn link                      -> 410 this link was withdrawn
```
The demo then prints the three stored contacts and the audit log: created, opened, submitted, revoked.

To try the form in a browser:
```bash
INTAKE_OPERATOR_KEY=choose-a-long-random-value python3 demo.py --serve
curl -s -X POST localhost:8790/api/invitations -H "X-Operator-Key: $INTAKE_OPERATOR_KEY" \
     -d '{"campaign_id": "c1", "campaign_name": "Test"}'
# open http://127.0.0.1:8790/intake/<token> and paste examples/contacts.csv
```

## How it works

| Request | Who | Result |
|---|---|---|
| `POST /api/invitations` | operator (`X-Operator-Key`, constant-time compare) | `201 {id, link}`; the token appears only here |
| `DELETE /api/invitations/<id>` | operator | `204`; the link answers `410` from now on |
| `GET /intake/<token>` | client | the form |
| `GET /api/intake/<token>` | client | campaign name and expiry, or `404` unknown / `409` already submitted / `410` expired or withdrawn |
| `POST /api/intake/<token>` | client | `201` with accepted and rejected rows, or `400` confirmations missing / `409` / `410` / `413` too large / `422` no valid contacts |

- **Only a hash of the token is stored.** A copy of the database does not reveal working links.
- **Exactly once.** The token is consumed with a conditional `UPDATE … WHERE submitted_at IS NULL` inside the same transaction that stores the contacts. A parallel second submission gets `409`, and a failed insert rolls the consumption back.
- **Validate before consuming.** Missing confirmations or a file without a single valid contact do not use up the link, so the client can fix the file and try again.
- **One contact per number per campaign.** Phones are normalized to E.164; duplicates inside the file and against earlier lists are counted, not stored twice.
- **The token stays out of logs and referrers:** no request logging, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.

## Status

| | |
|---|---|
| **Original use** | Client intake for an AI phone-campaign service (2026): a Supabase Edge Function in TypeScript with an HTML form, in production from 04/2026 with test data |
| **This repository** | Written fresh for publication in standard-library Python with SQLite. Changes against the original: the token is stored as a hash, and the token is consumed in the same transaction as the insert. Synthetic data only |
| **Authorship** | I define requirements, coordinate AI coding agents, test behaviour and operate the resulting systems; the agents implement the code. |

## Limitations
- It is a reference implementation, not a hardened service. Run it behind HTTPS and a reverse proxy, and add rate limiting before exposing it.
- One operator key, no user accounts. Store the key in a secret store.
- The phone check is minimal and German only; see [e164-normalizer](https://github.com/Dimanfr/e164-normalizer) for the full rules.
- The three confirmations are examples. They are not legal advice; your lawyer decides the wording.

Phone numbers in the examples come from the Bundesnetzagentur's ranges reserved for fiction; company names are fictional.

## Licence
MIT
