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
# then open .env and paste your key into AI_API_KEY

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
  research.py        fetches real website text -- evidence, no AI
  ai.py              provider layer (gemini / openai / mock)
  qualification.py   AI call + pydantic validation gate
  mock_ai.py         fake AI for free offline testing
  contacts.py        finds REAL contact routes; AI only labels them
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
| 3  | AI company qualification       | done |
| 4  | 100-point scoring              | done |
| 5  | Contact research               | done |
| 6  | Email drafting                 | next |
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

python main.py qualify --mock   # research + qualify, free and offline
python main.py qualify          # the real thing (needs AI_API_KEY)
python main.py qualify --limit 3   # only the first 3, to keep a first run cheap
python main.py qualify --force  # re-do companies that already have a status
python main.py show-results     # results table, best score first
python main.py report           # score distribution and pipeline counts

python main.py find-contacts    # find real contacts for QUALIFY companies
python main.py find-contacts --include-review   # also NEEDS_REVIEW ones
python main.py show-contacts    # list contacts found
```

## How contact research stays honest

The responsibilities are split so fabrication is structurally impossible:

- **Python finds.** A regex extracts email addresses from pages we
  actually fetched. Addresses on a third-party domain, and junk like
  `noreply@`, are discarded.
- **The AI only labels.** It is asked to classify what Python found. It
  is never asked to produce an address.
- **A guard enforces it.** Any email in the AI's reply that we did not
  observe is discarded and logged as an error. The model can describe a
  contact, never add one.

`Contact Name` and `LinkedIn` are therefore almost always `UNKNOWN`, and
that is correct: we do not scrape people, and a guessed name is worse
than no name. A careers portal is recorded as a legitimate route in --
for a large organisation it usually IS the right route.

`Email Verified` says `OBSERVED`, meaning the address was published on
the company's own site. It does not mean the mailbox was tested.

## AI provider

The default is **Google Gemini**, whose free tier needs no credit card.
Get a key at <https://aistudio.google.com/apikey> and put it in `.env`:

```
AI_PROVIDER=gemini
AI_API_KEY=your-key-here
```

To switch to OpenAI later, change two lines -- no code edits:

```
AI_PROVIDER=openai
AI_API_KEY=your-openai-key
```

All AI calls go through `app/ai.py`, so the rest of the code does not
know or care which provider answered.

## How qualification stays honest

The AI never researches from memory. The sequence is:

1. `research.py` fetches the company's real homepage and careers page and
   extracts the text. No AI involved.
2. That retrieved text -- and nothing else -- is given to the AI as
   evidence, along with any notes about what could NOT be fetched.
3. The AI returns JSON, which `pydantic` validates before it is written.
   A score above its maximum, an invented classification, or wrong
   arithmetic is caught here, not in your spreadsheet.
4. If the AI fails twice, the company is marked `ERROR` with the reason,
   and picked up again on the next run. Nothing fails silently.

Some real sites block automated access or render via JavaScript. Those
are recorded as "no evidence retrieved", which leads to `NEEDS_REVIEW` --
never `REJECT`. A company we could not look at is not a bad company.

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
