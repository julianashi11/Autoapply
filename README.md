# Autoapply

Finds open Summer 2027 SWE and PM internships, tailors your resume to each posting, and
fills in the application. You review each form and click Submit yourself.

## Where postings come from

1. **Company job boards, directly.** Every 20 minutes it reads about 2,200 companies' own
   boards on Greenhouse, Lever, Ashby, Workday and SmartRecruiters through their public
   APIs. Roles show up as soon as the company posts them, before community lists have them.
   The company list is every company that has posted a SWE, PM or AI role in the community
   feed, plus a built-in list of well-known tech companies, plus your `extra_boards`.
2. **The [SimplifyJobs Summer 2027 list](https://github.com/SimplifyJobs/Summer2027-Internships)**,
   which also covers companies with their own career sites (Google, Meta, Amazon, TikTok…).

Postings that appear in both are merged. Roles found on a company site first get a
**Direct** badge, and the page shows how far ahead of GitHub they were found.

Autofill works on Greenhouse, Lever and Ashby forms. Workday and company career sites open
in the browser for you to fill in by hand.

## Live page and email alerts (no laptop needed)

A GitHub Actions workflow (`.github/workflows/scan.yml`) runs the scan every 10 minutes in
the cloud, emails you each new role, and publishes the list at
**https://julianashi11.github.io/Autoapply/**. It uses `search.yaml`; your profile and
resumes never leave your computer.

One-time setup, in the repo's Settings → Secrets and variables → Actions (or with `gh secret set`):

| Secret | Value |
|---|---|
| `EMAIL_TO` | where alerts go |
| `RESEND_API_KEY` | easiest: a free [Resend](https://resend.com) API key. Sign up with the `EMAIL_TO` address; its test sender can mail that address with no domain setup |

Or send through a mailbox instead of Resend:

| Secret | Value |
|---|---|
| `SMTP_USER` | a Gmail (or personal Outlook.com) address |
| `SMTP_PASSWORD` | its App Password (Google: Account → Security → 2-Step Verification → App passwords) |

The first run records what's already open; emails start with the next new posting.
GitHub sometimes starts scheduled runs a few minutes late.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp profile.example.yaml profile.yaml   # then fill in your details and resume paths
```

## Web app

```sh
./aa web
```

Opens http://127.0.0.1:8765. Leave the terminal running while you use it.

- **Live postings:** scans every 20 minutes (a full scan takes about 6 minutes) or when
  you press Refresh. Postings that arrive while the page is open get a NEW badge. Filter
  by "Company sites only" or "Found before GitHub".
- **1. Resume screen:** estimates your chance of passing the first screen (Unlikely,
  Possible, Likely, Strong) and explains it: knockout requirements (graduation year,
  degree, GPA, citizenship/clearance), skill keywords from the requirements, formatting,
  and how early you're applying.
- **2. Tailored resume:** Claude reorders, trims and rewords your resume for the posting,
  using only what's already on it, and makes a one-page PDF. You can edit it on the page.
  Runs through Claude Code on your computer (your Claude login, no API key); an API key
  in Settings is optional. Saved in `data/tailored/`.
- **3. Apply:** opens the application in Chrome, fills it in, uploads the tailored resume,
  and lists the questions left for you. Submit in Chrome, then click "I submitted it".

The browser profile is saved in `data/browser`, so logins persist between runs.

## Command line

```sh
./aa fetch                     # scan company boards + the community list
./aa list --track pm           # also --ats, --company, --status all
./aa apply --limit 15          # autofill roles one at a time in the terminal
./aa mark <id> applied
./aa stats
./aa export                    # applications.csv
```

## Tuning

- **Citizenship:** set `me.us_citizen` in `profile.yaml` so roles needing U.S. citizenship,
  ITAR or a clearance are checked.
- **Answers:** `answers:` in `profile.yaml` maps a question regex to a value. Add rules
  for questions you keep seeing.
- **Search:** `search.yaml` controls categories, excluded title words, sponsorship and location
  filters. Add `AI/ML/Data` to the `swe` categories to include ML roles.
- **More companies:** add boards to `extra_boards` in `search.yaml`, e.g. `greenhouse:stripe` or
  `workday:nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite`. The board name is in the
  company's careers URL.
- **Company-site scan:** `us_only` skips roles outside the US; `direct_posted_after` drops
  undated postings older than this recruiting cycle.
- **Never guessed:** the tool never fills passwords, salary, accommodation, signature or
  "if yes, explain" questions.
