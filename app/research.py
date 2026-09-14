"""Gather real evidence about a company from its own website.

This module does NOT use AI. It is plain HTTP and text extraction, and it
exists so the AI has actual retrieved text to judge instead of answering
from memory.

The rule this enforces: if we could not fetch anything, we say so. An
empty evidence pack is an honest result, and the qualification prompt
turns it into NEEDS_REVIEW rather than a guess.

Why no BeautifulSoup: stripping tags with the standard library's
HTMLParser is about 30 lines and avoids another dependency. We only need
rough readable text for the AI, not accurate DOM parsing.
"""

import logging
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from app import schema

logger = logging.getLogger(__name__)

# A standard browser User-Agent. Many corporate sites reject unknown
# agents outright with 403, so this is needed to read pages that are
# publicly visible to any human visitor.
#
# The boundary we hold: reading a public page with a normal browser header
# is fine. Working around an active bot challenge (solving CAPTCHAs,
# rotating IPs, spoofing cookies) is NOT, and this project does not do it.
# Sites that genuinely refuse us are recorded as blocked, and "blocked"
# means we have no evidence -- never that the company is unsuitable.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Sent alongside the UA; some CDNs reject requests with no Accept header.
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

REQUEST_TIMEOUT = 15  # seconds; a slow site should not stall the whole run
MAX_TEXT_CHARS = 6000  # per page, to keep AI token cost predictable

# Paths commonly used for careers/jobs pages. We try a few rather than
# crawling the whole site.
CAREER_PATHS = [
    "/careers",
    "/career",
    "/jobs",
    "/join-us",
    "/work-with-us",
    "/about/careers",
    "/company/careers",
]

# Words that suggest a careers link when we scan the homepage's own links.
CAREER_LINK_WORDS = ["career", "job", "vacanc", "join us", "work with us", "hiring"]


# --- HTML -> text ----------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collect visible text, skipping script/style, and collect links."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.links: list[tuple[str, str]] = []  # (href, link text)
        self._skip_depth = 0
        self._current_href: str | None = None
        self._current_link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    self._current_href = value
                    self._current_link_text = []

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "a" and self._current_href:
            text = " ".join(self._current_link_text).strip()
            self.links.append((self._current_href, text))
            self._current_href = None
            self._current_link_text = []

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.chunks.append(text)
            if self._current_href is not None:
                self._current_link_text.append(text)

    def get_text(self) -> str:
        """Joined visible text, with runs of whitespace collapsed."""
        return re.sub(r"\s+", " ", " ".join(self.chunks)).strip()


# --- Evidence containers ---------------------------------------------------

@dataclass
class Page:
    """One successfully fetched page."""

    url: str
    text: str


@dataclass
class EvidencePack:
    """Everything we actually retrieved about one company.

    `notes` records what went wrong (timeouts, 404s, no website on file).
    Those notes are shown to the AI too -- knowing that a fetch FAILED is
    itself useful evidence, and prevents the model from assuming absence
    of data means absence of a careers page.
    """

    company_name: str
    website: str = ""
    pages: list[Page] = field(default_factory=list)
    careers_url: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def has_evidence(self) -> bool:
        """True when at least one page was actually retrieved."""
        return bool(self.pages)

    def to_prompt_text(self) -> str:
        """Render the pack as the EVIDENCE block given to the AI."""
        if not self.has_evidence:
            reasons = "; ".join(self.notes) or "no attempt recorded"
            return (
                "NO EVIDENCE RETRIEVED.\n"
                f"Reason: {reasons}\n"
                "You have no retrieved text about this company. "
                "Judge accordingly -- do not rely on prior knowledge."
            )

        parts = []
        for page in self.pages:
            parts.append(f"--- SOURCE: {page.url} ---\n{page.text}")
        if self.notes:
            parts.append("--- FETCH NOTES ---\n" + "\n".join(self.notes))
        return "\n\n".join(parts)


# --- Fetching --------------------------------------------------------------

def _normalise_url(raw: str) -> str:
    """Add https:// if the scheme is missing; return '' for placeholders."""
    url = (raw or "").strip()
    if not url or url.upper() == schema.UNKNOWN:
        return ""
    if not urlparse(url).scheme:
        url = "https://" + url
    return url


