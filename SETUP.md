# Job Tracker – Setup

## 1. Google Cloud (one time, free)
1. console.cloud.google.com -> create a project (e.g. "job-tracker").
2. APIs & Services -> Library -> enable **Gmail API** and **Google Sheets API**.
3. APIs & Services -> OAuth consent screen -> User type **External** -> fill app name + your email.
   Add your own Gmail as a **Test user**.
4. IMPORTANT: on the consent screen click **Publish app** (status "In production").
   While an app stays in "Testing", Google expires your login token every 7 days and the script breaks.
   For personal use you do not need verification; you will just see an "unverified app" warning once -> Advanced -> continue.
5. APIs & Services -> Credentials -> Create credentials -> **OAuth client ID** -> type **Desktop app**
   -> Download JSON -> save as `credentials.json` in this folder.

## 2. Google Sheet
Create a blank sheet. Copy the ID from its URL:
`https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`
The script creates the header row itself. Column F (Message IDs) is used for de-duplication; hide it if you like.

## 3. Install and configure
```
pip install -r requirements.txt
cp .env.example .env        # then fill GROQ_API_KEY and SHEET_ID
python job_tracker.py       # first run opens a browser to log in
```
First run: set `LOOKBACK_DAYS=30` in `.env` to backfill a month of applications, then set it back to 2.

## 4. Run it daily
Mac/Linux (cron, `crontab -e`), every day 8:30 pm:
```
30 20 * * * cd /path/to/job-tracker && /usr/bin/python3 job_tracker.py >> run.log 2>&1
```
Windows: Task Scheduler -> Create Basic Task -> Daily -> Start a program -> `python` with argument `job_tracker.py` and "Start in" set to this folder.
(Your laptop must be on at that time; if a run is missed, the next one still catches up because of the lookback window.)
