# Internship & Industrial Attachment Outreach Agent

An AI-assisted system that helps identify legitimate organisations that could
host an internship or industrial attachment, research and qualify them, find
appropriate contacts, draft personalised emails, and -- **only after human
approval** -- send them via Gmail and track replies.

Excel is the database. Python is the engine. A human is the approval gate.

## Pipeline

```
Companies (Excel) -> Research -> AI Qualification -> Scoring
    -> Contact Selection -> Personalisation -> Draft
    -> Excel Approval Queue -> HUMAN REVIEW -> Gmail Send
    -> Reply Monitoring -> Classification -> Follow-ups -> Excel CRM
```

## Setup

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your local config
cp .env.example .env
# then open .env and fill in your OPENAI_API_KEY

# 4. Check it works
python main.py
```

## Project layout

```
app/
  config.py          settings loaded from .env, validated at startup
  logging_setup.py   one place that configures logging
  schema.py          sheet names, columns and allowed values
  excel.py           the only module that touches the workbook
  sample_data.py     10 mixed test companies
prompts/             AI prompt templates (Stage 3+)
data/                the Excel workbook -- the database (Stage 2)
tests/               unit tests
main.py              CLI entry point
```

## Build stages

| Stage | What it adds | Status |
|-------|--------------|--------|
| 1  | Project setup, config, logging | done |
| 2  | Excel database (4 sheets)      | done |
| 3  | AI company qualification       | next |
| 4  | 100-point scoring              | |
| 5  | Contact research               | |
| 6  | Email drafting                 | |
| 7  | Approval workflow              | |
| 8  | Gmail sending                  | |
| 9  | Reply tracking                 | |
| 10 | Follow-ups                     | |
| 11 | Reporting                      | |
| 12 | Optional Streamlit dashboard   | |

## Safety rules

- No email is ever sent while `Approval Status != APPROVED`.
- `DRY_RUN=true` in `.env` blocks all sending.
- Secrets live in `.env`, which is git-ignored. Never commit it.
- The AI must never invent a person's name, email, company achievement, or
  internship programme. Unverifiable information is recorded as `UNKNOWN`.

## Commands

```bash
python main.py                  # status + workbook summary
python main.py init-excel       # create the workbook (refuses to overwrite)
python main.py load-samples     # add 10 test companies (skips duplicates)
python main.py show-companies   # list them as a table
python main.py check-excel      # validate structure, list unfilled settings
```

## The workbook

`data/internship_outreach.xlsx` has four sheets:

- **Companies** (27 columns) -- one row per organisation, from raw name
  through research, scoring and priority.
- **Contacts** (12 columns) -- people at those companies. Never invented;
  unverifiable fields are `UNKNOWN`.
- **Outreach** (20 columns) -- one row per email, carrying the approval
  status that gates sending.
- **Settings** (key/value) -- your reusable profile.

You can edit it by hand in Excel at any time. `update_row` only writes the
columns it is given, so manual edits to other columns survive. Close the
file before running commands -- Excel locks it while open.

## Running tests

```bash
python -m unittest discover tests
```
