"""Tests for company discovery.

The property that matters: results come from a real search index, and
the AI can only filter them -- never add an organisation of its own.

Run with:   python -m unittest discover tests
"""

import unittest

from app.discover import Candidate, clean_name, existing_domains, to_candidates


def result(url, title, description=""):
    return {"url": url, "title": title, "description": description}


class TestNameCleaning(unittest.TestCase):
    def test_a_title_suffix_is_trimmed(self):
        self.assertEqual(clean_name("Acme Data Ltd | Nairobi's leading firm"), "Acme Data Ltd")
        self.assertEqual(clean_name("Acme Data - Home"), "Acme Data")

    def test_boilerplate_first_segments_are_skipped(self):
        self.assertEqual(clean_name("Home | Acme Data Ltd"), "Acme Data Ltd")

    def test_a_plain_title_survives(self):
        self.assertEqual(clean_name("Acme Data Ltd"), "Acme Data Ltd")


class TestFiltering(unittest.TestCase):
    """A result from a job board is ABOUT a company, not the company."""

    def test_aggregators_and_social_sites_are_dropped(self):
        results = [
            result("https://linkedin.com/company/acme", "Acme | LinkedIn"),
            result("https://brightermonday.co.ke/jobs/x", "Jobs at Acme"),
            result("https://en.wikipedia.org/wiki/Acme", "Acme - Wikipedia"),
            result("https://acmedata.co.ke", "Acme Data Ltd"),
        ]
        candidates = to_candidates(results, "test")
        self.assertEqual([c.domain for c in candidates], ["acmedata.co.ke"])

    def test_subdomains_of_excluded_sites_are_also_dropped(self):
        results = [result("https://ke.linkedin.com/company/acme", "Acme")]
        self.assertEqual(to_candidates(results, "test"), [])

    def test_one_result_per_domain(self):
        results = [
            result("https://acme.co.ke/about", "Acme - About"),
            result("https://acme.co.ke/careers", "Acme - Careers"),
        ]
        self.assertEqual(len(to_candidates(results, "test")), 1)

    def test_the_site_root_is_used_not_the_matched_page(self):
        results = [result("https://acme.co.ke/careers/jobs/123", "Acme Careers")]
        self.assertEqual(to_candidates(results, "test")[0].url, "https://acme.co.ke")

    def test_results_without_a_url_or_title_are_skipped(self):
        results = [result("", "No URL"), result("https://x.co.ke", "")]
        self.assertEqual(to_candidates(results, "test"), [])


class TestDuplicatePrevention(unittest.TestCase):
    def test_domains_already_in_the_workbook_are_recognised(self):
        companies = [
            {"Website": "https://www.acme.co.ke"},
            {"Website": "UNKNOWN"},
            {"Website": ""},
        ]
        self.assertEqual(existing_domains(companies), {"acme.co.ke"})

    def test_www_is_ignored_when_comparing(self):
        companies = [{"Website": "https://www.acme.co.ke"}]
        candidate = Candidate(name="Acme", url="https://acme.co.ke")
        self.assertIn(candidate.domain, existing_domains(companies))


class TestCandidate(unittest.TestCase):
    def test_the_domain_is_derived_from_the_url(self):
        self.assertEqual(Candidate(name="X", url="https://www.a.co.ke").domain, "a.co.ke")


if __name__ == "__main__":
    unittest.main()


class TestFreeHostingFilter(unittest.TestCase):
    """A site on free hosting is a personal project, not an employer.

    A real search returned afyatech.vercel.app for "healthtech company
    Nairobi", and the AI screening kept it. A Vercel subdomain is almost
    always a student project, so it is filtered before screening.
    """

    def test_free_hosting_domains_are_detected(self):
        from app.discover import is_free_hosting

        for url in [
            "https://afyatech.vercel.app",
            "https://project.netlify.app",
            "https://someone.github.io",
            "https://demo.herokuapp.com",
        ]:
            self.assertTrue(is_free_hosting(url), url)

    def test_real_domains_are_not_flagged(self):
        from app.discover import is_free_hosting

        for url in ["https://acme.co.ke", "https://safaricom.co.ke", "https://kemri.go.ke"]:
            self.assertFalse(is_free_hosting(url), url)

    def test_free_hosting_results_are_dropped(self):
        results = [
            result("https://afyatech.vercel.app", "AfyaTech"),
            result("https://realcompany.co.ke", "Real Company Ltd"),
        ]
        self.assertEqual(
            [c.domain for c in to_candidates(results, "t")], ["realcompany.co.ke"]
        )


class TestJobAdvertFiltering(unittest.TestCase):
    """A job-board page is an advert, not an employer."""

    def test_job_advert_titles_are_skipped(self):
        results = [
            result("https://somesite.co.ke/x", "Data Analyst Jobs In Kenya 2026"),
            result("https://othersite.co.ke/y", "Software Vacancies in Nairobi"),
            result("https://realco.co.ke", "Real Co Ltd"),
        ]
        self.assertEqual([c.domain for c in to_candidates(results, "t")], ["realco.co.ke"])

    def test_company_directories_are_excluded(self):
        results = [
            result("https://www.f6s.com/companies/x", "Top 27 Data Companies"),
            result("https://clutch.co/ke/developers", "Top Developers"),
            result("https://realco.co.ke", "Real Co Ltd"),
        ]
        self.assertEqual([c.domain for c in to_candidates(results, "t")], ["realco.co.ke"])


class TestHtmlEntities(unittest.TestCase):
    def test_entities_are_decoded_in_names(self):
        self.assertEqual(clean_name("Data &amp; Analytics Ltd | Home"), "Data & Analytics Ltd")


class TestStagingFileBehaviour(unittest.TestCase):
    """Searching twice must not destroy the first search's results.

    The original version overwrote data/discovered.csv on every run, so
    searching three categories before importing any of them silently lost
    the first two. Results now accumulate, and importing clears the file.
    """

    def test_existing_domains_prevent_restaging(self):
        """A company already in the workbook is never offered again."""
        companies = [{"Website": "https://leta.ai"}]
        known = existing_domains(companies)
        candidate = Candidate(name="Leta", url="https://leta.ai")
        self.assertIn(candidate.domain, known)

    def test_domains_compare_without_www(self):
        companies = [{"Website": "https://www.betaagritech.com"}]
        candidate = Candidate(name="BETA", url="https://betaagritech.com")
        self.assertIn(candidate.domain, existing_domains(companies))