def fetch_page(url: str) -> tuple[Page | None, str | None]:
    """Fetch one URL and extract its text.

    Returns (page, error_note). Exactly one of the two is None, so the
    caller always knows whether it got data or a reason it did not.

    Every network failure mode is caught and turned into a readable note
    rather than an exception -- one dead website must not abort a run of
    ten companies.
    """
    try:
        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers=DEFAULT_HEADERS,
            allow_redirects=True,
        )
    except requests.exceptions.SSLError:
        return None, f"{url}: SSL certificate error"
    except requests.exceptions.ConnectionError as exc:
        # Covers DNS failure, refused connections, unreachable hosts.
        if isinstance(exc.__cause__, socket.gaierror) or "NameResolution" in str(exc):
            return None, f"{url}: domain does not resolve (site may not exist)"
        return None, f"{url}: could not connect"
    except requests.exceptions.Timeout:
        return None, f"{url}: timed out after {REQUEST_TIMEOUT}s"
    except requests.exceptions.RequestException as exc:
        return None, f"{url}: request failed ({type(exc).__name__})"

    if response.status_code == 404:
        return None, f"{url}: not found (404)"
    if response.status_code in (401, 403, 429):
        # The site is actively refusing automated access. This tells us
        # nothing about the company -- only that we could not look.
        return None, (
            f"{url}: access blocked by the website (HTTP {response.status_code}). "
            "No conclusion can be drawn from this."
        )
    if response.status_code >= 400:
        return None, f"{url}: HTTP {response.status_code}"

    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        return None, f"{url}: not an HTML page ({content_type or 'unknown type'})"

    parser = _TextExtractor()
    try:
        parser.feed(response.text)
    except Exception as exc:
        return None, f"{url}: could not parse HTML ({exc})"

    text = parser.get_text()
    if len(text) < 50:
        return None, f"{url}: page had almost no readable text (likely JavaScript-rendered)"

    # Keep the final URL after redirects -- it is what we actually read.
    return Page(url=response.url, text=text[:MAX_TEXT_CHARS]), None


def _find_careers_link(homepage_html_links: list[tuple[str, str]], base_url: str) -> str:
    """Pick the most likely careers link from the homepage's own links.

    Preferring a real link over guessed paths means we follow what the
    site actually publishes.
    """
    for href, text in homepage_html_links:
        haystack = f"{href} {text}".lower()
        if any(word in haystack for word in CAREER_LINK_WORDS):
            return urljoin(base_url, href)
    return ""


def research_company(name: str, website: str) -> EvidencePack:
    """Gather evidence for one company: homepage, then a careers page.

    Deliberately shallow -- two pages, not a crawl. It is enough for the
    AI to tell a software company from a phone shop, and it keeps the run
    fast and polite to the sites we visit.
    """
    pack = EvidencePack(company_name=name)

    url = _normalise_url(website)
    if not url:
        pack.notes.append(
            "No website recorded for this company (Website column is blank or UNKNOWN)."
        )
        logger.info("%s: no website to fetch", name)
        return pack

    pack.website = url
    logger.info("%s: fetching %s", name, url)

    homepage, error = fetch_page(url)
    if error:
        pack.notes.append(error)
        logger.warning("%s: %s", name, error)
        return pack

    pack.pages.append(homepage)

    # Look for a careers page: first via the homepage's own links, then
    # by trying a couple of conventional paths.
    parser = _TextExtractor()
    candidates: list[str] = []
    try:
        response = requests.get(
            url, timeout=REQUEST_TIMEOUT, headers=DEFAULT_HEADERS
        )
        parser.feed(response.text)
        found = _find_careers_link(parser.links, response.url)
        if found:
            candidates.append(found)
    except Exception:
        pass  # link discovery is best-effort; the fallback paths still apply

    base = f"{urlparse(homepage.url).scheme}://{urlparse(homepage.url).netloc}"
    candidates.extend(base + path for path in CAREER_PATHS[:3])

    for candidate in dict.fromkeys(candidates):  # de-duplicate, keep order
        if candidate.rstrip("/") == homepage.url.rstrip("/"):
            continue
        page, error = fetch_page(candidate)
        if page:
            pack.pages.append(page)
            pack.careers_url = page.url
            logger.info("%s: found careers page %s", name, page.url)
            break

    if not pack.careers_url:
        pack.notes.append(
            "No careers page found at the usual paths. This is not proof that "
            "none exists."
        )

    return pack
