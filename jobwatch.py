#!/usr/bin/env python3
"""
jobwatch v3 — ежедневный отбор удалённых вакансий под конкретный профиль.

    python3 jobwatch.py                 обычный запуск
    python3 jobwatch.py --no-telegram   без отправки в Telegram
    python3 jobwatch.py --test-telegram проверить, что бот пишет
    python3 jobwatch.py --weekly        принудительно прислать недельную сводку

Файлы:
    config.toml   настройки (правите вы)
    tracker.csv   ваши отклики и статусы (правите вы, бот только читает)
    found.csv     журнал всего найденного (пишет бот)
    report.html   отчёт по новым вакансиям
    letters/      черновики писем
    seen.json     память бота
    health.json   состояние источников
    state.json    номера вакансий, команды Telegram, напоминания
    tracker_tg.csv  отклики, отмеченные через Telegram (пишет бот)
"""

import argparse
import csv
import html
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import lru_cache

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("Нужен Python 3.11+ или пакет tomli: pip install tomli")

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    sys.exit("Нужен requests: pip install requests")

HERE = os.path.dirname(os.path.abspath(__file__))
P = lambda name: os.path.join(HERE, name)

TIMEOUT = 25
UA = {"User-Agent": "Mozilla/5.0 (jobwatch personal job search script)"}
def _clean_secret(value):
    """Убирает типичный мусор при копировании: пробелы, переносы, кавычки, префикс bot."""
    v = (value or "").strip().strip("'\"").strip()
    if v.lower().startswith("bot") and ":" in v:
        v = v[3:]
    return v


TG_TOKEN = _clean_secret(os.getenv("TG_TOKEN"))
TG_CHAT_ID = _clean_secret(os.getenv("TG_CHAT_ID"))
TODAY = date.today()


def load_config():
    with open(P("config.toml"), "rb") as f:
        return tomllib.load(f)


def make_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(UA)
    return s


# ================================================================ ИСТОЧНИКИ

def _job(source, title, company, location, url, text, posted=None,
         sal_min=None, sal_max=None, currency=None, sal_text=""):
    return {
        "source": source, "title": (title or "").strip(),
        "company": (company or "").strip(), "location": (location or "").strip(),
        "url": url or "", "text": (text or "")[:8000], "posted": posted,
        "sal_min": sal_min, "sal_max": sal_max, "currency": currency,
        "sal_text": sal_text or "",
    }


def parse_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value).date()
        except (OSError, ValueError):
            return None
    s = str(value).strip()
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s).date()
    except Exception:
        return None


def fetch_remotive(s):
    urls = [
        "https://remotive.com/api/remote-jobs",
        "https://remotive.com/api/remote-jobs?category=customer-support",
        "https://remotive.com/api/remote-jobs?category=devops",
    ]
    out = []
    for i, url in enumerate(urls):
        try:
            r = s.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception:
            if i == 0:
                raise  # основная лента обязана работать; категории — по возможности
            continue
        out += [_job("Remotive", j.get("title"), j.get("company_name"),
                     j.get("candidate_required_location"), j.get("url"),
                     f"{j.get('title','')} {j.get('description','')}",
                     parse_date(j.get("publication_date")), sal_text=j.get("salary", ""))
                for j in r.json().get("jobs", [])]
    return out


