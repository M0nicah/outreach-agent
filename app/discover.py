"""Finding new companies through real web search.

The distinction that matters: this searches an actual web index, not the
AI's memory. Every candidate comes from a page that really exists, and
its domain is verified before it reaches your workbook.

That matters because the obvious alternative -- asking an LLM to list
companies -- produces confident, plausible, and sometimes entirely
fictional results. This project already hit that: two companies suggested
from model memory turned out to have dead domains.

The AI still has a job here, but a narrow one: given real search results,
decide which of them are organisations worth researching further. It
never invents a name or a URL.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import requests

from app import schema
from app.config import Config

logger = logging.getLogger(__name__)

BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DUCKDUCKGO_ENDPOINT = "https://html.duckduckgo.com/html/"
REQUEST_TIMEOUT = 20

# Be a polite client of a free service: one query at a time, with a pause.
DUCKDUCKGO_DELAY = 2.0

# Domains that are never the company we want -- aggregators, directories,
# social networks and news sites. A result from one of these is about a
# company, not the company itself.
EXCLUDED_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "tiktok.com", "wikipedia.org", "crunchbase.com",
    "glassdoor.com", "indeed.com", "brightermonday.co.ke", "fuzu.com",
    "myjobmag.co.ke", "jobwebkenya.com", "kenyajob.com", "pigiame.co.ke",
    "jiji.co.ke", "yellowpageskenya.com", "medium.com", "reddit.com",
    "bloomberg.com", "reuters.com", "businessdailyafrica.com", "nation.africa",
    "standardmedia.co.ke", "the-star.co.ke", "techcabal.com", "disrupt-africa.com",
    "github.com", "google.com", "amazon.com",
    # Company-listing directories. These rank highly for "top X companies
    # in Y" searches, but a directory page is a list ABOUT companies, not
    # a company. Worth browsing yourself; not worth importing as a row.
    "f6s.com", "goodfirms.co", "clutch.co", "techbehemoths.com",
    "ensun.io", "startupblink.com", "trustpilot.com", "yelp.com",
    "designrush.com", "sortlist.com", "manifest.com", "toprate.co.ke",
    "businesslist.co.ke", "kenyaplex.com", "vconnect.com",
    # More job boards. A job-board page is an advert, not an employer.
    "careerjet.co.ke", "recruit.net", "corporatestaffing.co.ke",
    "getclera.com", "jobsinkenya.co.ke", "kazitoday.com", "careerpointkenya.co.ke",
    "ngojobsinafrica.com", "unjobs.org", "devex.com", "idealist.org",
}


class DiscoverError(Exception):
    """Raised when search cannot be performed."""


# Free hosting subdomains. A real employer has its own domain; a site on
# one of these is nearly always a personal project or a demo, and is not
# an organisation that could host an intern.
FREE_HOSTING_SUFFIXES = (
    ".vercel.app", ".netlify.app", ".github.io", ".herokuapp.com",
    ".wordpress.com", ".blogspot.com", ".wixsite.com", ".weebly.com",
    ".firebaseapp.com", ".web.app", ".pages.dev", ".replit.app",
    ".glitch.me", ".surge.sh", ".onrender.com",
)


def is_free_hosting(url: str) -> bool:
    """True when the URL sits on a free hosting subdomain."""
    host = urlparse(url).netloc.lower()
    return host.endswith(FREE_HOSTING_SUFFIXES)


@dataclass
class Candidate:
    """One organisation found through search."""

    name: str
    url: str
    description: str = ""
    query: str = ""

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower().removeprefix("www.")


# --- Searching -------------------------------------------------------------
#
# Two back ends:
#
#   duckduckgo -- the default. No API key, no account, no card. It reads
#                 the same HTML results page a person would see. Fewer
#                 results per query and no formal service guarantee, but
#                 it costs nothing and needs no signup.
#   brave      -- a proper API. Better and more reliable results, $5 per
#                 1,000 queries with $5 of free credit each month. A
#                 credit card is required at signup even for the free
#                 credit, which is why it is not the default.


def search_duckduckgo(query: str, count: int = 20) -> list[dict[str, Any]]:
    """Search DuckDuckGo's HTML endpoint. No key required.

    We read the plain results page rather than an API, so the parsing is
    tied to their markup and could break if they change it. That is the
    honest trade for needing no account: if it stops returning results,
    switch to Brave rather than assuming there is nothing to find.
    """
    from app.research import DEFAULT_HEADERS

    try:
        response = requests.post(
            DUCKDUCKGO_ENDPOINT,
            data={"q": query},
            headers=DEFAULT_HEADERS,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise DiscoverError(f"Search request failed: {exc}") from exc

    if response.status_code == 429:
        raise DiscoverError(
            "DuckDuckGo is rate-limiting us (HTTP 429). Wait 5-15 minutes, then "
            "run ONE search rather than a --preset. Nothing was lost."
        )
    if response.status_code >= 400:
        raise DiscoverError(f"DuckDuckGo returned HTTP {response.status_code}.")

    html = response.text
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)

    if not links:
        # Two causes, and they need different responses, so say both.
        if "anomaly" in html.lower() or "unusual traffic" in html.lower():
            raise DiscoverError(
                "DuckDuckGo is temporarily rate-limiting us.\n"
                "         This is not an error in your setup and nothing was "
                "lost -- anything\n"
                "         already in data/discovered.csv is still there.\n\n"
                "         What to do:\n"
                "           - Wait 5-15 minutes. It clears on its own.\n"
                "           - Then run ONE search rather than a --preset "
                "(a preset fires\n"
                "             three searches at once, which is what triggers "
                "this).\n"
                "           - Or use --engine brave if you have SEARCH_API_KEY set."
            )
        logger.warning(
            "DuckDuckGo returned no results for this query. Either nothing "
            "matched, or their page layout changed. Try rephrasing, or use "
            "--engine brave if you have a key."
        )
        return []

    def strip_tags(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text)).strip()

    results = []
    for index, link in enumerate(links[:count]):
        # DuckDuckGo wraps some links in a redirect; unwrap it.
        actual = link
        match = re.search(r"uddg=([^&]+)", link)
        if match:
            from urllib.parse import unquote

            actual = unquote(match.group(1))

        results.append({
            "url": actual,
            "title": strip_tags(titles[index]) if index < len(titles) else "",
            "description": strip_tags(snippets[index]) if index < len(snippets) else "",
        })

    return results


def search_serper(config: Config, query: str, count: int = 20) -> list[dict[str, Any]]:
    """Search via Serper.dev (a Google results API).

    Needs SERPER_API_KEY in .env. Included because it is a common free
    tier; verify the current allowance at serper.dev before relying on it.
    """
    api_key = config.serper_api_key
    if not api_key:
        raise DiscoverError(
            "SERPER_API_KEY is not set, which the serper engine needs.\n"
            "Get a key at https://serper.dev, then add it to .env."
        )

    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": min(count, 20), "gl": "ke"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise DiscoverError(f"Serper request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise DiscoverError("Serper rejected the API key. Check SERPER_API_KEY in .env.")
    if response.status_code == 429:
        raise DiscoverError("Serper quota reached. Check your allowance at serper.dev.")
    if response.status_code >= 400:
        raise DiscoverError(f"Serper returned HTTP {response.status_code}.")

    try:
        payload = response.json()
    except ValueError as exc:
        raise DiscoverError(f"Serper returned invalid JSON: {exc}") from exc

    return [
        {"url": r.get("link", ""), "title": r.get("title", ""),
         "description": r.get("snippet", "")}
        for r in payload.get("organic", [])
    ]


# Engines in preference order when falling back. DuckDuckGo first because
# it needs no key; the others are only reachable if you have configured
# their keys.
ENGINES = {
    "duckduckgo": lambda config, q, n: search_duckduckgo(q, n),
    "brave": lambda config, q, n: search_brave(config, q, n),
    "serper": lambda config, q, n: search_serper(config, q, n),
}

FALLBACK_ORDER = ["duckduckgo", "brave", "serper"]


def available_engines(config: Config) -> list[str]:
    """Engines we could actually use, given the keys configured."""
    usable = ["duckduckgo"]  # never needs a key
    if config.search_api_key:
        usable.append("brave")
    if getattr(config, "serper_api_key", None):
        usable.append("serper")
    return usable


def search(
    config: Config, query: str, count: int = 20, engine: str = "duckduckgo",
    fallback: bool = True,
) -> list[dict[str, Any]]:
    """Run a search, optionally falling back to another engine.

    A rate limit on one free engine should not end the run if another is
    configured. We try the chosen engine first, then any others that have
    keys, and only give up when all of them refuse.
    """
    caller = ENGINES.get(engine)
    if caller is None:
        raise DiscoverError(
            f"Unknown engine {engine!r}. Available: {', '.join(sorted(ENGINES))}."
        )

    order = [engine]
    if fallback:
        order += [e for e in FALLBACK_ORDER
                  if e != engine and e in available_engines(config)]

    errors: list[str] = []
    for name in order:
        try:
            results = ENGINES[name](config, query, count)
            if name != engine:
                logger.info("Fell back to the %s engine for this query.", name)
            return results
        except DiscoverError as exc:
            errors.append(f"{name}: {exc}")
            continue

    raise DiscoverError(
        "Every available search engine refused this query.\n         "
        + "\n         ".join(errors)
    )



def search_brave(config: Config, query: str, count: int = 20) -> list[dict[str, Any]]:
    """Run one Brave web search and return the raw results."""
    api_key = config.search_api_key
    if not api_key:
        raise DiscoverError(
            "No SEARCH_API_KEY set, which the brave engine needs.\n"
            "Brave costs $5 per 1,000 queries and includes $5 of free credit "
            "each month,\nbut asks for a credit card at signup.\n"
            "To search without any key or card, use the default engine:\n"
            "    python main.py discover --preset startups"
        )

    try:
        response = requests.get(
            BRAVE_ENDPOINT,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
            params={"q": query, "count": min(count, 20), "country": "ke"},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        raise DiscoverError(f"Search request failed: {exc}") from exc

    if response.status_code == 401:
        raise DiscoverError(
            "Brave rejected the API key. Check SEARCH_API_KEY in your .env file."
        )
    if response.status_code == 429:
        raise DiscoverError(
            "Brave rate limit reached. The free tier allows one query per "
            "second and 2,000 a month. Wait a moment and try again."
        )
    if response.status_code >= 400:
        raise DiscoverError(f"Brave returned HTTP {response.status_code}: "
                            f"{response.text[:200]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise DiscoverError(f"Brave returned something that is not JSON: {exc}") from exc

    return payload.get("web", {}).get("results", [])


def clean_name(title: str) -> str:
    """Turn a page title into something like an organisation name.

    Titles look like "Acme Data Ltd | Home - Nairobi's leading...". We
    keep the first segment, which is usually the name. This is a guess
    about FORMATTING, not about facts -- the name still comes from the
    real page.
    """
    import html as html_module

    # Search results carry HTML entities: "Data &amp; Analytics".
    title = html_module.unescape(title)
    name = re.split(r"\s*[|\-–—:]\s*", title.strip())[0].strip()

    # Drop boilerplate suffixes that are not part of a name.
    for junk in ["Home", "Homepage", "Welcome", "Official Site", "Official Website"]:
        if name.lower() == junk.lower():
            # The first segment was boilerplate; try the second.
            parts = re.split(r"\s*[|\-–—:]\s*", title.strip())
            name = parts[1].strip() if len(parts) > 1 else title.strip()
            break

    return name[:80]


def to_candidates(results: list[dict[str, Any]], query: str) -> list[Candidate]:
    """Turn raw search results into candidates, dropping the obvious noise."""
    candidates: list[Candidate] = []
    seen_domains: set[str] = set()

    for result in results:
        url = str(result.get("url", "")).strip()
        title = str(result.get("title", "")).strip()
        if not url or not title:
            continue

        domain = urlparse(url).netloc.lower().removeprefix("www.")

        # Skip aggregators, social networks and news sites: a result from
        # one of those is ABOUT a company, not the company's own site.
        if any(domain == bad or domain.endswith("." + bad) for bad in EXCLUDED_DOMAINS):
            continue

        # Free hosting means a personal project, not an employer.
        if any(domain.endswith(suffix.lstrip(".")) or domain.endswith(suffix)
               for suffix in FREE_HOSTING_SUFFIXES):
            logger.debug("Skipping free-hosting domain: %s", domain)
            continue

        # One result per domain -- deeper pages on the same site are the
        # same organisation.
        if domain in seen_domains:
            continue
        seen_domains.add(domain)

        # A title that reads like a job advert is not an organisation name.
        if re.search(
            r"\b(jobs?|vacanc\w*|hiring|recruitment|career opportunit\w*)\b",
            title, re.I,
        ):
            logger.debug("Skipping job-advert style result: %s", title[:60])
            continue

        candidates.append(
            Candidate(
                name=clean_name(title),
                # Use the site root: we want the organisation, not the
                # particular page that matched.
                url=f"{urlparse(url).scheme}://{urlparse(url).netloc}",
                description=str(result.get("description", ""))[:400],
                query=query,
            )
        )

    return candidates


def verify_reachable(candidate: Candidate) -> tuple[bool, str]:
    """Check the site actually loads before we add it.

    Adding an unreachable domain is how the workbook fills up with
    NEEDS_REVIEW rows that can never be researched.
    """
    from app.research import fetch_page

    page, error = fetch_page(candidate.url)
    if page:
        return True, "reachable"
    # A site that blocks bots still exists, and is worth keeping.
    if error and "blocked" in error:
        return True, "reachable but blocks automated access"
    return False, error or "could not be reached"


def existing_domains(companies: list[dict[str, Any]]) -> set[str]:
    """Domains already in the workbook, for duplicate prevention."""
    domains = set()
    for company in companies:
        website = str(company.get("Website", "")).strip()
        if website and website.upper() != schema.UNKNOWN:
            host = urlparse(website).netloc.lower().removeprefix("www.")
            if host:
                domains.add(host)
    return domains
