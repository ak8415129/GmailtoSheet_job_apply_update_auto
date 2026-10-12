"""
Job Application Tracker
Gmail -> Groq (classify + extract) -> Google Sheet

Sheet columns: Date | Company Name | Job Role | Status | Comment / Details | Message IDs
(The last column is used for de-duplication; you can hide it in the sheet.)
"""
import base64
import json
import os
import re
import sys
import time
from datetime import datetime
from html import unescape

from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from groq import Groq, NotFoundError

load_dotenv()

# ---------------------------------------------------------------- config
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
SHEET_ID = os.environ["SHEET_ID"]
SHEET_TAB = os.getenv("SHEET_TAB", "Sheet1")
LOOKBACK_DAYS = int(os.getenv("LOOKBACK_DAYS", "2"))
MAX_EMAILS = int(os.getenv("MAX_EMAILS", "300"))
CLASSIFY_MODEL = os.getenv("CLASSIFY_MODEL", "llama-3.1-8b-instant")
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "llama-3.3-70b-versatile")
BATCH_SIZE = 15
BODY_CHARS = 4000

HEADER = ["Date", "Company Name", "Job Role", "Status", "Comment / Details", "Message IDs"]
STATUSES = ["Applied", "Screening Stage", "Assessment", "Interview", "Offer", "Rejection", "Other"]

SEEN_FILE = "seen_ids.json"  # emails already classified (job or not), saves Groq calls
groq = Groq(api_key=os.environ["GROQ_API_KEY"])

PREFER_FAST = ["llama-3.1-8b-instant", "openai/gpt-oss-20b", "llama-3.3-70b-versatile", "openai/gpt-oss-120b"]
PREFER_SMART = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
NOT_CHAT = ("whisper", "tts", "guard", "orpheus", "embed", "compound", "safeguard")


def resolve_models():
    """Ask Groq which models this key can use; fall back automatically if a configured one is gone."""
    global CLASSIFY_MODEL, EXTRACT_MODEL
    try:
        available = {m.id for m in groq.models.list().data}
    except Exception as e:
        print(f"Could not list Groq models ({e.__class__.__name__}); using configured names")
        return
    chat = sorted(m for m in available if not any(x in m for x in NOT_CHAT))
    print("Groq chat models available to your key:", ", ".join(chat))

    def pick(current, prefs, label):
        if current in available:
            return current
        for cand in prefs + chat:
            if cand in available and not any(x in cand for x in NOT_CHAT):
                print(f"Model '{current}' not available for {label}; using '{cand}' instead")
                return cand
        sys.exit("No usable Groq chat model found for this API key. Check console.groq.com.")

    CLASSIFY_MODEL = pick(CLASSIFY_MODEL, PREFER_FAST, "classification")
    EXTRACT_MODEL = pick(EXTRACT_MODEL, PREFER_SMART, "extraction")


# ---------------------------------------------------------------- auth
def get_creds():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            creds = None
    if not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
        creds = flow.run_local_server(port=0)
    with open("token.json", "w") as f:
        f.write(creds.to_json())
    return creds


# ---------------------------------------------------------------- gmail
def list_message_ids(gmail):
    query = f"newer_than:{LOOKBACK_DAYS}d -in:spam -in:trash -in:sent"
    ids, page = [], None
    while len(ids) < MAX_EMAILS:
        resp = gmail.users().messages().list(
            userId="me", q=query, pageToken=page, maxResults=100
        ).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page:
            break
    return ids[:MAX_EMAILS]


def header(msg, name):
    for h in msg["payload"].get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def fetch_meta(gmail, mid):
    m = gmail.users().messages().get(
        userId="me", id=mid, format="metadata",
        metadataHeaders=["From", "Subject"],
    ).execute()
    return {
        "id": mid,
        "from": header(m, "From"),
        "subject": header(m, "Subject"),
        "snippet": unescape(m.get("snippet", ""))[:200],
        "ts": int(m["internalDate"]) / 1000,
    }