def fetch_remoteok(s):
    r = s.get("https://remoteok.com/api", timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for j in r.json():
        if isinstance(j, dict) and "position" in j:
            out.append(_job("RemoteOK", j.get("position"), j.get("company"),
                            j.get("location") or "Worldwide", j.get("url"),
                            f"{j.get('position','')} {' '.join(j.get('tags', []))} {j.get('description','')}",
                            parse_date(j.get("date")),
                            j.get("salary_min") or None, j.get("salary_max") or None, "USD"))
    return out


def fetch_himalayas(s, pages=4):
    out = []
    for offset in range(0, pages * 20, 20):
        r = s.get(f"https://himalayas.app/jobs/api?limit=20&offset={offset}", timeout=TIMEOUT)
        r.raise_for_status()
        for j in r.json().get("jobs", []):
            locs = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
            out.append(_job("Himalayas", j.get("title"), j.get("companyName"), locs,
                            j.get("applicationLink") or j.get("guid"),
                            f"{j.get('title','')} {j.get('excerpt','')} {j.get('description','')}",
                            parse_date(j.get("pubDate")),
                            j.get("minSalary"), j.get("maxSalary"), j.get("currency")))
        time.sleep(0.7)
    return out


def fetch_jobicy(s):
    urls = ["https://jobicy.com/api/v2/remote-jobs?count=50",
            "https://jobicy.com/api/v2/remote-jobs?count=50&geo=apac"]
    out = []
    for i, url in enumerate(urls):
        try:
            r = s.get(url, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception:
            if i == 0:
                raise
            continue
        out += [_job("Jobicy", j.get("jobTitle"), j.get("companyName"), j.get("jobGeo"),
                     j.get("url"),
                     f"{j.get('jobTitle','')} {j.get('jobExcerpt','')} {j.get('jobDescription','')}",
                     parse_date(j.get("pubDate")),
                     j.get("annualSalaryMin"), j.get("annualSalaryMax"), j.get("salaryCurrency"))
                for j in r.json().get("jobs", [])]
    return out


def fetch_arbeitnow(s):
    r = s.get("https://www.arbeitnow.com/api/job-board-api", timeout=TIMEOUT)
    r.raise_for_status()
    return [_job("Arbeitnow", j.get("title"), j.get("company_name"), j.get("location"),
                 j.get("url"), f"{j.get('title','')} {j.get('description','')}",
                 parse_date(j.get("created_at")))
            for j in r.json().get("data", []) if j.get("remote")]


def fetch_wwr(s):
    feeds = [
        "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
    ]
    out = []
    for feed in feeds:
        r = s.get(feed, timeout=TIMEOUT)
        r.raise_for_status()
        for item in ET.fromstring(r.content).iter("item"):
            raw = item.findtext("title", "")
            company, sep, title = raw.partition(":")
            if not sep:
                company, title = "", raw
            out.append(_job("WeWorkRemotely", title, company,
                            item.findtext("region", "") or "Remote", item.findtext("link", ""),
                            f"{raw} {item.findtext('description','')}",
                            parse_date(item.findtext("pubDate"))))
        time.sleep(0.7)
    return out


def fetch_workingnomads(s):
    r = s.get("https://www.workingnomads.com/api/exposed_jobs/", timeout=TIMEOUT)
    r.raise_for_status()
    return [_job("WorkingNomads", j.get("title"), j.get("company_name"), j.get("location"),
                 j.get("url"), f"{j.get('title','')} {j.get('tags','')} {j.get('description','')}",
                 parse_date(j.get("pub_date")))
            for j in r.json()]


def fetch_hh(s, cfg):
    """hh.ru: удалённые вакансии по заголовку. Зарплата — рубли в месяц.
    Если задан секрет HH_TOKEN (токен приложения с dev.hh.ru), запросы идут с авторизацией."""
    h = cfg.get("hh", {})
    email = h.get("contact_email", "")
    s.headers.update({"HH-User-Agent": f"jobwatch/1.0 ({email})",
                      "User-Agent": f"jobwatch/1.0 ({email})"})
    token = _clean_secret(os.getenv("HH_TOKEN"))
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    out, errors, first_error = [], 0, ""
    for q in h.get("queries", []):
        base = {"text": q, "search_field": "name", "per_page": 100,
                "period": h.get("period_days", 14)}
        try:
            r = s.get("https://api.hh.ru/vacancies", params={**base, "schedule": "remote"},
                      timeout=TIMEOUT)
            if r.status_code == 400:  # на случай смены справочника на стороне hh
                r = s.get("https://api.hh.ru/vacancies",
                          params={**base, "work_format": "REMOTE"}, timeout=TIMEOUT)
            if r.status_code >= 400:
                body = re.sub(r"\s+", " ", r.text)[:160]
                raise RuntimeError(f"HTTP {r.status_code}: {body}")
        except Exception as e:
            errors += 1
            first_error = first_error or str(e)
            continue
        for j in r.json().get("items", []):
            sal = j.get("salary") or {}
            snip = j.get("snippet") or {}
            job = _job("hh.ru", j.get("name"), (j.get("employer") or {}).get("name"),
                       "Удалённо (hh.ru)", j.get("alternate_url"),
                       f"{j.get('name','')} {snip.get('requirement') or ''} "
                       f"{snip.get('responsibility') or ''}",
                       parse_date(j.get("published_at")),
                       sal.get("from"), sal.get("to"), sal.get("currency"))
            job["period"] = "month"
            job["skip_location"] = True  # удалёнка задана фильтром запроса
            out.append(job)
        time.sleep(0.5)
    if errors and not out:
        raise RuntimeError(f"все {errors} запросов с ошибкой — {first_error}"
                           f"{'' if token else ' (HH_TOKEN не задан)'}")
    return out


AGGREGATORS = [fetch_remotive, fetch_remoteok, fetch_himalayas, fetch_jobicy,
               fetch_arbeitnow, fetch_wwr, fetch_workingnomads]


# ---------------------------------------------------------------- карьерные страницы

def _direct(job, remote_flag=False):
    job["direct"] = True
    job["remote_flag"] = remote_flag
    return job


def board_greenhouse(s, b):
    r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{b['slug']}/jobs?content=true",
              timeout=TIMEOUT)
    r.raise_for_status()
    name = b.get("name", b["slug"])
    return [_direct(_job(f"Career:{name}", j.get("title"), name,
                         (j.get("location") or {}).get("name", ""), j.get("absolute_url"),
                         f"{j.get('title','')} {j.get('content','')}",
                         parse_date(j.get("updated_at"))))
            for j in r.json().get("jobs", [])]


def board_lever(s, b):
    r = s.get(f"https://api.lever.co/v0/postings/{b['slug']}?mode=json", timeout=TIMEOUT)
    r.raise_for_status()
    name = b.get("name", b["slug"])
    out = []
    for j in r.json():
        cat = j.get("categories") or {}
        created = j.get("createdAt")
        out.append(_direct(_job(f"Career:{name}", j.get("text"), name, cat.get("location", ""),
                                j.get("hostedUrl"),
                                f"{j.get('text','')} {j.get('descriptionPlain','')} "
                                f"{j.get('additionalPlain','')}",
                                parse_date(created / 1000) if isinstance(created, (int, float)) else None),
                           remote_flag=(j.get("workplaceType") == "remote")))
    return out


def board_ashby(s, b):
    r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{b['slug']}?includeCompensation=true",
              timeout=TIMEOUT)
    r.raise_for_status()
    name = b.get("name", b["slug"])
    out = []
    for j in r.json().get("jobs", []):
        comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
        out.append(_direct(_job(f"Career:{name}", j.get("title"), name, j.get("location", ""),
                                j.get("jobUrl"),
                                f"{j.get('title','')} {j.get('descriptionPlain','')}",
                                parse_date(j.get("publishedAt")), sal_text=comp or ""),
                           remote_flag=bool(j.get("isRemote"))))
    return out


def board_recruitee(s, b):
    r = s.get(f"https://{b['slug']}.recruitee.com/api/offers/", timeout=TIMEOUT)
    r.raise_for_status()
    name = b.get("name", b["slug"])
    out = []
    for j in r.json().get("offers", []):
        loc = j.get("location") or ", ".join(x for x in (j.get("city"), j.get("country")) if x)
        out.append(_direct(_job(f"Career:{name}", j.get("title"), name, loc,
                                j.get("careers_url") or j.get("careers_apply_url"),
                                f"{j.get('title','')} {j.get('description','')} {j.get('requirements','')}",
                                parse_date(j.get("published_at") or j.get("created_at"))),
                           remote_flag=bool(j.get("remote"))))
    return out


