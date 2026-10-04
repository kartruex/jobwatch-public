# jobwatch

A small Python bot that does my daily job search for me. Every weekday morning it collects remote jobs from seven job boards and from the career pages of companies I follow, throws out everything that doesn't fit, scores the rest and sends the best matches to Telegram. I reply to the bot to log applications, and it reminds me when it's time to follow up.

I built it with an AI assistant. I designed the filtering rules, debugged the edge cases on real job postings and maintain it myself.

> This is a public copy for my portfolio. My working instance lives in a private repo, because it stores my application history. The schedule is disabled here.

## What it does

```
GitHub Actions (cron)
  → read commands sent to the bot since the last run ("+105", "interview 105", /stats)
  → fetch jobs from 7 boards + company career pages, in parallel, with retries
  → normalize every source into one format → deduplicate
  → filter and score → drop anything already sent (seen.json)
  → Telegram digest + HTML report + draft cover letters
  → commit state back to the repo
```

**Sources**
- Job boards: Remotive, RemoteOK, Himalayas, Jobicy, Arbeitnow, We Work Remotely, Working Nomads, plus hh.ru (optional).
- Company career pages via public ATS APIs: Greenhouse, Lever, Ashby, Recruitee, Personio, SmartRecruiters.

**Filtering and scoring** (rules live in `config.toml`, no code changes needed)
- Role keywords must appear in the title. Matching uses word boundaries with common suffixes, so "intern" doesn't match "international" and "integration" matches "integrations".
- Location works as a whitelist. A posting limited to a specific country is dropped unless it mentions relocation or visa support.
- Penalties for US-only hours, required experience above a threshold, and senior titles. Bonuses for global hiring, skills match and direct company postings.
- Salary floor, max posting age, company cooldown after I've applied, blocked industries.

**Telegram**
- Daily digest with short numbers for each job (#105) and at most two jobs per company.
- Commands: `+105` logs an application, `reply / interview / offer / rejected 105` update the status, `/stats` shows the funnel.
- Commands are accepted only from my own chat ID.
- If an application gets no answer within 7–14 days, the bot sends a follow-up reminder with a template.

## Design decisions I'd point out

- **At-least-once delivery.** If sending to Telegram fails, new jobs are *not* marked as seen and come again the next day. A duplicate is better than a lost job.
- **Source health monitoring.** Each source's consecutive failures are tracked in `health.json`, and the bot alerts me when one keeps failing.
- **Tests run before every collection.** 53 unit tests, including regression tests built from real postings that once slipped through the filters. A config change can't silently break the filters.
- **Secrets stay out of the code.** The Telegram token and chat ID come from GitHub Secrets. The bot validates their format and explains common mistakes, like swapping the token and the chat ID.
- **State in git.** GitHub Actions runners are ephemeral, so the bot commits its small state files back to the repo instead of using a database.

## Run it

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python jobwatch.py --no-telegram      # report.html only
```

For Telegram, set `TG_TOKEN` and `TG_CHAT_ID` as environment variables (or GitHub Secrets) and run `python jobwatch.py --test-telegram`.

## Files

| File | Purpose |
|---|---|
| `jobwatch.py` | everything: sources, filters, scoring, Telegram, state |
| `config.toml` | all search settings |
| `tests/test_jobwatch.py` | unit and regression tests |
| `.github/workflows/jobwatch.yml` | GitHub Actions workflow |
| `tracker.csv` | manual application log (the bot also writes `tracker_tg.csv`) |

Setup notes in Russian, from when I first deployed it: [README.ru.md](README.ru.md).

## Stack

Python 3.12 · requests · tomllib · unittest · GitHub Actions · Telegram Bot API · REST/XML APIs