def _decode(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _collect(part, plain, htm):
    mime = part.get("mimeType", "")
    data = part.get("body", {}).get("data")
    if data and mime == "text/plain":
        plain.append(_decode(data))
    elif data and mime == "text/html":
        htm.append(_decode(data))
    for sub in part.get("parts", []) or []:
        _collect(sub, plain, htm)


def fetch_body(gmail, mid):
    m = gmail.users().messages().get(userId="me", id=mid, format="full").execute()
    plain, htm = [], []
    _collect(m["payload"], plain, htm)
    if plain:
        text = "\n".join(plain)
    else:
        text = "\n".join(htm)
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()[:BODY_CHARS]


# ---------------------------------------------------------------- llm
def llm_json(model, system, user, retries=4):
    for attempt in range(retries):
        try:
            r = groq.chat.completions.create(
                model=model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
            return json.loads(r.choices[0].message.content)
        except NotFoundError as e:
            sys.exit(f"Groq says model '{model}' was not found: {e}\n"
                     "Set CLASSIFY_MODEL / EXTRACT_MODEL in .env to a model from the list printed above.")
        except Exception as e:  # rate limit, bad JSON, network...
            wait = 5 * 2**attempt
            print(f"  Groq error ({e.__class__.__name__}: {str(e)[:150]}), retrying in {wait}s")
            time.sleep(wait)
    return None


CLASSIFY_SYSTEM = """You sort a person's emails. For each email decide if it is about THEIR OWN job application process:
application received/confirmation, assessment or coding test invite, screening/interview invite or scheduling,
recruiter reply about a specific application, offer, or rejection.
NOT job related: job-alert digests, "jobs you may like", newsletters, course/bootcamp ads, generic marketing,
social notifications, bills, anything unrelated to hiring.
Reply ONLY with JSON: {"results":[{"id":"<id>","job_related":true|false}]} covering every id given."""


def classify(batch):
    payload = [
        {"id": e["id"], "from": e["from"], "subject": e["subject"], "snippet": e["snippet"]}
        for e in batch
    ]
    out = llm_json(CLASSIFY_MODEL, CLASSIFY_SYSTEM, json.dumps(payload))
    if not out:
        return None  # unknown -> don't mark as seen, retry next run
    return {r["id"]: bool(r.get("job_related")) for r in out.get("results", []) if "id" in r}


EXTRACT_SYSTEM = f"""You extract job-application details from one email.
Reply ONLY with JSON: {{"company": str, "role": str, "status": str, "details": str}}
- company: the HIRING company (not the ATS/platform such as Workday, Greenhouse, Lever, LinkedIn).
- role: job title; append the job/requisition ID in parentheses if the email shows one.
- status: exactly one of {STATUSES}.
  Applied = application received/submitted; Screening Stage = recruiter screen or initial screening invite;
  Assessment = online test/coding challenge; Interview = interview scheduled/invited; Offer; Rejection.
- details: ONE short factual sentence (max 25 words), include any deadline or interview date mentioned.
If a field is unknown use an empty string; use "Other" for status only if nothing fits."""


def extract(email, body):
    user = f"From: {email['from']}\nSubject: {email['subject']}\n\n{body}"
    out = llm_json(EXTRACT_MODEL, EXTRACT_SYSTEM, user)
    if not out:
        return None
    status = out.get("status", "Other")
    if status not in STATUSES:
        status = "Other"
    return {
        "company": str(out.get("company", "")).strip(),
        "role": str(out.get("role", "")).strip(),
        "status": status,
        "details": str(out.get("details", "")).strip(),
    }


# ---------------------------------------------------------------- sheet logic (pure, testable)
def norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def same_company(a, b):
    a, b = " ".join(norm(a)), " ".join(norm(b))
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def same_role(a, b):
    a, b = " ".join(norm(a)), " ".join(norm(b))
    return bool(a) and bool(b) and (a in b or b in a)


def plan_changes(existing, items):
    """existing: sheet rows (without header). items: chronologically sorted dicts.
    Returns (rows, dirty_row_numbers, first_new_index). Row number = index + 2."""
    rows = [(r + [""] * 6)[:6] for r in existing]
    n_existing = len(rows)
    dirty = set()
    for it in items:
        hit = None
        for i, r in enumerate(rows):
            if same_company(r[1], it["company"]) and same_role(r[2], it["role"]):
                hit = i
                break
        if hit is None:
            rows.append([it["date"], it["company"], it["role"], it["status"],
                         it["details"], it["id"]])
        else:
            r = rows[hit]
            if it["status"] != "Other":
                r[3] = it["status"]
            note = f"{it['date']}: {it['details']}" if it["details"] else it["date"]
            r[4] = f"{r[4]} | {note}" if r[4] else note
            r[5] = f"{r[5]},{it['id']}" if r[5] else it["id"]
            if hit < n_existing:
                dirty.add(hit + 2)
    return rows, dirty, n_existing


# ---------------------------------------------------------------- sheets io
def read_sheet(sheets):
    rng = f"{SHEET_TAB}!A1:F"
    vals = sheets.spreadsheets().values().get(spreadsheetId=SHEET_ID, range=rng).execute().get("values", [])
    if not vals:
        sheets.spreadsheets().values().update(
            spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A1:F1",
            valueInputOption="RAW", body={"values": [HEADER]},
        ).execute()
        return []
    return vals[1:]


def write_sheet(sheets, rows, dirty, n_existing):
    if dirty:
        data = [{"range": f"{SHEET_TAB}!A{n}:F{n}", "values": [rows[n - 2]]} for n in sorted(dirty)]
        sheets.spreadsheets().values().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
    new = rows[n_existing:]
    if new:
        sheets.spreadsheets().values().append(
            spreadsheetId=SHEET_ID, range=f"{SHEET_TAB}!A:F",
            valueInputOption="USER_ENTERED", insertDataOption="INSERT_ROWS",
            body={"values": new},
        ).execute()


# ---------------------------------------------------------------- main
def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen)[-5000:], f)