def board_personio(s, b):
    r = s.get(f"https://{b['slug']}.jobs.personio.de/xml", timeout=TIMEOUT)
    r.raise_for_status()
    name = b.get("name", b["slug"])
    out = []
    for pos in ET.fromstring(r.content).iter("position"):
        office = pos.findtext("office", "") or ""
        desc = " ".join(d.findtext("value", "") or "" for d in pos.iter("jobDescription"))
        remote = "remote" in office.lower() or "remote" in (pos.findtext("schedule", "") or "").lower()
        out.append(_direct(_job(f"Career:{name}", pos.findtext("name", ""), name, office,
                                f"https://{b['slug']}.jobs.personio.de/job/{pos.findtext('id', '')}",
                                f"{pos.findtext('name','')} {desc}",
                                parse_date(pos.findtext("createdAt"))),
                           remote_flag=remote))
    return out


def board_smartrecruiters(s, b):
    r = s.get(f"https://api.smartrecruiters.com/v1/companies/{b['slug']}/postings?limit=100",
              timeout=TIMEOUT)
    r.raise_for_status()
    name = b.get("name", b["slug"])
    out = []
    for j in r.json().get("content", []):
        loc = j.get("location") or {}
        where = ", ".join(x for x in (loc.get("city"), loc.get("country")) if x)
        extra = " ".join((j.get(k) or {}).get("label", "") for k in ("department", "function"))
        out.append(_direct(_job(f"Career:{name}", j.get("name"), name, where,
                                f"https://jobs.smartrecruiters.com/{b['slug']}/{j.get('id')}",
                                f"{j.get('name','')} {extra}", parse_date(j.get("releasedDate"))),
                           remote_flag=bool(loc.get("remote"))))
    return out


BOARD_FETCHERS = {"greenhouse": board_greenhouse, "lever": board_lever, "ashby": board_ashby,
                  "recruitee": board_recruitee, "personio": board_personio,
                  "smartrecruiters": board_smartrecruiters}


def build_tasks(cfg):
    pages = cfg.get("sources", {}).get("himalayas_pages", 4)
    tasks = []
    for f in AGGREGATORS:
        name = f.__name__.replace("fetch_", "")
        if f is fetch_himalayas:
            tasks.append((name, lambda s, p=pages: fetch_himalayas(s, p)))
        else:
            tasks.append((name, f))
    if cfg.get("hh", {}).get("enabled"):
        tasks.append(("hh", lambda s: fetch_hh(s, cfg)))
    for b in cfg.get("companies_direct", {}).get("boards", []):
        fn = BOARD_FETCHERS.get(str(b.get("ats", "")).lower())
        if fn and b.get("slug"):
            tasks.append((f"board:{b.get('name', b['slug'])}", lambda s, b=b, fn=fn: fn(s, b)))
        else:
            print(f"  пропущена запись в boards: {b}")
    return tasks


def collect(cfg):
    jobs, stats = [], {}
    tasks = build_tasks(cfg)
    with ThreadPoolExecutor(max_workers=min(10, len(tasks))) as pool:
        futures = {pool.submit(fn, make_session()): name for name, fn in tasks}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                got = fut.result()
                jobs.extend(got)
                stats[name] = len(got)
            except Exception as e:
                stats[name] = f"ошибка: {str(e)[:220] or type(e).__name__}"
    return jobs, stats


# ---------------------------------------------------------------- здоровье источников

def update_health(stats):
    """Считает подряд идущие сбои. Возвращает список предупреждений."""
    path = P("health.json")
    data = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    alerts = []
    for name, v in stats.items():
        h = data.setdefault(name, {"fails": 0, "last_ok": None})
        # у агрегаторов ноль вакансий — подозрительно; у карьерной страницы — нормально
        failed = isinstance(v, str) or (v == 0 and not name.startswith("board:"))
        if failed:
            h["fails"] += 1
            if h["fails"] == 3:
                alerts.append(f"{name}: не работает 3 запуска подряд ({v})")
        else:
            h["fails"], h["last_ok"] = 0, str(TODAY)
    data = {k: v for k, v in data.items() if k in stats}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    if not any(isinstance(v, int) and v > 0 for v in stats.values()):
        alerts.insert(0, "Ни один источник не вернул вакансий — проверьте лог запуска.")
    return alerts


# ================================================================ ЗАРПЛАТА

RATES = {"USD": 1.0, "EUR": 1.08, "GBP": 1.27, "CAD": 0.73, "AUD": 0.66,
         "CHF": 1.12, "PLN": 0.25, "SEK": 0.095, "INR": 0.012,
         "RUB": 1 / 84, "RUR": 1 / 84}
SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


def _annualize(v):
    if v < 500:
        return v * 2000      # похоже на почасовую
    if v < 15000:
        return v * 12        # похоже на месячную
    return v


def salary_usd(job):
    """Возвращает (min, max) в USD в год или (None, None)."""
    cur = (job.get("currency") or "USD").upper()
    lo, hi = job.get("sal_min"), job.get("sal_max")

    if not (lo or hi) and job.get("sal_text"):
        t = job["sal_text"]
        cur = next((c for sym, c in SYMBOLS.items() if sym in t), cur)
        m = re.findall(r"(\d[\d,.]*)\s*([kK])?", t)
        nums = []
        for n, k in m:
            try:
                v = float(n.replace(",", ""))
            except ValueError:
                continue
            if k:
                v *= 1000
            if v >= 10:
                nums.append(v)
        if nums:
            lo, hi = min(nums), max(nums)

    try:
        lo = float(lo) if lo else None
        hi = float(hi) if hi else None
    except (TypeError, ValueError):
        return None, None
    if not (lo or hi):
        return None, None

    rate = RATES.get(cur, 1.0)
    if job.get("period") == "month":
        lo = lo * 12 * rate if lo else None
        hi = hi * 12 * rate if hi else None
    else:
        lo = _annualize(lo) * rate if lo else None
        hi = _annualize(hi) * rate if hi else None
    return lo or hi, hi or lo


