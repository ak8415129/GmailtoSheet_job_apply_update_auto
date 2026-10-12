# Job Application Tracker

Scans your Gmail daily, finds emails about your job applications (applied, assessment, interview, offer, rejection), extracts the details with an LLM (Groq), and logs them in a Google Sheet.

```
Gmail  ->  Groq (1. classify  2. extract)  ->  Google Sheet
```

## Output format

| Date | Company Name | Job Role | Status | Comment / Details | Message IDs |
|---|---|---|---|---|---|
| 05 Apr 2026 | NTT DATA | Data Engineer | Screening Stage | Received invitation to participate in the initial screening assessment. | ... |
| 05 Apr 2026 | Microsoft | Data Scientist (Job ID: 200033809) | Rejection | Application submitted on Apr 5; received rejection notification on Apr 8. | ... |

- One row per application. When a new email arrives for the same company and role, the existing row is updated: the status moves forward and the comment grows (`Applied | 08 Apr 2026: Not moving forward.`).
- Column F (Message IDs) stores Gmail message IDs so nothing is logged twice. Hide the column if you want.
- Status values: Applied, Screening Stage, Assessment, Interview, Offer, Rejection, Other.

## Files

| File | Purpose |
|---|---|
| `job_tracker.py` | The whole tool |
| `requirements.txt` | Python packages |
| `.env.example` | Template for your settings (copy to `.env`) |
| `credentials.json` | You download this from Google Cloud (step 1). Never share it. |
| `token.json` | Created automatically after your first login. Never share it. |
| `seen_ids.json` | Created automatically. Remembers emails already checked. |

## Setup (Windows)

### 1. Google Cloud (one time, free)

1. Go to https://console.cloud.google.com and create a project.
2. **APIs & Services > Library**: enable **Gmail API** and **Google Sheets API**.
3. **OAuth consent screen** (newer UI: **Google Auth Platform**): choose **External**, fill in an app name and your email.
4. **Publish the app** (status must become **In production**). If you leave it in Testing, you get `Error 403: access_denied` unless you are added as a test user, and your login token expires every 7 days, which breaks the daily run. For personal use no Google verification is needed.
5. **Credentials > Create credentials > OAuth client ID > Desktop app**. Download the JSON and save it in this folder as `credentials.json`.

### 2. Google Sheet

Create a blank Google Sheet and copy its ID from the URL:

```
https://docs.google.com/spreadsheets/d/<THIS_PART_IS_THE_ID>/edit
```

The script writes the header row itself. Note the tab name at the bottom (usually `Sheet1`).

### 3. Groq API key

Create a free key at https://console.groq.com/keys.

### 4. Install and configure

Open Command Prompt in this folder:

```
pip install -r requirements.txt
copy .env.example .env
notepad .env
```

Fill in `.env`:

```
GROQ_API_KEY=gsk_your_key
SHEET_ID=your_sheet_id
SHEET_TAB=Sheet1
LOOKBACK_DAYS=30
```

Use `LOOKBACK_DAYS=30` for the first run to backfill a month of applications. After that, change it to `2`.

### 5. First run

```
python job_tracker.py
```

- A browser opens. Sign in with the Gmail account you want to scan.
- You may see "Google hasn't verified this app". This is your own app, so click **Advanced > Go to (app name)** and allow access.
- The script prints the Groq models it found, the emails it checked, and each row it logged.

## Run it daily (Windows Task Scheduler)

1. Run `where python` and note the full path.
2. Open **Task Scheduler > Create Task**.
3. **Triggers**: New > Daily, at a time your laptop is usually on.
4. **Actions**: Start a program.
   - Program: the full python path from step 1
   - Arguments: `job_tracker.py`
   - Start in: the full path of this folder
5. **Settings**: tick "Run task as soon as possible after a scheduled start is missed".

If a run is missed, the next one catches up because of the `LOOKBACK_DAYS` window.

## Settings (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `GROQ_API_KEY` | required | Your Groq key |
| `SHEET_ID` | required | Google Sheet ID |
| `SHEET_TAB` | `Sheet1` | Tab name |
| `LOOKBACK_DAYS` | `2` | How many days of email to scan |
| `MAX_EMAILS` | `300` | Safety cap per run |
| `CLASSIFY_MODEL` | `llama-3.1-8b-instant` | Fast model that decides job or not job |
| `EXTRACT_MODEL` | `llama-3.3-70b-versatile` | Stronger model that extracts the fields |

Groq changes its model list often. At startup the script asks Groq which models your key can use and switches automatically if a configured one is gone. You can pin models yourself with `CLASSIFY_MODEL=` and `EXTRACT_MODEL=` using names from the list it prints.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Error 403: access_denied` / "has not completed the Google verification process" | Publish the app (step 1.4), or add your Gmail as a test user. Then delete `token.json` and rerun. |
| Browser logs into the wrong Google account | Delete `token.json`, rerun, and pick the right account. |
| Login stops working after about a week | The app is still in Testing mode. Publish it, delete `token.json`, rerun. |
| `NotFoundError` / "model not found" | Use the latest `job_tracker.py` (it auto-detects models), or set `CLASSIFY_MODEL` / `EXTRACT_MODEL` in `.env`. |
| `RateLimitError` / retries in a loop | Free Groq limits. Wait a bit, or lower `LOOKBACK_DAYS` and `MAX_EMAILS`. The script retries on its own. |
| `KeyError: 'SHEET_ID'` or `'GROQ_API_KEY'` | `.env` is missing or not in this folder. |
| Nothing is added to the sheet | Check the printed counts. If "0 look job-related", try a larger `LOOKBACK_DAYS`. Emails in Spam, Trash, or Sent are skipped on purpose. |
| Wrong company or role extracted | The LLM can misread unusual emails. Edit the row by hand and keep the message ID in column F. |
| Same email processed again | Do not delete column F or `seen_ids.json`. |

## Privacy and safety

- The script has read-only access to Gmail. It cannot send, delete, or change emails.
- Email text (sender, subject, and the body of job-related emails) is sent to Groq for processing.
- Never share or upload `credentials.json`, `token.json`, or `.env`. If you use git, add them to `.gitignore`.
- To revoke access: https://myaccount.google.com/permissions

## Ideas for next steps

- Status dropdown and colour rules in the sheet (for example, red for Rejection, green for Offer)
- A daily summary email of new updates
- Run in the cloud with GitHub Actions so the laptop does not need to be on
