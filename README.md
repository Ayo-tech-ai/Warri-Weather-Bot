# Warri Laundry Weather Bot

A small, fully automated bot that sends a **laundry-focused weather message to Telegram twice a day**, with no one needing to ask for it.

- **7 AM:** what today looks like for line drying, pickups and deliveries
- **9 PM:** what tomorrow looks like, how it compares with today, and what to plan

The trigger comes from a scheduler, not from a person. The bot wakes up on its own, fetches the forecast, works out whether clothes will dry outdoors, asks an AI model to word it nicely, and sends it. If the AI is unavailable, it still sends a clean fallback message.

It runs for free on GitHub Actions: no server, no hosting bill.

---

## Table of contents

1. [How it works](#how-it-works)
2. [Why the design looks like this](#why-the-design-looks-like-this)
3. [Repository layout](#repository-layout)
4. [The drying logic](#the-drying-logic)
5. [The AI message layer](#the-ai-message-layer)
6. [Reliability and fallbacks](#reliability-and-fallbacks)
7. [Configuration](#configuration)
8. [Setup guide](#setup-guide)
9. [Deploying on GitHub Actions](#deploying-on-github-actions)
10. [Testing](#testing)
11. [Handling secrets safely](#handling-secrets-safely)
12. [Troubleshooting](#troubleshooting)
13. [Known limitations](#known-limitations)
14. [How this project was built](#how-this-project-was-built)
15. [Ideas for later](#ideas-for-later)
16. [Useful links](#useful-links)

---

## How it works

```
GitHub Actions scheduler (06:50 and 19:50 UTC)
        |
        v
  weather_bot.py <morning|evening>
        |
        |-- 1. Fetch forecast from Open-Meteo (today + tomorrow, hourly + daily)
        |-- 2. Compute facts in plain code (best drying window, rain periods, verdict)
        |-- 3. Ask an AI model to word those facts (Groq first, Gemini second)
        |-- 4. If both AIs fail, build a template message from the same facts
        |-- 5. Send the message to Telegram
        |
        v
  You, on Telegram
```

If something breaks before a message can be built, a short warning is sent to Telegram instead, so silence never means "maybe it's working."

**Two modes**

| Mode | Runs at (Warri time) | Content |
|---|---|---|
| `morning` | about 7 AM | Today only: conditions, drying verdict, best window, rain timing, tips |
| `evening` | about 9 PM | Tomorrow, compared with today: better or worse for drying, rain and heat, plus planning tips |

---

## Why the design looks like this

**It is a pipeline with one AI step, not a free-roaming agent.**
The sequence (fetch, compute, word, send) is fixed. The model only adds value in the last mile: friendly wording and practical advice. Deterministic code around a single model call is much easier to make reliable than a loop that decides its own steps.

**Facts are computed by code, not by the model.**
Weather codes alone are misleading. A day labelled "drizzle" can still have a strong dry afternoon with rain only in the evening. So the script analyses hourly data itself and hands the model finished facts. The model cannot invent a drying window, and the fallback template can show exactly the same facts.

**The scheduler is the trigger.**
Nothing here waits for a chat message. A cron schedule on GitHub Actions starts the script, the script does its work and exits. This is why no always-on server is needed.

**Plain API calls instead of an agent framework.**
One HTTP request to a model does the job. Fewer moving parts means fewer failure points.

---

## Repository layout

```
.
├── weather_bot.py              # the whole bot: fetch, compute, AI, fallback, send
├── requirements.txt            # Python dependencies (requests, pandas)
├── .gitignore                  # keeps local env files out of git
├── README.md                   # this file
└── .github/
    └── workflows/
        └── weather.yml         # schedule + manual trigger
```

---

## The drying logic

All of this lives in `weather_bot.py` and runs without any AI.

**Data requested from Open-Meteo** (location: Warri, timezone `Africa/Lagos`, 2 forecast days):

- *Daily:* weather code, max/min temperature, total rain, max rain probability, max UV index
- *Hourly:* temperature, relative humidity, rain probability, rain amount, wind speed, shortwave radiation (sunshine strength)

**Rules** (constants at the top of the script, so they are easy to tune):

| Setting | Default | Meaning |
|---|---|---|
| `DRY_START`, `DRY_END` | 8, 17 | Hours when outdoor line drying is realistic |
| `RAIN_START`, `RAIN_END` | 7, 19 | Hours that matter for pickups and deliveries |
| `MAX_RAIN_PROB` | 40 % | Above this, an hour counts as risky |
| `MAX_RAIN_MM` | 0.3 mm | Rain at or above this ruins an hour; smaller traces are ignored |
| `MAX_HUMIDITY` | 85 % | Above this, drying is very slow |

**How a day is judged**

1. An hour between 08:00 and 17:00 is *dry-friendly* if it passes all three limits above (rain probability, rain amount, humidity).
2. The **best window** is the longest unbroken run of dry-friendly hours.
3. The **verdict** comes from the window length:
   - `good`: 5 hours or more
   - `risky`: 3 to 4 hours
   - `poor`: fewer than 3 hours, or no window
4. **Rain periods** are hours between 07:00 and 19:00 with at least 60 % rain probability or at least 0.5 mm of rain, merged into ranges like "5PM to 8PM".

**Evening comparison** is also computed in code: drying verdict (better, worse, same), rain total (less, more, similar if within 2 mm) and daily high temperature (hotter or cooler if at least 1.5 °C different).

These thresholds are starting points. Compare the bot's verdicts with how your clothes actually dry, then adjust the constants.

---

## The AI message layer

The model never sees raw JSON from the weather service. It receives a short list of human-readable facts such as "best line-drying window: 9AM to 4PM" and "rain periods: 5PM to 8PM".

The prompt tells the model to:

- use only the facts provided, with no invented numbers or times
- write plain text with no markdown
- stay under about 120 words
- give 2 or 3 concrete tips (hanging times, indoor drying, bagging finished orders, delivery timing)
- not assume details that were not given (for example, how deliveries are made)

**Providers, in order:**

1. **Groq** (main). Fast, and responses usually come back in well under a second.
2. **Gemini** (second choice). Used only if Groq fails.
3. **Template** (last resort). No AI involved.

Some models "think out loud" before answering, wrapped in `<think>...</think>`. The script strips that block so reasoning text never reaches Telegram.

Model names and free-tier limits change over time. Before relying on a model name, list the models your key can use (both Groq and Gemini offer a list-models endpoint) and update `GROQ_MODEL` / `GEMINI_MODEL` in the script if needed.

---

## Reliability and fallbacks

Free AI tiers can be overloaded or rate-limited, so the bot is built to degrade gracefully.

| What fails | What happens |
|---|---|
| Groq errors or times out | Retried up to 3 times, then Gemini is tried |
| Gemini errors or times out | Retried up to 3 times, then the template message is used |
| Both AI providers fail | A plain, readable template message is sent (with a note that it is a basic summary) |
| Open-Meteo fails | Retried up to 3 times, then a short warning is sent to Telegram and the run is marked as failed |
| Anything unexpected | A warning is sent to Telegram and the run is marked as failed |

Details:

- **Retries** happen only for errors that retrying can fix: rate limits (429), server errors (500, 502, 503, 504), network problems and timeouts. Errors like a bad API key fail immediately instead of wasting time.
- **Backoff:** waits grow between attempts. For rate limits, the `retry-after` header is respected when the provider sends one.
- **Timeouts** are set on every request so a hung connection cannot block a run.
- **Failed runs are marked red** in GitHub on purpose, so you also get GitHub's own failure notification as a second signal.
- **Error messages are sanitized** so an exception can never print a URL containing a token.

---

## Configuration

**Environment variables the script reads**

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Lets the script send messages as your bot |
| `TELEGRAM_CHAT_ID` | yes | Which chat receives the messages |
| `GROQ_API_KEY` | yes (main model) | Main AI provider |
| `GEMINI_API_KEY` | recommended | Second AI provider (fallback) |
| `FORCE_FAIL` | no, testing only | Comma-separated: `groq`, `gemini`, `weather` to simulate outages |

Secrets are read only at the moment they are needed, so a missing optional key does not stop the rest of the bot from working.

**Settings inside `weather_bot.py`**

- Location: `WARRI_LAT`, `WARRI_LON`
- Models: `GROQ_MODEL`, `GEMINI_MODEL`
- Drying rules: the constants listed in [The drying logic](#the-drying-logic)

To adapt the bot to another city, change the coordinates (and the timezone string in `fetch_weather` if the city is outside West Africa).

---

## Setup guide

### 1. Create a Telegram bot

1. In Telegram, message **@BotFather** and send `/newbot`. Follow the prompts to name it.
2. BotFather gives you a **bot token**. Treat it like a password.
3. Open a chat with your new bot and send it any message (for example "hello").
4. Find your **chat ID** by calling the bot API `getUpdates` method once. The number under `chat.id` is your chat ID.

### 2. Get AI provider keys

- **Groq:** create an API key in the Groq console.
- **Gemini:** create an API key in Google AI Studio.

Both have free tiers. Limits and model names change, so check the current documentation.

### 3. Run it locally (optional)

```bash
pip install -r requirements.txt

# set the variables in your shell (macOS / Linux)
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export GROQ_API_KEY="..."
export GEMINI_API_KEY="..."

python weather_bot.py morning     # or: evening
```

On Windows PowerShell, use `$env:NAME = "value"` instead of `export`. Do not put real values in files that are committed.

### 4. Deploy

See the next section.

---

## Deploying on GitHub Actions

The workflow file `.github/workflows/weather.yml` has two triggers:

1. **Schedule** (cron, always in UTC)
2. **Manual run** (the "Run workflow" button) with a mode dropdown and an optional force-fail field for testing

**Schedule**

Warri is UTC+1 with no daylight saving, so Warri time minus one hour gives UTC.

| Message | Cron (UTC) | Warri time |
|---|---|---|
| Morning | `50 5 * * *` | 06:50 (arrives around 7 AM) |
| Evening | `50 19 * * *` | 20:50 (arrives around 9 PM) |

The times are set slightly early on purpose: GitHub often starts scheduled runs several minutes late, especially at the top of the hour.

**How the mode is chosen**

- For scheduled runs, the workflow checks the UTC hour: before 12:00 it runs `morning`, otherwise `evening`.
- For manual runs, it uses the mode you pick in the dropdown.

**Other workflow settings**

- Permissions are limited to reading repository contents.
- The job has a 10-minute timeout.
- Dependencies are cached to keep runs short (a typical run takes well under a minute).
- Consider pinning the runner image to a specific Ubuntu version instead of `ubuntu-latest`, so a platform migration cannot surprise you.

**Steps**

1. Create a repository and add the files from [Repository layout](#repository-layout), keeping the `.github/workflows/` path exactly.
2. Add the required secrets in the repository's Actions settings (see [Handling secrets safely](#handling-secrets-safely)).
3. Open the **Actions** tab, choose **Weather bot**, and click **Run workflow** to test.
4. Make sure the workflow file is on the **default branch**; scheduled workflows only run from there.

---

## Testing

Because the real trigger is an external clock, test everything you can by hand first.

**Manual runs**

Use the Actions tab, then Weather bot, then Run workflow. Run each case and check Telegram:

| Mode | Force-fail field | Expected result |
|---|---|---|
| `morning` | *(blank)* | Normal morning message from Groq |
| `evening` | *(blank)* | Normal evening message, with comparison to today |
| `evening` | `groq` | Message arrives; log shows it was sent via Gemini |
| `evening` | `groq,gemini` | Plain template message with the "basic summary" note |
| `morning` | `weather` | Warning message on Telegram; the run shows red (this is intended) |

**Reading the log**

Open the run, then the `send` job, then the "Send weather message" step. A successful run ends with a line like `morning message sent via groq`.

**Testing the scheduler itself**

The schedule is the one thing manual runs cannot prove. Options:

- Wait for the first scheduled slot and check the Actions tab for a run triggered by "schedule".
- Or temporarily change the cron to every 10 minutes, watch it fire a couple of times, then **restore the real times**.

Judge the first day by whether the message arrives, not by whether it arrives at exactly 7:00; delays of several minutes are normal.

**What good output looks like**

- Numbers and times in the message match the computed facts.
- The evening message really compares tomorrow with today.
- Tips are practical for a laundry business, and nothing is invented.

---

## Handling secrets safely

This repository can be public, so treat anything sensitive as something that must never appear in code, commits, issues, comments, screenshots or logs. General advice that applies to any project like this:

- **Use your platform's secret store.** Keep tokens and API keys in the secure secrets feature of wherever the code runs, and read them as environment variables. Never hard-code them, even "temporarily".
- **Keep local env files out of git.** If you ever use a local `.env` file, add it to `.gitignore` before the first commit. Check what is staged before every push.
- **Remember that git history is permanent.** Deleting a file after a leak does not remove it from history. If a secret is ever exposed, revoke it at the provider and create a new one; do not rely on deleting the commit.
- **Prefer secrets over plain variables.** Variables and configuration values are usually visible to anyone who can view the repo or logs.
- **Copy values carefully.** Stray spaces, quotes or trailing newlines are a common reason a correct key is rejected.
- **Use least privilege.** Give workflows read-only repository permissions unless they need more, and avoid giving write access to people you do not fully trust.
- **Secure your account.** Turn on two-factor authentication for the account that owns the repository, since account access means secret access.
- **Mind your logs.** On a public repository, logs are public. Never print keys or identifiers, and avoid dumping full error responses. Platforms usually mask registered secrets in logs, but treat that as a safety net, not a plan.
- **Rotate periodically** and remove keys you no longer use.
- **Watch for leak warnings.** Enable any secret-scanning or alerting your platform offers, and act on warnings immediately.
- **Treat identifiers as private too.** A chat ID or user ID is not a password, but there is no reason to publish it.

---

## Troubleshooting

| Symptom | Likely cause | What to do |
|---|---|---|
| Log says `Missing secret: NAME` | Secret not added, or the name has a typo | Names are case-sensitive and must match exactly |
| Telegram error 401 | Wrong or revoked bot token | Re-copy the token, or generate a new one with BotFather |
| Telegram error 400 about chat | Wrong chat ID, or you never messaged the bot | Message the bot first, then fetch the chat ID again |
| Groq 401 or 403 | Bad or revoked key | Create a new key and update the secret |
| Groq 404 or "model not found" | Model name changed or is unavailable to your key | List models for your key and update `GROQ_MODEL` |
| Groq 429 | Rate limit | Usually clears on retry; the bot also falls back to Gemini |
| Every message is the plain template | Both AI providers are failing | Check each provider's key and model name in the log |
| No run appears at the scheduled time | Workflow not on default branch, schedule not yet picked up, or runs delayed | Check the Actions tab; allow several minutes of delay |
| Scheduled runs stopped after weeks of quiet | Scheduled workflows can be paused after long repository inactivity | Make a small commit or re-enable the workflow (see limitations) |
| Verdicts feel wrong | Thresholds do not match real-world drying | Tune the drying constants |
| Message includes stray `*` or markdown | The model ignored the plain-text rule | Tighten the prompt rules or lower the temperature |

---

## Known limitations

- **Schedule precision.** GitHub scheduled runs can start several minutes late, and occasionally a run may be skipped under heavy load. This is fine for weather, but not for anything time-critical.
- **Inactivity pause.** As far as I know, GitHub may disable scheduled workflows in a repository with no activity for about 60 days. Check the current GitHub documentation, and set a reminder to make a small commit now and then, or add a keep-alive step.
- **Free-tier limits change.** Model names, quotas and availability on free AI tiers can change without notice. This is exactly why the fallback chain exists.
- **Forecasts are forecasts.** Open-Meteo data is model output, and tropical showers can be hard to predict. The bot reports probabilities; it cannot promise dry clothes.
- **Single location.** Coordinates are hard-coded for Warri.
- **Delivery only.** The bot sends messages but does not receive or answer them.

---

## How this project was built

The project was developed in two stages: prototype in a notebook first, then move to a repository. It was built step by step so every piece was understood before the next one was added.

**Stage 1: Google Colab prototype**

1. **Telegram first.** Create the bot, fetch the chat ID, send a test message. This proves the delivery end before anything else exists.
2. **Open-Meteo fetch.** Request daily and hourly data for Warri and inspect the raw tables to understand their shape.
3. **Drying logic.** Turn hourly rows into a best window, a verdict and rain periods, then check the results against the data by eye.
4. **AI wording.** Feed only the computed facts to a model and send the result to Telegram, with retries.
5. **Fallbacks.** Add the template message and error alerts, then test every failure path on purpose with a force-fail switch.

**Stage 2: script and GitHub Actions**

6. Move the notebook functions into one script, switch secrets to environment variables, add a command-line mode argument, and wrap it in a workflow with two cron schedules and a manual trigger.

**Lessons learned along the way**

- **Check the data, not just the label.** The first drying rule treated 0.1 mm of rain (the smallest amount the API reports) as a ruined hour, which split a good afternoon into fragments and turned a good drying day into a "poor" one. Looking at hour-by-hour pass/fail output exposed the problem immediately.
- **Free AI tiers can be flaky.** The first provider needed several retries before succeeding, which is what motivated both the fallback chain and switching the main provider.
- **Keep model output clean.** Some models emit reasoning text before the answer; the script strips it before sending.
- **Never trust the model with facts.** Letting code compute the window and having the model only word it prevents invented times.
- **Design failures first.** Deciding what the bot does when each part breaks, and then testing those paths deliberately, matters more than the happy path for something that runs unattended.

---

## Ideas for later

- Add a keep-alive step so scheduled runs never pause from inactivity
- A short weekly outlook message (for example Sunday evening)
- Highlight the best day of the coming week for heavy washing
- Support a second location, such as a pickup area
- Learn from feedback: let a reply such as "clothes dried fine" adjust the thresholds
- Add a simple log of daily verdicts to compare against real drying results over time
- Bump the pinned versions of the GitHub actions when newer major versions are released

---

## Useful links

- Open-Meteo forecast API: https://open-meteo.com/en/docs
- Telegram Bot API: https://core.telegram.org/bots/api
- Groq documentation: https://console.groq.com/docs
- Google AI Studio and Gemini API: https://ai.google.dev
- GitHub Actions documentation: https://docs.github.com/actions
- GitHub Actions workflow schedule syntax: https://docs.github.com/actions/using-workflows/events-that-trigger-workflows#schedule