def fmt_rub(lo, hi):
    lo, hi = lo or hi, hi or lo
    if not lo:
        return ""
    return f"{lo/1000:.0f}k ₽/мес" if lo == hi else f"{lo/1000:.0f}–{hi/1000:.0f}k ₽/мес"


def fmt_salary(lo, hi):
    if not lo:
        return ""
    if abs(hi - lo) < 1000:
        return f"${lo/1000:.0f}k/год"
    return f"${lo/1000:.0f}–{hi/1000:.0f}k/год"


# ================================================================ СКОРИНГ

def clean(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)  # на случай двойного экранирования
    return re.sub(r"\s+", " ", s).lower()


@lru_cache(maxsize=4096)
def _rx(term):
    return re.compile(r"(?<![a-z0-9])" + re.escape(term.strip().lower())
                      + r"(?:s|es|ing|ed)?(?![a-z0-9])")


def has(term, text):
    """Совпадение по границам слов с окончаниями s/es/ing/ed.
    'intern' не ловит 'international', 'iam' — 'william', но 'integration' ловит 'integrations'."""
    return bool(term.strip()) and _rx(term).search(text) is not None


def hits(terms, text):
    return [t for t in terms if has(t, text)]


def anyhit(terms, text):
    return any(has(t, text) for t in terms)


def norm_company(name):
    n = clean(name)
    n = re.sub(r"\b(inc|llc|ltd|gmbh|corp|co|s\.?a|b\.?v|oy|ab|limited)\b\.?", "", n)
    return re.sub(r"[^a-z0-9а-я]+", " ", n).strip()


def dedupe(jobs):
    """Одна вакансия с нескольких площадок: оставляем ссылку на сайт компании."""
    unique = {}
    for j in jobs:
        jid = job_id(j)
        cur = unique.get(jid)
        if cur is None or (j.get("direct") and not cur.get("direct")):
            unique[jid] = j
    return unique


def job_id(job):
    return f"{norm_company(job['company'])}|{clean(job['title'])}".strip()


LOCATION_NOISE = {
    "remote", "only", "hybrid", "fully", "full", "time", "work", "from", "home", "wfh",
    "flexible", "location", "locations", "any", "more", "or", "and", "in", "the", "based",
    "timezone", "timezones", "zone", "zones", "hours", "job", "jobs", "position", "role",
    "within", "of", "first", "distributed", "team", "friendly", "office", "optional",
    "remoto", "remota", "teletrabajo", "telework", "telecommute", "homeoffice",
}


def location_verdict(loc, cfg):
    """ok — доступно отовсюду/из Азии; soft — Европа/EMEA; restricted — конкретная страна;
    neutral — не указано или просто «Remote»."""
    L = cfg["location"]
    if not loc:
        return "neutral"
    if anyhit(L["accepted"], loc):
        return "ok"
    if anyhit(L["soft"], loc):
        return "soft"
    words = [w for w in re.sub(r"[^a-zа-я\s]", " ", loc).split() if w not in LOCATION_NOISE]
    return "restricted" if words else "neutral"


_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
_EXP_RX = re.compile(
    r"\b(\d{1,2}|" + "|".join(_WORDNUM) + r")\s*(?:\+|plus)?\s*(?:or more\s+)?"
    r"years?\s+(?:of\s+)?[^.;]{0,60}?experience")


def required_years(text):
    vals = []
    for m in _EXP_RX.finditer(text):
        v = m.group(1)
        n = int(v) if v.isdigit() else _WORDNUM[v]
        if n <= 20:
            vals.append(n)
    return max(vals) if vals else None


