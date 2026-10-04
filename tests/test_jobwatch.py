"""Тесты фильтров. Запуск: python -m unittest discover -s tests -v"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jobwatch as jw  # noqa: E402

CFG = jw.load_config()
RECENT = date.today() - timedelta(days=2)


JOB_ARGS = {"sal_min", "sal_max", "currency", "sal_text"}


def job(title, text="rest api sql troubleshooting", loc="Worldwide", company="Acme", **kw):
    posted = kw.pop("posted", RECENT)
    ctor = {k: kw.pop(k) for k in list(kw) if k in JOB_ARGS}
    j = jw._job("Test", title, company, loc, "https://example.com", text, posted, **ctor)
    j.update(kw)  # direct, remote_flag и прочие поля, которые выставляют сборщики
    return j


def score(j, cooldown=frozenset()):
    return jw.score(j, CFG, set(cooldown))


class WordBoundaries(unittest.TestCase):
    def test_international_is_not_intern(self):
        s, why = score(job("International Support Engineer"))
        self.assertIsNotNone(s, why)

    def test_internal_is_not_intern(self):
        s, why = score(job("Internal Tools Support Engineer"))
        self.assertIsNotNone(s, why)

    def test_internship_dropped(self):
        s, why = score(job("Technical Support Internship"))
        self.assertIsNone(s)

    def test_iam_does_not_match_william(self):
        s, why = score(job("Office Coordinator", text="report to William, diamond district"))
        self.assertIsNone(s)
        self.assertIn("нет ключевых слов роли", why)

    def test_sales_engineer_passes(self):
        s, why = score(job("Sales Engineer", text="pre-sales demos, rest api, saas"))
        self.assertIsNotNone(s, why)

    def test_account_executive_dropped(self):
        s, _ = score(job("Account Executive"))
        self.assertIsNone(s)

    def test_salesforce_not_blocked(self):
        s, why = score(job("Salesforce Support Engineer"))
        self.assertIsNotNone(s, why)


class TextCleaning(unittest.TestCase):
    def test_html_entities_decoded(self):
        j = job("Support Engineer", text="Experience with REST&nbsp;API and B2B&amp;SaaS")
        s, why = score(j)
        self.assertIn("rest api", " ".join(why))

    def test_escaped_html_stripped(self):
        self.assertEqual(jw.clean("&lt;p&gt;SQL&lt;/p&gt;").strip(), "sql")


class Location(unittest.TestCase):
    def test_usa_exact_blocked(self):
        s, _ = score(job("Support Engineer", loc="USA"))
        self.assertIsNone(s)

    def test_us_only_phrase_blocked(self):
        s, _ = score(job("Support Engineer", text="sql. Must reside in the US."))
        self.assertIsNone(s)

    def test_direct_onsite_city_dropped(self):
        s, why = score(job("Support Engineer", loc="Amsterdam", direct=True, remote_flag=False))
        self.assertIsNone(s)

    def test_direct_without_remote_signal_dropped(self):
        s, why = score(job("Support Engineer", loc="", direct=True, remote_flag=False))
        self.assertIsNone(s)
        self.assertIn("не удалёнка", why[0])

    def test_direct_remote_passes_with_bonus(self):
        s, why = score(job("Support Engineer", loc="Remote - EMEA", direct=True))
        self.assertIsNotNone(s, why)
        self.assertIn("★ целевая компания", why)


class Filters(unittest.TestCase):
    def test_low_salary_dropped(self):
        s, why = score(job("Support Engineer", sal_min=900, sal_max=1100, currency="USD"))
        self.assertIsNone(s)

    def test_salary_text_eur_parsed(self):
        lo, hi = jw.salary_usd(job("X", sal_text="€40k – €55k"))
        self.assertGreater(lo, 40000)
        self.assertGreater(hi, lo)

    def test_gambling_dropped(self):
        s, _ = score(job("Support Engineer", text="igaming casino platform, sql"))
        self.assertIsNone(s)

    def test_old_posting_dropped(self):
        s, _ = score(job("Support Engineer", posted=date.today() - timedelta(days=60)))
        self.assertIsNone(s)

    def test_cooldown(self):
        s, _ = score(job("Support Engineer", company="Nebius B.V."), cooldown={"nebius"})
        self.assertIsNone(s)

    def test_russian_bonus(self):
        base, _ = score(job("Support Engineer"))
        ru, why = score(job("Support Engineer", text="rest api sql troubleshooting, fluent in russian"))
        self.assertGreater(ru, base)
        self.assertIn("★ нужен русский", why)

    def test_us_hours_penalty(self):
        base, _ = score(job("Support Engineer"))
        us, _ = score(job("Support Engineer", text="rest api sql troubleshooting, EST hours"))
        self.assertLess(us, base)


class Output(unittest.TestCase):
    def test_tracker_line_escapes_commas(self):
        line = jw.tracker_line({"company": "Acme, Inc", "title": "Support"})
        self.assertIn('"Acme, Inc"', line)

    def test_digest_caps_per_company(self):
        jobs = [dict(score=10, title=f"Role {i}", company="Acme", url="u", salary="", lang=None)
                for i in range(5)]
        text = jw.telegram_digest(jobs, 5, 8)
        self.assertEqual(text.count("— Acme"), 2)


LITTELFUSE_TEXT = (
    "Littelfuse is a global manufacturer of technologies in circuit protection, serving "
    "customers worldwide. The HRIS Lead Analyst is a highly experienced Workday professional "
    "who delivers solutions through configuration, testing, validation, and implementation. "
    "Qualifications: seven plus years of relevant HRIS, HR technology, or enterprise "
    "application experience. Troubleshooting guidance. Integrations."
)


class RealWorldRegressions(unittest.TestCase):
    """Вакансии, которые реально пролезли в выдачу и не должны были."""

    def test_littelfuse_hris_italy_only(self):
        j = job("Lead Analyst, HRIS (Workday & AI)", text=LITTELFUSE_TEXT, loc="Italy",
                company="Littelfuse", sal_min=55000, sal_max=70000, currency="EUR")
        s, why = score(j)
        self.assertIsNone(s, why)


class LocationWhitelist(unittest.TestCase):
    def test_single_country_dropped(self):
        for loc in ("Italy", "India", "Germany", "Berlin", "Poland, Spain", "Remote (US)", "Americas"):
            s, why = score(job("Support Engineer", loc=loc))
            self.assertIsNone(s, f"{loc}: {why}")

    def test_us_based_in_title_dropped(self):
        s, why = score(job("Live Technical Support Representative (Location: Remote, U.S.-based)",
                           loc="Remote"))
        self.assertIsNone(s, why)

    def test_remoto_is_neutral(self):
        s, why = score(job("Support Engineer", loc="Remoto"))
        self.assertIsNotNone(s, why)

    def test_internationally_located_accepted(self):
        s, why = score(job("Support Engineer", loc="Internationally located (not in US)"))
        self.assertIn("глобальный найм", why)

    def test_new_role_titles(self):
        for t in ("Product Support Specialist", "Support Analyst", "Technical Consultant"):
            s, why = score(job(t))
            self.assertIsNotNone(s, f"{t}: {why}")

    def test_plain_remote_is_neutral(self):
        s, why = score(job("Support Engineer", loc="Remote"))
        self.assertIsNotNone(s, why)
        self.assertNotIn("глобальный найм", why)

    def test_asia_accepted(self):
        s, why = score(job("Support Engineer", loc="APAC"))
        self.assertIn("глобальный найм", why)

    def test_europe_soft_penalty(self):
        base, _ = score(job("Support Engineer", loc="Remote"))
        eu, why = score(job("Support Engineer", loc="Remote - Europe"))
        self.assertIsNotNone(eu, why)
        self.assertLess(eu, base)

    def test_relocation_overrides_country(self):
        s, why = score(job("Support Engineer", loc="Netherlands",
                           text="rest api sql troubleshooting. Visa sponsorship and relocation support."))
        self.assertIsNotNone(s, why)
        self.assertTrue(any("релокация" in w for w in why))

    def test_global_in_company_description_gives_no_bonus(self):
        s, why = score(job("Support Engineer", loc="Remote",
                           text="rest api sql. We are a global company serving customers worldwide."))
        self.assertNotIn("глобальный найм", why)

    def test_hire_globally_phrase_gives_bonus(self):
        s, why = score(job("Support Engineer", loc="Remote",
                           text="rest api sql. We hire globally through Deel."))
        self.assertIn("глобальный найм", why)


class TitleAndForms(unittest.TestCase):
    def test_role_only_in_text_rejected(self):
        s, why = score(job("Data Analyst", text="implementation of dashboards, sql"))
        self.assertIsNone(s)
        self.assertIn("роль не в заголовке", why)

    def test_plural_integrations_matches(self):
        s, why = score(job("Integrations Manager"))
        self.assertIsNotNone(s, why)

    def test_troubleshooting_counts_as_skill(self):
        _, why = score(job("Support Engineer", text="troubleshooting customer issues"))
        self.assertIn("troubleshoot", " ".join(why))


class Experience(unittest.TestCase):
    def test_parses_digits_and_words(self):
        self.assertEqual(jw.required_years("3+ years of experience in support"), 3)
        self.assertEqual(jw.required_years("seven plus years of relevant hris experience"), 7)
        self.assertIsNone(jw.required_years("founded in 1927, 16,000 employees"))

    def test_senior_requirement_penalized(self):
        base, _ = score(job("Support Engineer", text="rest api sql, 3+ years of experience"))
        sen, why = score(job("Support Engineer", text="rest api sql, 8+ years of experience"))
        self.assertLess(sen, base)
        self.assertIn("требуют 8+ лет", why)


def hh_job(title, sal_from=None, sal_to=None, text="sql, rest api"):
    j = jw._job("hh.ru", title, "ООО Ромашка", "Удалённо (hh.ru)", "https://hh.ru/x",
                f"{title} {text}", RECENT, sal_from, sal_to, "RUR")
    j.update(period="month", skip_location=True)
    return j


class HeadHunter(unittest.TestCase):
    def test_russian_role_title(self):
        s, why = score(hh_job("Специалист технической поддержки"))
        self.assertIsNotNone(s, why)
        self.assertIn("русскоязычная (hh)", why)

    def test_russian_integration_title(self):
        s, why = score(hh_job("Инженер по интеграциям"))
        self.assertIsNotNone(s, why)

    def test_operator_dropped(self):
        s, _ = score(hh_job("Оператор технической поддержки"))
        self.assertIsNone(s)

    def test_monthly_rub_above_floor_passes(self):
        s, why = score(hh_job("Инженер технической поддержки", 130000, 160000))
        self.assertIsNotNone(s, why)
        j = hh_job("Инженер технической поддержки", 130000, 160000)
        score(j)
        self.assertEqual(j["salary"], "130–160k ₽/мес")

    def test_monthly_rub_below_floor_dropped(self):
        s, why = score(hh_job("Инженер технической поддержки", 60000, 80000))
        self.assertIsNone(s)
        self.assertIn("зарплата ниже порога", why)

    def test_moscow_area_not_dropped(self):
        j = hh_job("Инженер поддержки")
        j["location"] = "Москва"
        s, why = score(j)
        self.assertIsNotNone(s, why)

    def test_russian_gambling_dropped(self):
        s, _ = score(hh_job("Специалист поддержки", text="букмекерская компания, sql"))
        self.assertIsNone(s)


class Commands(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(jw.parse_command("+105"), ("applied", ["105"]))
        self.assertEqual(jw.parse_command("+105 107, 110"), ("applied", ["105", "107", "110"]))
        self.assertEqual(jw.parse_command("Интервью 105"), ("interview", ["105"]))
        self.assertEqual(jw.parse_command("отказ 105"), ("rejected", ["105"]))
        self.assertEqual(jw.parse_command("/stats"), ("stats", []))
        self.assertEqual(jw.parse_command("/start"), ("help", []))
        self.assertEqual(jw.parse_command("привет")[0], None)

    def test_apply_writes_tracker_and_confirms(self):
        import tempfile
        tmp = tempfile.mkdtemp()
        old_p = jw.P
        jw.P = lambda name: os.path.join(tmp, name)
        try:
            st = {"jobs": {"105": {"company": "Supabase", "title": "Support Engineer", "url": "u",
                                   "date": str(date.today())}}}
            msg = jw.apply_command("applied", ["105", "999"], st)
            self.assertIn("Supabase", msg)
            self.assertIn("#999", msg)
            rows = jw.read_tracker()
            self.assertEqual(rows[0]["status"], "applied")
            self.assertEqual(rows[0]["company"], "Supabase")
        finally:
            jw.P = old_p


class Followups(unittest.TestCase):
    def rows(self, *items):
        return [{"company": c, "title": "Role", "status": st,
                 "date": date.today() - timedelta(days=d)} for c, st, d in items]

    def test_due_after_a_week(self):
        st = {"reminded": []}
        due = jw.followups_due(self.rows(("Acme", "applied", 8)), st, CFG)
        self.assertEqual(len(due), 1)
        self.assertEqual(jw.followups_due(self.rows(("Acme", "applied", 8)), st, CFG), [])  # один раз

    def test_not_due_if_replied(self):
        rows = self.rows(("Acme", "applied", 9), ("Acme", "reply", 2))
        self.assertEqual(jw.followups_due(rows, {"reminded": []}, CFG), [])

    def test_not_due_too_early_or_late(self):
        rows = self.rows(("A", "applied", 3), ("B", "applied", 30))
        self.assertEqual(jw.followups_due(rows, {"reminded": []}, CFG), [])


class Dedupe(unittest.TestCase):
    def test_prefers_company_site(self):
        agg = job("Support Engineer", company="Supabase")
        agg["url"] = "https://jobicy.com/x"
        direct = job("Support Engineer", company="Supabase", direct=True)
        direct["url"] = "https://jobs.ashbyhq.com/supabase/x"
        u = jw.dedupe([agg, direct])
        self.assertEqual(len(u), 1)
        self.assertIn("ashbyhq", list(u.values())[0]["url"])


if __name__ == "__main__":
    unittest.main()
