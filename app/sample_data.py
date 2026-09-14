"""Ten test companies for developing and testing the pipeline.

These exist to prove the system can TELL THE DIFFERENCE between a real
internship employer and a shop with "Technology" in its name. The list is
deliberately mixed:

  - Well-known Kenyan employers with real engineering/data teams
  - Mid-size legitimate technology companies
  - A small startup (small is fine -- smallness alone is not a reason
    to reject)
  - A university research unit
  - Businesses that should be REJECTED: a cyber cafe, a computer repair
    shop, a phone accessories shop

Only Company Name, Website, Country, Region and Source are filled in.
Everything else is left blank ON PURPOSE -- those columns are the AI's job
in Stages 3-5. Pre-filling them would mean testing our own guesses rather
than the system.

The websites here were checked to resolve at the time of writing. Some
real sites sit behind bot protection and will return "blocked" -- that is
a genuine outcome the pipeline must handle, not a bug, and it is why two
of these entries are expected to end up NEEDS_REVIEW rather than QUALIFY.
"""

from app import schema

# (Company Name, Website, Country, Region, note-to-self about expectation)
SAMPLE_COMPANIES = [
    (
        "Safaricom PLC",
        "https://www.safaricom.co.ke",
        "Kenya",
        "Nairobi",
        "Large telecom with substantial technology and data functions",
    ),
    (
        "Equity Bank Kenya",
        "https://equitygroupholdings.com",
        "Kenya",
        "Nairobi",
        "Bank with an internal technology and data department",
    ),
    (
        "Twiga Foods",
        schema.UNKNOWN,  # current domain not confirmed -- do not guess one
        "Kenya",
        "Nairobi",
        "Agri-logistics built on a tech platform, but we have no verified "
        "website. A legitimate company with NO evidence -- should land in "
        "NEEDS_REVIEW, never REJECT. This is the case that proves the "
        "system distinguishes 'bad company' from 'no data'.",
    ),
    (
        "Cellulant",
        "https://cellulant.io",
        "Kenya",
        "Nairobi",
        "Pan-African fintech / payments engineering",
    ),
    (
        "iHub",
        "https://ihub.co.ke",
        "Kenya",
        "Nairobi",
        "Technology innovation hub and community",
    ),
    (
        "Strathmore University iLabAfrica",
        "https://ilabafrica.strathmore.edu",
        "Kenya",
        "Nairobi",
        "University research centre -- research/data projects",
    ),
    (
        "Andela",
        "https://andela.com",
        "Kenya",
        "Nairobi",
        "Engineering talent company with a large Nairobi presence",
    ),
    (
        "Jamii Cyber Cafe",
        schema.UNKNOWN,
        "Kenya",
        "Nairobi",
        "EXPECT REJECT -- cyber cafe, no professional tech function",
    ),
    (
        "Kilimani Computer Repairs",
        schema.UNKNOWN,
        "Kenya",
        "Nairobi",
        "EXPECT REJECT -- repair shop",
    ),
    (
        "Digital Tech Solutions Phone Accessories",
        schema.UNKNOWN,
        "Kenya",
        "Nairobi",
        "EXPECT REJECT -- accessories shop despite a very techy name",
    ),
]


def build_company_records(start_index: int = 1) -> list[dict[str, str]]:
    """Turn the sample list into Companies-sheet rows.

    Research Status starts at PENDING: nothing has been researched yet,
    and the system must not pretend otherwise.
    """
    from app.excel import today

    records = []
    # `note` is developer commentary about what we EXPECT the AI to decide.
    # It is deliberately not written to the sheet -- if it were, the AI would
    # be reading our answer instead of researching the company.
    for offset, (name, website, country, region, _note) in enumerate(SAMPLE_COMPANIES):
        records.append(
            {
                "Company ID": f"C{start_index + offset:03d}",
                "Company Name": name,
                "Website": website,
                "Country": country,
                "Region": region,
                "Research Status": schema.RESEARCH_PENDING,
                "Source": "Sample data (Stage 2)",
                "Date Added": today(),
            }
        )
    return records