def score(job, cfg, cooldown):
    title, text = clean(job["title"]), clean(job["text"])
    loc = clean(job["location"]).strip(" ,.")
    head = text[:3000]
    why = []

    comp = norm_company(job["company"])
    if comp and comp in cooldown:
        return None, ["уже откликались недавно"]
    if comp in [c.lower() for c in cfg["companies"]["blocklist"]]:
        return None, ["компания в чёрном списке"]
    if anyhit(cfg["search"]["title_stop"], title):
        return None, ["стоп-слово в заголовке"]

    L = cfg["location"]
    if (anyhit(L["blocked_phrases"], title) or anyhit(L["blocked_phrases"], loc)
            or anyhit(L["blocked_phrases"], head)):
        return None, ["недоступна по локации"]

    verdict = "neutral" if job.get("skip_location") else location_verdict(loc, cfg)
    relocation = anyhit(L.get("relocation_phrases", []), text)
    if verdict == "restricted" and not relocation:
        return None, [f"только: {job['location'][:30]}"]

    direct = job.get("direct")
    if direct and cfg.get("companies_direct", {}).get("remote_only", True):
        remote = (job.get("remote_flag") or has("remote", loc) or has("remote", head[:1500])
                  or verdict == "ok")
        if not remote and not relocation:
            return None, ["не удалёнка (карьерная страница)"]

    ind = cfg["industry"]
    if anyhit(ind["drop"], text):
        return None, ["индустрия исключена"]

    posted = job.get("posted")
    if posted and (TODAY - posted).days > cfg["search"]["max_age_days"]:
        return None, ["старая вакансия"]

    lo, hi = salary_usd(job)
    is_hh = job.get("source") == "hh.ru"
    if is_hh and str(job.get("currency", "")).upper() in ("RUB", "RUR"):
        job["salary"] = fmt_rub(job.get("sal_min"), job.get("sal_max"))
    else:
        job["salary"] = fmt_salary(lo, hi)
    if is_hh:
        hcfg = cfg.get("hh", {})
        floor = hcfg.get("min_monthly_rub", 110000) * 12 / hcfg.get("rub_per_usd", 84)
    else:
        floor = cfg["salary"]["min_annual_usd"]
    if hi and hi < floor:
        return None, ["зарплата ниже порога"]

    S = cfg["search"]
    t_hits = hits(S["role_keywords"], title)
    x_hits = [k for k in hits(S["role_keywords"], text) if k not in t_hits]
    if not t_hits:
        if not x_hits:
            return None, ["нет ключевых слов роли"]
        if S.get("require_title_match", True):
            return None, ["роль не в заголовке"]

    s = 0
    if t_hits:
        s += 5
        why.append("роль: " + ", ".join(t_hits[:2]))
    if x_hits:
        s += 2

    skills = hits(S["skills"], text)
    s += min(len(skills), 5)
    if skills:
        why.append("навыки: " + ", ".join(skills[:4]))

    if direct:
        s += 2
        why.append("★ целевая компания")
    if is_hh:
        s += cfg.get("hh", {}).get("bonus", 2)
        why.append("русскоязычная (hh)")

    if verdict == "ok" or anyhit(L.get("good_text", []), head):
        s += 3
        why.append("глобальный найм")
    elif verdict == "soft":
        s -= L.get("soft_penalty", 3)
        why.append("⚠ только Европа/EMEA — проверь найм из Азии")
    elif verdict == "restricted":
        s -= 1
        why.append(f"⚠ {job['location'][:25]}, но есть релокация/виза")

    tz = cfg["timezone"]
    if anyhit(tz["penalty"], text):
        s -= 3
        why.append("⚠ американские часы")
    elif anyhit(tz["bonus"], text):
        s += 2
        why.append("удобный часовой пояс")

    if lo and not is_hh and lo >= cfg["salary"]["good_annual_usd"]:
        s += 3
        why.append("хорошая вилка")

    langs = cfg["languages"]
    job["lang"] = None
    if anyhit(langs["russian"], text):
        s += 4
        job["lang"] = "ru"
        why.append("★ нужен русский")
    elif anyhit(langs["chinese"], text):
        s += 2
        job["lang"] = "zh"
        why.append("китайский (проверь уровень)")

    if re.search(r"\b(russia|belarus)\b", text):
        s -= 1
        why.append("⚠ упоминается РФ — проверь ограничения")

    if anyhit(ind["penalty"], text):
        s -= ind["penalty_points"]
        why.append("крипто/трейдинг")

    exp = cfg.get("experience", {})
    yrs = required_years(text)
    if yrs and yrs > exp.get("max_years", 6):
        s -= exp.get("penalty", 3)
        why.append(f"требуют {yrs}+ лет")

    if anyhit(cfg["seniority"]["penalty"], title):
        s -= 2
        why.append("высокий грейд")

    return s, why


# ================================================================ ТРЕКЕР

STATUS_MAP = {
    "applied": "applied", "отправлено": "applied", "отклик": "applied",
    "reply": "reply", "ответ": "reply", "screening": "reply",
    "interview": "interview", "интервью": "interview",
    "offer": "offer", "оффер": "offer",
    "rejected": "rejected", "отказ": "rejected",
}


TRACKER_FILES = ("tracker.csv", "tracker_tg.csv")  # второй пишет бот по командам из Telegram