def main():
    resolve_models()
    creds = get_creds()
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    existing = read_sheet(sheets)
    logged = {i for r in existing if len(r) > 5 for i in r[5].split(",")}
    seen = load_seen()

    ids = [i for i in list_message_ids(gmail) if i not in logged and i not in seen]
    print(f"{len(ids)} new emails to check")
    if not ids:
        return

    metas = [fetch_meta(gmail, i) for i in ids]

    job_emails = []
    for k in range(0, len(metas), BATCH_SIZE):
        batch = metas[k:k + BATCH_SIZE]
        verdict = classify(batch)
        if verdict is None:
            print("  classification failed for a batch; will retry next run")
            continue
        for e in batch:
            seen.add(e["id"])
            if verdict.get(e["id"]):
                job_emails.append(e)
    print(f"{len(job_emails)} look job-related")

    items = []
    for e in sorted(job_emails, key=lambda x: x["ts"]):
        info = extract(e, fetch_body(gmail, e["id"]))
        if not info or not info["company"]:
            print(f"  skipped (no company found): {e['subject'][:60]}")
            continue
        info["id"] = e["id"]
        info["date"] = datetime.fromtimestamp(e["ts"]).strftime("%d %b %Y")
        items.append(info)
        print(f"  {info['date']} | {info['company']} | {info['role']} | {info['status']}")

    if items:
        rows, dirty, n_existing = plan_changes(existing, items)
        write_sheet(sheets, rows, dirty, n_existing)
        print(f"Sheet updated: {len(rows) - n_existing} new rows, {len(dirty)} rows updated")
    save_seen(seen)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)