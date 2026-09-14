"""Choosing which campaign fits a company.

Your specification defines four campaigns. Which one applies depends on
what the company actually does, so we pick it in Python from the
qualification results rather than asking the AI again -- it is a simple
rule, and a deterministic choice is easier to audit than a model's.
"""

import logging

from app import schema

logger = logging.getLogger(__name__)

DATA = "Campaign 1 - Data / Analytics Internship"
SOFTWARE = "Campaign 2 - Software / Technology Internship"
AI_ML = "Campaign 3 - AI / ML Internship"
GENERAL = "Campaign 4 - General Technology Internship"

CAMPAIGNS = [DATA, SOFTWARE, AI_ML, GENERAL]

# Words that point to each campaign, checked against the company's
# "What They Do", "Technology Function" and "Relevant Roles" columns.
#
# Two lessons are baked into this list:
#
# 1. AI/ML is checked first, but its signals must be SPECIFIC. An early
#    version included "data science", which matched almost every company
#    -- partly because the student's own degree is echoed back in the
#    relevant-roles column. Everything became an AI/ML campaign, which was
#    plainly wrong for a bank or a telecom.
# 2. We only count a signal from the company's OWN description columns,
#    never from roles that merely reflect what the student studies.
CAMPAIGN_SIGNALS = [
    (AI_ML, ["machine learning", "artificial intelligence", "ai/ml",
             "deep learning", " nlp ", "computer vision", "ml engineer",
             "ai platform", "ai research"]),
    (DATA, ["data analy", "analytics", "business intelligence", " bi ",
            "data engineer", "data team", "data-driven", "dashboard",
            "statistics", "research centre", "research center"]),
    (SOFTWARE, ["software", "engineering", "developer", "web development",
                "application", "platform", "api", "cloud", "devops",
                "product team", "telecommunication", "fintech"]),
]


def choose_campaign(company: dict) -> str:
    """Pick the campaign that best fits one company.

    Falls back to the general technology campaign when the evidence does
    not clearly point anywhere -- a vaguer email is better than a
    confidently wrong one.
    """
    # Deliberately excludes "Relevant Roles": that column describes what
    # the STUDENT could do, not what the company is, so including it made
    # every company look like a data science company.
    haystack = " ".join(
        str(company.get(column, "")).lower()
        for column in ["What They Do", "Technology Function", "Industry"]
    )
    # Pad so " ai " style checks can match at the edges.
    haystack = f" {haystack} "

    for campaign, signals in CAMPAIGN_SIGNALS:
        if any(signal in haystack for signal in signals):
            return campaign

    return GENERAL


def campaign_focus(campaign: str) -> str:
    """A one-line description of what the email should emphasise."""
    return {
        DATA: (
            "data analysis, reporting and visualisation work -- the student's "
            "strongest area"
        ),
        SOFTWARE: (
            "software and web development, and application support work"
        ),
        AI_ML: (
            "the student's interest in AI/ML, while being clear that their "
            "machine learning experience is at a basic, coursework level"
        ),
        GENERAL: (
            "general technology, IT and data work, kept broad because the "
            "organisation's specific focus is not confirmed"
        ),
    }.get(campaign, "general technology and data work")