def read_tracker():
    rows = []
    for name in TRACKER_FILES:
        path = P(name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                status = STATUS_MAP.get(clean(r.get("status", "")).strip())
                rows.append({"date": parse_date(r.get("date")), "company": r.get("company", ""),
                             "title": r.get("title", ""), "status": status})
    return rows


def append_tracker_tg(company, title, status, note=""):
    path = P("tracker_tg.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "company", "title", "status", "notes"])
        w.writerow([TODAY, company, title, status, note])


def cooldown_set(rows, days):
    border = TODAY - timedelta(days=days)
    return {norm_company(r["company"]) for r in rows
            if r["status"] and (r["date"] is None or r["date"] >= border)}


def weekly_summary(rows):
    week = TODAY - timedelta(days=7)
    recent = [r for r in rows if r["date"] and r["date"] >= week]
    count = lambda rs, st: sum(1 for r in rs if r["status"] == st)
    total_applied = sum(1 for r in rows if r["status"])
    total_replies = sum(1 for r in rows if r["status"] in ("reply", "interview", "offer"))

    lines = [
        "jobwatch · итоги недели", "",
        f"Отправлено за неделю: {sum(1 for r in recent if r['status'])}",
        f"Ответов: {count(recent, 'reply')}  ·  интервью: {count(recent, 'interview')}"
        f"  ·  офферов: {count(recent, 'offer')}", "",
        f"Всего в трекере: {total_applied} откликов, {total_replies} с ответом",
    ]
    if total_applied >= 40 and total_replies == 0:
        lines += ["", "40+ откликов без ответа — стоит пересмотреть резюме или таргетинг."]
    elif total_applied < 15:
        lines += ["", "Пока мало откликов для выводов, держите темп."]
    return "\n".join(lines)


# ================================================================ ПИСЬМА

HOOKS = [
    (("integration", "implementation", "api", "onboarding"),
     "The integration focus of this role maps directly onto that work: scoping client "
     "requirements, running connections through testing and production, and "
     "troubleshooting when a data feed breaks."),
    (("cloud", "infrastructure", "devops", "linux", "platform"),
     "I've also built IT infrastructure for a 15-20 person office from scratch and worked "
     "hands-on with Linux, Windows Server, VPN and DNS, so infrastructure-side "
     "troubleshooting is familiar ground."),
    (("identity", "iam", "access", "sso", "security"),
     "I've administered the full access lifecycle, including onboarding, offboarding "
     "and permissions, which lines up with the identity side of this role."),
]
DEFAULT_HOOK = ("I'm used to owning tickets from first investigation to resolution or a "
                "well-documented escalation, and to turning recurring issues into "
                "knowledge base articles so they get solved once.")
LANG_LINE = {
    "ru": "I'm a native Russian speaker, so the Russian-language side of this role is covered.\n\n",
    "zh": "I also speak Chinese at B1 level and I'm actively working on improving it.\n\n",
}

LETTER = """Hi {company} team,

I'm applying for the {title} role. I have 3+ years of hands-on experience in IT support and B2B integrations. Most recently, at Properstar, I owned the client integration cycle end-to-end, delivering 10-20 integrations a year, diagnosing REST API failures with Postman and Swagger, and verifying data with SQL.

{hook}

{lang}I'm based in Vietnam (GMT+7), open to relocation, and can be hired through EOR platforms such as Deel or Remote.com. English C1, Russian native, Chinese B1.

I'd be glad to talk about how I could help your team.

Best,
Andrei Diachenko
kartruex@gmail.com · github.com/kartruex
"""


def tracker_line(job):
    buf = io.StringIO()
    csv.writer(buf).writerow([TODAY, job["company"], job["title"], "applied", ""])
    return buf.getvalue().strip()


def prune_letters(days):
    folder = P("letters")
    if not os.path.isdir(folder):
        return 0
    border = TODAY - timedelta(days=days)
    removed = 0
    for name in os.listdir(folder):
        d = parse_date(name[:10])
        if d and d < border:
            os.remove(os.path.join(folder, name))
            removed += 1
    return removed


def save_letter(job, cfg):
    os.makedirs(P("letters"), exist_ok=True)
    blob = clean(job["title"] + " " + job["text"][:2500])
    hook = next((h for keys, h in HOOKS if any(k in blob for k in keys)), DEFAULT_HOOK)
    body = LETTER.format(company=job["company"] or "hiring", title=job["title"],
                         hook=hook, lang=LANG_LINE.get(job.get("lang"), ""))
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{job['title']}".lower()).strip("-")[:70]
    path = P(os.path.join("letters", f"{TODAY:%Y-%m-%d}_{slug}.txt"))
    header = (f"URL:     {job['url']}\n"
              f"Резюме:  {cfg['letter']['resume_file']}\n"
              f"Балл:    {job['score']}  ({'; '.join(job['reasons'])})\n"
              f"Перед отправкой: добавьте после первого абзаца одно предложение "
              f"о том, чем вас зацепил их продукт.\n"
              f"Отметить в Telegram: +{job.get('num', '')}\n"
              f"Или в tracker.csv: {tracker_line(job)}\n"
              f"{'-' * 60}\n\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + body)
    return path


# ================================================================ ВЫВОД

def write_report(jobs, stats):
    rows = []
    for j in jobs:
        rows.append(
            f"<tr><td class='s'>{j['score']}<div class='r'>#{j.get('num','')}</div></td>"
            f"<td><a href='{html.escape(j['url'])}' target='_blank'>{html.escape(j['title'])}</a>"
            f"<div class='r'>{html.escape(' · '.join(j['reasons']))}</div></td>"
            f"<td>{html.escape(j['company'])}</td><td>{html.escape(j['location'][:40])}</td>"
            f"<td>{j.get('salary','')}</td>"
            f"<td>{j['posted'] or ''}</td><td>{j['source']}</td></tr>")
    src = " · ".join(f"{k}: {v}" for k, v in sorted(stats.items()))
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jobwatch · {TODAY}</title><style>
body{{font-family:system-ui,sans-serif;margin:24px;color:#1a1a1a}}
h1{{font-size:20px;margin-bottom:4px}} .m{{color:#777;font-size:12px;margin-bottom:16px}}
.w{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;min-width:760px}}
td,th{{border-bottom:1px solid #e5e5e5;padding:8px;text-align:left;vertical-align:top;font-size:14px}}
th{{background:#f5f5f5}} .s{{font-weight:700;text-align:center;width:40px}}
.r{{color:#777;font-size:12px;margin-top:3px}} a{{color:#1a56db;text-decoration:none}}
</style></head><body>
<h1>Новые вакансии: {len(jobs)} · {TODAY:%d.%m.%Y}</h1>
<div class="m">{html.escape(src)}</div>
<div class="w"><table><tr><th>Балл</th><th>Вакансия</th><th>Компания</th><th>Локация</th>
<th>Зарплата</th><th>Дата</th><th>Источник</th></tr>
{''.join(rows) or '<tr><td colspan=7>Сегодня новых нет</td></tr>'}
</table></div></body></html>"""
    with open(P("report.html"), "w", encoding="utf-8") as f:
        f.write(page)


def append_found(jobs):
    path = P("found.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["found", "score", "title", "company", "location", "salary", "source", "url"])
        for j in jobs:
            w.writerow([TODAY, j["score"], j["title"], j["company"], j["location"],
                        j.get("salary", ""), j["source"], j["url"]])


def check_telegram_config():
    """Возвращает текст проблемы или None, если формат секретов похож на правильный."""
    if not TG_TOKEN or not TG_CHAT_ID:
        return "не заданы TG_TOKEN и/или TG_CHAT_ID"
    if not re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", TG_TOKEN):
        if re.fullmatch(r"-?\d+", TG_TOKEN):
            return "TG_TOKEN похож на chat_id — возможно, секреты перепутаны местами"
        return ("TG_TOKEN неверного формата: должен выглядеть как 123456789:AAH... "
                f"(сейчас длина {len(TG_TOKEN)}, двоеточие {'есть' if ':' in TG_TOKEN else 'отсутствует'})")
    if not re.fullmatch(r"-?\d+", TG_CHAT_ID):
        return "TG_CHAT_ID должен быть числом"
    return None


def telegram(text):
    if not (TG_TOKEN and TG_CHAT_ID):
        return False
    problem = check_telegram_config()
    if problem:
        raise RuntimeError(problem)
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id": TG_CHAT_ID, "text": chunk,
                                "disable_web_page_preview": True}, timeout=TIMEOUT)
        if r.status_code == 404:
            raise RuntimeError("Telegram 404: токен неверный. Пересоздайте секрет TG_TOKEN "
                               "только с токеном от BotFather, без слова bot и пробелов.")
        if r.status_code == 401:
            raise RuntimeError("Telegram 401: токен отозван или скопирован не полностью.")
        if r.status_code == 400 and "chat not found" in r.text.lower():
            raise RuntimeError("Telegram: chat not found. Проверьте TG_CHAT_ID и что вы "
                               "написали боту хотя бы одно сообщение.")
        if r.status_code == 403:
            raise RuntimeError("Telegram 403: бот заблокирован или вы ему ещё не писали.")
        r.raise_for_status()
    return True


def telegram_digest(jobs, total, top):
    per_company, picked = {}, []
    for j in jobs:
        c = norm_company(j["company"])
        if per_company.get(c, 0) >= 2:
            continue
        per_company[c] = per_company.get(c, 0) + 1
        picked.append(j)
        if len(picked) >= top:
            break
    lines = [f"jobwatch · {TODAY:%d.%m} · новых {total}", ""]
    for j in picked:
        extra = f" · {j['salary']}" if j.get("salary") else ""
        tag = " ★RU" if j.get("lang") == "ru" else ""
        tag += " ◆" if j.get("direct") else ""
        num = f"#{j['num']} " if j.get("num") else ""
        lines.append(f"{num}[{j['score']}]{tag} {j['title']} — {j['company']}{extra}\n{j['url']}\n")
    if total > len(picked):
        lines.append(f"Ещё {total - len(picked)} в report.html")
    if picked and picked[0].get("num"):
        lines.append(f"Откликнулись? Ответьте «+{picked[0]['num']}». Все команды — /help")
    return "\n".join(lines)


# ================================================================ СОСТОЯНИЕ И КОМАНДЫ

def load_state():
    path = P("state.json")
    st = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    st.setdefault("tg_offset", 0)
    st.setdefault("next_num", 100)
    st.setdefault("jobs", {})
    st.setdefault("reminded", [])
    return st


def save_state(st):
    border = str(TODAY - timedelta(days=45))
    st["jobs"] = {k: v for k, v in st["jobs"].items() if v.get("date", "") >= border}
    st["reminded"] = st["reminded"][-300:]
    with open(P("state.json"), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def assign_numbers(jobs, st):
    """Короткие номера для ответов в Telegram: #100, #101…"""
    for j in jobs:
        n = st["next_num"]
        st["next_num"] += 1
        j["num"] = n
        st["jobs"][str(n)] = {"company": j["company"], "title": j["title"],
                              "url": j["url"], "date": str(TODAY)}


CMD_WORDS = {
    "ответ": "reply", "reply": "reply", "скрининг": "reply",
    "интервью": "interview", "инт": "interview", "interview": "interview",
    "оффер": "offer", "offer": "offer",
    "отказ": "rejected", "rej": "rejected", "rejected": "rejected",
}

HELP_TEXT = """Команды jobwatch:

+105          — откликнулся на вакансию #105
+105 107 110  — на несколько сразу
ответ 105     — рекрутер ответил
интервью 105  — позвали на интервью
оффер 105     — оффер
отказ 105     — отказ
/stats        — статистика откликов

Команды обрабатываются раз в несколько часов, подтверждение придёт после обработки."""


def parse_command(text):
    t = (text or "").strip().lower()
    if t.startswith("/stats") or t in ("stats", "статистика"):
        return "stats", []
    if t.startswith("/help") or t.startswith("/start") or t in ("help", "помощь"):
        return "help", []
    nums = re.findall(r"\d+", t)
    if t.startswith("+"):
        return "applied", nums
    m = re.match(r"/?([a-zа-яё]+)", t)
    if m and m.group(1) in CMD_WORDS:
        return CMD_WORDS[m.group(1)], nums
    return None, nums


def apply_command(kind, nums, st):
    """Записывает статусы в tracker_tg.csv, возвращает текст подтверждения."""
    if kind == "help":
        return HELP_TEXT
    if kind == "stats":
        return weekly_summary(read_tracker())
    if kind is None:
        return "Не понял команду. Напишите /help"
    if not nums:
        return "Укажите номер вакансии, например: +105"
    done, missing = [], []
    for n in nums:
        info = st["jobs"].get(n)
        if not info:
            missing.append(f"#{n}")
            continue
        append_tracker_tg(info["company"], info["title"], kind, f"telegram #{n}")
        done.append(f"#{n} {info['company']} — {info['title']}")
    label = {"applied": "отклик", "reply": "ответ", "interview": "интервью",
             "offer": "оффер", "rejected": "отказ"}[kind]
    lines = []
    if done:
        lines.append(f"Записал ({label}):\n" + "\n".join(done))
    if missing:
        lines.append("Не нашёл: " + ", ".join(missing) + " (номера живут 45 дней)")
    return "\n\n".join(lines)


def process_commands(st):
    """Забирает новые сообщения боту и выполняет команды. Возвращает ответы."""
    r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                     params={"offset": st["tg_offset"], "timeout": 0,
                             "allowed_updates": json.dumps(["message"])}, timeout=TIMEOUT)
    r.raise_for_status()
    replies = []
    for upd in r.json().get("result", []):
        st["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(TG_CHAT_ID):
            continue  # команды принимаются только из вашего чата
        text = msg.get("text", "")
        if not text:
            continue
        replies.append(apply_command(*parse_command(text), st))
    return replies


def followups_due(rows, st, cfg):
    fu = cfg.get("followup", {})
    after, until = fu.get("after_days", 7), fu.get("remind_until", 14)
    latest = {}
    for r in rows:
        if not (r["status"] and r["date"]):
            continue
        key = norm_company(r["company"])
        if key not in latest or r["date"] >= latest[key]["date"]:
            latest[key] = r
    due = []
    for key, r in latest.items():
        age = (TODAY - r["date"]).days
        if r["status"] == "applied" and after <= age <= until and key not in st["reminded"]:
            due.append(r)
            st["reminded"].append(key)
    return due


def followup_message(due):
    lines = ["Неделя без ответа — можно напомнить о себе:", ""]
    for r in due:
        lines.append(f"• {r['company']} — {r['title'] or 'отклик'} (с {r['date']:%d.%m})")
    lines += ["", "Шаблон:",
              "Hi, I applied for the [role] position on [date] and wanted to follow up. "
              "I'm still very interested and happy to share any additional details. "
              "Best regards, Andrei Diachenko"]
    return "\n".join(lines)


# ================================================================ MAIN

def load_seen():
    path = P("seen.json")
    if not os.path.exists(path):
        return {}
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, list):  # миграция с v2
        return {k: str(TODAY) for k in data}
    return data


def save_seen(seen):
    border = str(TODAY - timedelta(days=45))
    seen = {k: v for k, v in seen.items() if v >= border}
    with open(P("seen.json"), "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0, sort_keys=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-telegram", action="store_true")
    ap.add_argument("--test-telegram", action="store_true")
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--commands-only", action="store_true",
                    help="только обработать команды из Telegram, без сбора вакансий")
    args = ap.parse_args()

    if args.test_telegram:
        problem = check_telegram_config()
        if problem:
            print("Проблема с секретами:", problem)
            return
        try:
            telegram("jobwatch: связь с ботом работает ✅")
            print("Отправлено")
        except Exception as e:
            print("Ошибка:", e)
        return

    cfg = load_config()
    rate = 1 / cfg.get("hh", {}).get("rub_per_usd", 84)
    RATES["RUB"] = RATES["RUR"] = rate
    state = load_state()
    tg_on = not args.no_telegram and bool(TG_TOKEN and TG_CHAT_ID)

    # 1. команды, присланные боту с прошлого запуска
    if tg_on:
        try:
            replies = process_commands(state)
            for text in replies:
                telegram(text)
            if replies:
                print(f"Обработано команд из Telegram: {len(replies)}")
        except Exception as e:
            print("Команды Telegram:", e)
    if args.commands_only:
        save_state(state)
        return

    tracker = read_tracker()  # уже с учётом только что записанных команд
    cooldown = cooldown_set(tracker, cfg["companies"]["cooldown_days"])
    seen = load_seen()

    t0 = time.time()
    raw, stats = collect(cfg)
    alerts = update_health(stats)
    for name, n in sorted(stats.items()):
        print(f"  {name:<14} {n}")

    unique = dedupe(raw)

    passed, dropped, near_miss = [], {}, []
    for jid, j in unique.items():
        s, why = score(j, cfg, cooldown)
        if s is None:
            dropped[why[0]] = dropped.get(why[0], 0) + 1
            if why[0] == "роль не в заголовке":
                tech = len(hits(cfg["search"]["skills"], clean(j["text"])))
                near_miss.append((tech, j["title"]))
        elif s >= cfg["search"]["min_score"]:
            j.update(score=s, reasons=why, id=jid)
            passed.append(j)

    fresh = sorted((j for j in passed if j["id"] not in seen),
                   key=lambda x: x["score"], reverse=True)

    print(f"\nСобрано {len(unique)} за {time.time() - t0:.0f} с · прошло фильтр {len(passed)}"
          f" · новых {len(fresh)}")
    loc_drops = {k[8:].strip(): v for k, v in dropped.items() if k.startswith("только:")}
    for reason, n in sorted(dropped.items(), key=lambda x: -x[1]):
        if not reason.startswith("только:"):
            print(f"  отсеяно — {reason}: {n}")
    if loc_drops:
        top = sorted(loc_drops.items(), key=lambda x: -x[1])[:8]
        print(f"  отсеяно — только конкретная страна/город: {sum(loc_drops.values())}"
              f"  (чаще всего: {', '.join(f'{k} {v}' for k, v in top)})")
    if near_miss:
        print("\n  Доступны по локации, но роль не распознана по заголовку.")
        print("  Если среди них есть ваши — добавьте слово в role_keywords:")
        seen_titles, shown = set(), 0
        for tech, t in sorted(near_miss, key=lambda x: -x[0]):
            if t in seen_titles:
                continue
            seen_titles.add(t)
            print(f"    · [{tech}] {t}")
            shown += 1
            if shown >= 20:
                break
        print("    (в скобках — сколько ваших навыков упомянуто в описании)")
    print()
    for j in fresh[:15]:
        print(f"  [{j['score']:>2}] {j['title']} — {j['company']}  {j.get('salary','')}")

    assign_numbers(fresh, state)
    pruned = prune_letters(cfg.get("maintenance", {}).get("letters_keep_days", 30))
    letters = [save_letter(j, cfg) for j in fresh if j["score"] >= cfg["search"]["letter_score"]]
    write_report(fresh, stats)
    if fresh:
        append_found(fresh)

    delivered = True  # если Telegram не настроен или выключен — считаем, что «доставили» в отчёт
    if tg_on:
        try:
            if alerts:
                telegram("⚠ jobwatch\n\n" + "\n".join(alerts))
            if fresh:
                telegram(telegram_digest(fresh, len(fresh), cfg["search"]["telegram_top"]))
            if args.weekly or TODAY.weekday() == cfg["weekly"]["summary_weekday"]:
                telegram(weekly_summary(tracker))
            due = followups_due(tracker, state, cfg)
            if due:
                telegram(followup_message(due))
        except Exception as e:
            delivered = False
            print("Telegram:", e)

    fresh_ids = {j["id"] for j in fresh}
    for j in passed:
        # если отправка упала, новые вакансии не помечаем — придут в следующий запуск
        if not delivered and j["id"] in fresh_ids:
            continue
        seen.setdefault(j["id"], str(TODAY))
    if not delivered:
        print("Отправка не удалась: новые вакансии не отмечены как показанные.")
    save_seen(seen)
    save_state(state)
    for a in alerts:
        print("  ⚠", a)
    print(f"\nreport.html · писем: {len(letters)} · удалено старых: {pruned}")


if __name__ == "__main__":
    main()
