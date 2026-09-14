"""The workbook schema: sheet names, columns, and allowed values.

Kept separate from excel.py so you can read the shape of the database
without wading through openpyxl code. Every other module imports its
column names and status values FROM HERE rather than typing string
literals, so a typo becomes an ImportError instead of a silently empty
Excel column.
"""

# --- Sheet names -----------------------------------------------------------

COMPANIES = "Companies"
CONTACTS = "Contacts"
OUTREACH = "Outreach"
SETTINGS = "Settings"

SHEET_NAMES = [COMPANIES, CONTACTS, OUTREACH, SETTINGS]


# --- Columns ---------------------------------------------------------------
# Order matters: this is the left-to-right column order in the spreadsheet.

COMPANY_COLUMNS = [
    "Company ID",
    "Company Name",
    "Website",
    "Country",
    "Region",
    "Industry",
    "Company Type",
    "Company Size",
    "What They Do",
    "Technology Function",
    "Internship Evidence",
    "Careers URL",
    "Internship URL",
    "Remote Possibility",
    "Relevant Roles",
    "Technology Score",
    "Internship Score",
    "Skills Match Score",
    "Maturity Score",
    "Contactability Score",
    "Geographic Score",
    "Total Score",
    "Priority",
    "Qualification Reason",
    "Research Status",
    "Source",
    "Date Added",
]

CONTACT_COLUMNS = [
    "Contact ID",
    "Company ID",
    "Company Name",
    "Contact Name",
    "Contact Role",
    "Contact Type",
    "Email",
    "Email Verified",
    "LinkedIn",
    "Source",
    "Why This Contact",
    "Contact Status",
]

OUTREACH_COLUMNS = [
    "Outreach ID",
    "Company ID",
    "Contact ID",
    "Company Name",
    "Contact Name",
    "Campaign",
    "Subject",
    "Email Body",
    "Approval Status",
    "Email Status",
    "Date Drafted",
    "Date Approved",
    "Date Sent",
    "Follow Up 1",
    "Follow Up 2",
    "Reply Status",
    "Reply Date",
    "Reply Summary",
    "Next Action",
    "Notes",
]

SETTINGS_COLUMNS = ["Key", "Value"]

SHEET_COLUMNS = {
    COMPANIES: COMPANY_COLUMNS,
    CONTACTS: CONTACT_COLUMNS,
    OUTREACH: OUTREACH_COLUMNS,
    SETTINGS: SETTINGS_COLUMNS,
}


# --- Allowed values --------------------------------------------------------
# The placeholder used whenever something cannot be verified. The system
# must prefer this over inventing a value.
UNKNOWN = "UNKNOWN"

# Research Status (Companies)
RESEARCH_PENDING = "PENDING"
RESEARCH_QUALIFY = "QUALIFY"
RESEARCH_NEEDS_REVIEW = "NEEDS_REVIEW"
RESEARCH_REJECT = "REJECT"
RESEARCH_ERROR = "ERROR"

RESEARCH_STATUSES = [
    RESEARCH_PENDING,
    RESEARCH_QUALIFY,
    RESEARCH_NEEDS_REVIEW,
    RESEARCH_REJECT,
    RESEARCH_ERROR,
]

# Priority bands (Stage 4 scoring)
PRIORITIES = ["A", "B", "C", "D", "REJECT"]

# Approval Status (Outreach) -- the human gate
APPROVAL_PENDING = "PENDING"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"
APPROVAL_EDITED = "EDITED"

APPROVAL_STATUSES = [
    APPROVAL_PENDING,
    APPROVAL_APPROVED,
    APPROVAL_REJECTED,
    APPROVAL_EDITED,
]

# Email Status (Outreach)
EMAIL_NOT_STARTED = "NOT_STARTED"
EMAIL_DRAFTED = "DRAFTED"
EMAIL_APPROVED = "APPROVED"
EMAIL_SENT = "SENT"
EMAIL_FAILED = "FAILED"

EMAIL_STATUSES = [
    EMAIL_NOT_STARTED,
    EMAIL_DRAFTED,
    EMAIL_APPROVED,
    EMAIL_SENT,
    EMAIL_FAILED,
]

# Reply Status (Outreach)
REPLY_NONE = "NO_REPLY"
REPLY_STATUSES = [
    REPLY_NONE,
    "POSITIVE",
    "NEGATIVE",
    "REFERRAL",
    "CV_REQUEST",
    "INTERVIEW",
    "OTHER",
]

# Contact Type (Contacts)
CONTACT_TYPES = [
    "Internship/Graduate Recruitment",
    "Talent Acquisition",
    "Recruiter",
    "HR",
    "Hiring Manager",
    "Department Manager",
    "CTO",
    "Head of Engineering",
    "Head of Data",
    "Engineering Manager",
    "Data/Analytics Manager",
    "Founder",
    "Other",
]

# Research confidence levels (Stage 3)
VERIFIED = "VERIFIED"
INFERRED = "INFERRED"
EVIDENCE_LEVELS = [VERIFIED, INFERRED, UNKNOWN]


# --- Default Settings sheet contents --------------------------------------
# Your reusable profile. Anything not supplied is left as a placeholder for
# you to fill in by hand -- the system must not fabricate details about you.

DEFAULT_SETTINGS = [
    ("Name", "Monica Wughanga Masae"),
    ("University", "Open University of Kenya"),
    ("Degree", "BSc Data Science"),
    ("Graduation", "2027"),
    ("Email", "FILL_IN"),
    ("LinkedIn", "FILL_IN"),
    ("GitHub", "FILL_IN"),
    ("Portfolio", "FILL_IN"),
    (
        "Target Roles",
        "Data Analyst; Data Science Intern; Data/Research Assistant; "
        "Business Intelligence; Data Operations; IT; Technology; "
        "Software/Web Development; AI/ML; Application Support",
    ),
    ("Availability", "FILL_IN"),
    ("Internship Type", "Internship / Industrial Attachment"),
    ("Preferred Locations", "FILL_IN"),
    (
        "Skills",
        "Python; R; Excel; Data Analysis; Data Visualization; Tableau exposure; "
        "Statistics; Basic Machine Learning; SQL; Web Development; "
        "Software Projects; Technical Documentation; Reporting; "
        "AI-powered applications",
    ),
    ("Experience Level", "Student / early-career"),
]
