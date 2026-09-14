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
  campaigns.py       picks which of the 4 campaigns fits a company
  email_drafts.py    generates drafts + quality checks; sends nothing
  approval.py        the human approval gate -- decides what may send
  manual_send.py     export for sending by hand + sent tracking
  followups.py       follow-up schedule and drafting; stops on reply
  gmail.py           OAuth + sending, with every safety limit enforced
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
| 6  | Email drafting                 | done |
| 7  | Approval workflow              | done |
| 7b | Manual sending + tracking      | done |
| 8  | Gmail sending                  | done |
| 9  | Reply tracking                 | |
| 10 | Follow-ups                     | done |
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

python main.py draft            # write drafts (PENDING; sends nothing)
python main.py show-drafts        # list drafts
python main.py show-drafts --full # read the full text of each draft

python main.py review           # step through drafts, approve or reject
python main.py check-approvals  # show exactly what would be sent

python main.py export           # write approved emails to data/to_send.txt
python main.py mark-sent O001   # record that you sent one by hand

python main.py followups          # who is due for a follow-up
python main.py followups --draft  # write the follow-ups that are due
python main.py mark-followup O003 1   # record that you sent follow-up 1

python main.py gmail-auth       # authorise Gmail (opens your browser once)
python main.py gmail-test       # send ONE test email to your own address
python main.py send             # send approved emails
```

## Gmail setup

One-time setup in Google Cloud Console:

1. Create a project, enable the **Gmail API**.
2. **OAuth consent screen** -> External -> add the scopes
   `gmail.send` and `gmail.readonly` -> add your own address under
   **Test users** (this step is easy to miss and causes most failures).
3. **Credentials** -> OAuth client ID -> **Desktop app** -> download the
   JSON as `credentials.json` in the project root.

Then:

```bash
python main.py gmail-auth   # opens your browser, saves token.json
python main.py gmail-test   # sends one test email to YOURSELF
```

On first authorisation Google warns that the app is not verified. That is
expected for an app you built for your own use: click **Advanced -> Go to
(app name) (unsafe)**.

`credentials.json` and `token.json` are git-ignored. **token.json can send
mail as you -- never share it or commit it.**

## Sending safety

Every guard is enforced in code, not by convention:

| Guard | Behaviour |
|---|---|
| Approval gate | Calls the same `is_sendable()` the manual path uses. The sender does not reimplement it. |
| `DRY_RUN=true` | Blocks all sending and shows what would have gone. **On by default.** |
| `DAILY_SEND_LIMIT` | Counted from the workbook, so it survives restarts and counts emails you sent by hand. |
| `BATCH_SIZE` | Caps how many go out in one run. |
| `SECONDS_BETWEEN_SENDS` | Pause between messages. |
| Duplicate prevention | An already-SENT row is refused. |
| Confirmation | Typing `SEND` is required before emailing real people. |
| Failures | Recorded as `FAILED` with the reason. Never silent. |

The first live send must go to your own address: `gmail-test` reads your
address from Settings and refuses unless it matches the authorised
account, so it cannot email a company.

## Follow-ups

```
Day 0    initial email
Day 5    follow-up 1   did this reach the right person? offer a CV
Day 12   follow-up 2   a narrower question: do you take attachment students?
Day 22   follow-up 3   final message, graceful close, no new ask
```

Each follow-up has a **different job**. Three messages that all say "just
checking in" read as automated nagging, so the prompt gives each one its
own purpose and each is shorter than the last.

Two rules matter more than the dates:

1. **A reply stops the sequence immediately** -- whatever it said. A
   rejection is still a reply, and chasing someone who has answered is
   the fastest way to annoy an employer.
2. **There is never a fourth follow-up.** No code path schedules one.

The schedule runs off `Date Sent` in the workbook, so it works whether
you send by hand or through Gmail. Change the intervals in `.env`
(`FOLLOWUP_1_DAYS`, etc.).

Quality checks apply to follow-ups too, plus two of their own: a
follow-up that simply resends the original is flagged, and so is one that
sounds like a complaint ("I have not heard back from you").

## Sending by hand

Automated sending is optional. Sending the first emails yourself is a
reasonable choice: you see how real employers reply before automating
anything, and the first messages carry no risk of a technical mistake.

```bash
python main.py review      # approve the drafts you are happy with
python main.py export      # writes data/to_send.txt
# copy each block into Gmail and send it
python main.py mark-sent O003 --note "sent via Gmail"
```

`export` only ever includes rows the approval gate permits, so sending by
hand obeys exactly the same rule as automated sending would. `mark-sent`
refuses to record an unapproved email as sent, and refuses to mark the
same row twice.

The export separates two cases, because they are genuinely different:

- **Emails** -- an address was observed on the company's site, so you can
  send directly.
- **Applications** -- the organisation publishes a careers portal and no
  address. You apply through their form; the drafted text usually fits
  the "message" or "cover letter" field.

`data/to_send.txt` is git-ignored, since it contains recipient addresses.

## The approval gate

One function, `app/approval.py:is_sendable()`, decides whether an email
may be sent. Stage 8's sender calls it and does not reimplement it, so
there is one place to audit.

The rule:

> If `Approval Status != APPROVED`, the email is NOT sent -- even if
> `Email Status` says DRAFTED.

It also refuses to send when the row was already SENT (duplicate), when a
reply has arrived, or when the subject or body is empty.

Approve either way you prefer:

- **In Excel** -- type `APPROVED` in the Approval Status column. Case and
  surrounding spaces are tolerated.
- **In the terminal** -- `python main.py review` shows each draft and
  asks.

A value the system does not recognise, such as `APPROVE`, is **refused
and reported**, never guessed. Run `check-approvals` to see the exact
list of what would send before anything is sent.

## How drafts stay honest

The AI writes the email, then **Python checks it**. Two failure modes are
verified in code rather than trusted to the prompt, because both would
embarrass you in front of a real employer:

- **Flattery** -- "I have always admired your innovative company",
  "world-class", "passionate about". Caught by a banned-phrase list.
- **Overclaiming** -- "experienced data scientist", "years of
  experience", "proven track record". You are a student; the email must
  say so.

A third check came from a real failure: a draft described you as a
"second-year student", which appears nowhere in Settings. Settings
records an expected *graduation* year, not a current year, so any
year-of-study claim is invention and is now flagged.

A flagged draft is still saved as PENDING, with the problem written into
the Notes column. Flagging never auto-rejects -- you decide.

The signature is built from the Settings sheet, not written by the AI,
so your name, email and links are never paraphrased. Unfilled fields are
omitted rather than shown as FILL_IN.

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
