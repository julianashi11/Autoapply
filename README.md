# Autoapply

Finds open Summer 2027 SWE and PM internships and fills in their applications for you.
You review each form and click Submit yourself.

Listings come from the community-maintained
[SimplifyJobs Summer 2027 internships](https://github.com/SimplifyJobs/Summer2027-Internships) feed.
Autofill works on Greenhouse, Lever and Ashby forms. Workday and company career sites
open in the browser for you to fill in by hand (`--ats all`).

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp profile.example.yaml profile.yaml   # then fill in your details
mkdir -p resumes                       # put resume_swe.pdf and resume_pm.pdf here
```

## Usage

```sh
./aa fetch                     # pull open roles that match profile.yaml (run daily)
./aa list                      # new roles, newest first
./aa list --track pm           # PM only; also --ats, --company, --status all
./aa apply                     # autofill the next 15 roles one at a time
./aa apply --track swe --limit 30
./aa apply --ats all           # include Workday and others (opened, not autofilled)
./aa apply --id db6f3fe2       # one specific role
./aa mark db6f3fe2 applied     # change a status by hand
./aa stats
./aa export                    # applications.csv
```

During `apply`, each form opens in Chrome with everything it could answer filled in. The
terminal lists required questions it left for you (essays, EEO questions, "have you worked
here before", and so on). Finish the form, submit it, then press `a` (applied), `s` (skip),
`l` (later), `r` (refill) or `q` (quit).

The browser profile is saved in `data/browser`, so logins persist between runs.

## Tuning

- **Answers:** `answers:` in `profile.yaml` maps a question regex to a value. Add rules
  for questions you keep seeing.
- **Search:** `search:` controls categories, excluded title words, sponsorship and location
  filters. Add `AI/ML/Data` to the `swe` categories to include ML roles.
- **Never guessed:** the tool never fills passwords, salary, accommodation, signature or
  "if yes, explain" questions.
