"""Weather bot: sends a laundry-focused weather message for Warri to Telegram.

Usage: python weather_bot.py morning|evening
"""
import argparse
import json
import os
import re
import time

import pandas as pd
import requests

# ---------- Settings ----------
WARRI_LAT = 5.52
WARRI_LON = 5.75

GROQ_MODEL = "qwen/qwen3.8-27b"
GEMINI_MODEL = "gemini-2.5-flash"   # replace with the model that worked for you in Colab

# Drying rules (tune these to how your clothes really dry)
DRY_START, DRY_END = 8, 17     # hours when line drying is realistic
RAIN_START, RAIN_END = 7, 19   # hours that matter for pickups/deliveries
MAX_RAIN_PROB = 40             # % - above this, an hour is risky
MAX_RAIN_MM = 0.3              # mm - traces below this don't matter
MAX_HUMIDITY = 85              # % - above this, drying is very slow

RETRYABLE = {429, 500, 502, 503, 504}

# Testing only: comma-separated list of "groq", "gemini", "weather"
FORCE_FAIL = {x.strip() for x in os.environ.get("FORCE_FAIL", "").split(",") if x.strip()}

WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    80: "light rain showers", 81: "rain showers", 82: "violent rain showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}

VERDICT_RANK = {"poor": 0, "risky": 1, "good": 2}


# ---------- Secrets ----------
def get_secret(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing secret: {name}")
    return value


# ---------- Telegram ----------
def send_telegram(text):
    token = get_secret("TELEGRAM_BOT_TOKEN")
    chat_id = get_secret("TELEGRAM_CHAT_ID")
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        # "from None" hides the original error, whose text contains the bot URL
        raise RuntimeError(f"Telegram network error: {type(e).__name__}") from None
    if not resp.ok:
        raise RuntimeError(f"Telegram error HTTP {resp.status_code}: {resp.text[:150]}")


def safe_send(text):
    """Send to Telegram without raising (used for alerts)."""
    try:
        send_telegram(text)
    except Exception as e:
        print(f"Alert could not be sent: {type(e).__name__}")


# ---------- Open-Meteo ----------
def fetch_weather():
    params = {
        "latitude": WARRI_LAT,
        "longitude": WARRI_LON,
        "timezone": "Africa/Lagos",
        "forecast_days": 2,
        "daily": ",".join([
            "weather_code", "temperature_2m_max", "temperature_2m_min",
            "precipitation_sum", "precipitation_probability_max", "uv_index_max",
        ]),
        "hourly": ",".join([
            "temperature_2m", "relative_humidity_2m", "precipitation_probability",
            "precipitation", "wind_speed_10m", "shortwave_radiation",
        ]),
    }
    resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_weather_with_retry(max_attempts=3):
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            if "weather" in FORCE_FAIL:
                raise RuntimeError("forced failure for testing")
            return fetch_weather()
        except Exception as e:
            last = e
            print(f"Open-Meteo attempt {attempt} failed: {type(e).__name__}")
            if attempt < max_attempts:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Open-Meteo failed after {max_attempts} attempts: {last}")


# ---------- Drying logic ----------
def get_day_hours(weather, day_index):
    hourly = pd.DataFrame(weather["hourly"])
    hourly["time"] = pd.to_datetime(hourly["time"])
    target_date = pd.to_datetime(weather["daily"]["time"][day_index]).date()
    day = hourly[hourly["time"].dt.date == target_date].copy()
    day["hour"] = day["time"].dt.hour
    return day


def find_best_window(day):
    dry = day[(day["hour"] >= DRY_START) & (day["hour"] <= DRY_END)]
    best, current = [], []
    for _, row in dry.iterrows():
        ok = (
            row["precipitation_probability"] <= MAX_RAIN_PROB
            and row["precipitation"] < MAX_RAIN_MM
            and row["relative_humidity_2m"] <= MAX_HUMIDITY
        )
        if ok:
            current.append(int(row["hour"]))
            if len(current) > len(best):
                best = current.copy()
        else:
            current = []
    if not best:
        return None
    return (best[0], best[-1] + 1)


def verdict_from_window(window):
    if window is None:
        return "poor"
    hours = window[1] - window[0]
    if hours >= 5:
        return "good"
    if hours >= 3:
        return "risky"
    return "poor"


def find_rain_hours(day):
    rain = day[(day["hour"] >= RAIN_START) & (day["hour"] <= RAIN_END)]
    wet = rain[(rain["precipitation_probability"] >= 60) | (rain["precipitation"] >= 0.5)]
    return [int(h) for h in wet["hour"]]


def summarize_day(weather, day_index):
    daily = weather["daily"]
    day = get_day_hours(weather, day_index)
    window = find_best_window(day)
    dry_hours = day[(day["hour"] >= DRY_START) & (day["hour"] <= DRY_END)]
    return {
        "date": daily["time"][day_index],
        "weather_code": daily["weather_code"][day_index],
        "temp_max": daily["temperature_2m_max"][day_index],
        "temp_min": daily["temperature_2m_min"][day_index],
        "rain_mm": daily["precipitation_sum"][day_index],
        "rain_prob_max": daily["precipitation_probability_max"][day_index],
        "uv_max": daily["uv_index_max"][day_index],
        "avg_humidity_drying_hours": round(float(dry_hours["relative_humidity_2m"].mean())),
        "avg_wind_drying_hours": round(float(dry_hours["wind_speed_10m"].mean()), 1),
        "best_window": window,
        "verdict": verdict_from_window(window),
        "rain_hours": find_rain_hours(day),
    }


# ---------- Formatting ----------
def fmt_hour(h):
    h = h % 24
    return f"{h % 12 or 12}{'AM' if h < 12 else 'PM'}"


def hours_to_ranges(hours):
    ranges = []
    for h in hours:
        if ranges and h == ranges[-1][1] + 1:
            ranges[-1][1] = h
        else:
            ranges.append([h, h])
    return [f"{fmt_hour(a)} to {fmt_hour(b + 1)}" for a, b in ranges]


def readable_day(d):
    w = d["best_window"]
    return {
        "date": d["date"],
        "conditions": WEATHER_CODES.get(d["weather_code"], "mixed conditions"),
        "high_c": d["temp_max"],
        "low_c": d["temp_min"],
        "total_rain_mm": d["rain_mm"],
        "max_rain_chance_percent": d["rain_prob_max"],
        "uv_index_max": d["uv_max"],
        "avg_humidity_percent_in_drying_hours": d["avg_humidity_drying_hours"],
        "avg_wind_kmh_in_drying_hours": d["avg_wind_drying_hours"],
        "drying_verdict": d["verdict"],
        "best_line_drying_window": f"{fmt_hour(w[0])} to {fmt_hour(w[1])}" if w else "none",
        "rain_periods_7AM_to_7PM": hours_to_ranges(d["rain_hours"]) or "none expected",
    }


def build_prompt(mode, today, tomorrow):
    if mode == "morning":
        task = ("Write the MORNING message about TODAY only. "
                "Tell me what today looks like and how it affects line drying and pickups/deliveries.")
        data = {"today": readable_day(today)}
    else:
        task = ("Write the EVENING message about TOMORROW. Say how tomorrow compares with today "
                "(better, worse or similar for drying, rain, heat), then give planning advice, "
                "for example which loads to wash now or later.")
        data = {"today": readable_day(today), "tomorrow": readable_day(tomorrow)}

    return f"""You are a weather assistant for a small laundry business in Warri, Nigeria.
The owner dries most clothes outdoors on a line and handles customer pickups and deliveries.

{task}

Rules:
- Use ONLY the facts below. Never invent numbers, times or conditions.
- Plain text only: no markdown, no asterisks, no bullet symbols. A few emojis are fine.
- Maximum 120 words. Friendly and practical.
- Mention the best line-drying window if there is one, and when rain is expected.
- Give 2 or 3 concrete tips (hanging times, indoor drying, bagging finished orders, delivery timing).
- Use the times exactly as written below (for example "9AM to 4PM").
- Do not assume how I deliver (no bikes, cars or other vehicles) or add any detail that is not in the facts.

Facts:
{json.dumps(data, indent=2)}
"""


# ---------- AI providers ----------
def clean_model_output(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    return text.strip()


def call_groq(prompt, max_attempts=3):
    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {get_secret('GROQ_API_KEY')}"}
    body = {
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": 2000,
    }
    last_error = "unknown error"

    for attempt in range(1, max_attempts + 1):
        wait = 2 ** attempt
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
        except requests.exceptions.RequestException as e:
            last_error = f"network error: {type(e).__name__}"
        else:
            if resp.status_code == 200:
                choices = resp.json().get("choices") or []
                raw = choices[0]["message"].get("content", "") if choices else ""
                text = clean_model_output(raw)
                if text:
                    return text
                last_error = "empty response"
            elif resp.status_code in RETRYABLE:
                last_error = f"HTTP {resp.status_code} (retryable)"
                try:
                    wait = min(float(resp.headers.get("retry-after", wait)), 20)
                except ValueError:
                    pass
            else:
                raise RuntimeError(f"Groq error HTTP {resp.status_code}: {resp.text[:200]}")

        print(f"Groq attempt {attempt} failed: {last_error}")
        if attempt < max_attempts:
            time.sleep(wait)

    raise RuntimeError(f"Groq failed after {max_attempts} attempts: {last_error}")


def call_gemini(prompt, max_attempts=3):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {"x-goog-api-key": get_secret("GEMINI_API_KEY")}
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4},
    }
    last_error = "unknown error"

    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
        except requests.exceptions.RequestException as e:
            last_error = f"network error: {type(e).__name__}"
        else:
            if resp.status_code == 200:
                candidates = resp.json().get("candidates") or []
                parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
                text = "".join(p.get("text", "") for p in parts).strip()
                if text:
                    return text
                last_error = "empty response"
            elif resp.status_code in RETRYABLE:
                last_error = f"HTTP {resp.status_code} (retryable)"
            else:
                raise RuntimeError(f"Gemini error HTTP {resp.status_code}: {resp.text[:200]}")

        print(f"Gemini attempt {attempt} failed: {last_error}")
        if attempt < max_attempts:
            time.sleep(2 ** attempt)

    raise RuntimeError(f"Gemini failed after {max_attempts} attempts: {last_error}")


# ---------- Template fallback ----------
def compare_days(today, tomorrow):
    lines = []
    t, n = VERDICT_RANK[today["verdict"]], VERDICT_RANK[tomorrow["verdict"]]
    if n > t:
        lines.append("Drying: better than today.")
    elif n < t:
        lines.append("Drying: worse than today.")
    else:
        lines.append("Drying: about the same as today.")

    rain_diff = tomorrow["rain_mm"] - today["rain_mm"]
    if rain_diff <= -2:
        lines.append("Rain: less than today.")
    elif rain_diff >= 2:
        lines.append("Rain: more than today.")
    else:
        lines.append("Rain: similar to today.")

    temp_diff = round(tomorrow["temp_max"] - today["temp_max"], 1)
    if temp_diff >= 1.5:
        lines.append(f"Heat: about {temp_diff}°C hotter than today.")
    elif temp_diff <= -1.5:
        lines.append(f"Heat: about {abs(temp_diff)}°C cooler than today.")
    return lines


def template_message(mode, today, tomorrow):
    d = today if mode == "morning" else tomorrow
    r = readable_day(d)
    greeting = "Good morning" if mode == "morning" else "Good evening"
    label = "today" if mode == "morning" else "tomorrow"

    rain_periods = r["rain_periods_7AM_to_7PM"]
    if isinstance(rain_periods, list):
        rain_periods = ", ".join(rain_periods)

    lines = [
        f"{greeting}! Weather for {label} ({r['date']}):",
        f"{r['conditions'].capitalize()}, {r['low_c']} to {r['high_c']}°C",
        f"Rain: {r['total_rain_mm']} mm total, up to {r['max_rain_chance_percent']}% chance",
        f"Rain periods (7AM to 7PM): {rain_periods}",
        f"Line drying: {r['drying_verdict'].upper()}",
        f"Best drying window: {r['best_line_drying_window']}",
        f"UV index up to {r['uv_index_max']}",
    ]
    if mode == "evening":
        lines.append("")
        lines.extend(compare_days(today, tomorrow))
    lines.append("")
    lines.append("(Basic summary: the AI message was unavailable.)")
    return "\n".join(lines)


# ---------- Orchestration ----------
def generate_message(mode, today, tomorrow):
    prompt = build_prompt(mode, today, tomorrow)
    for name, fn in [("groq", call_groq), ("gemini", call_gemini)]:
        try:
            if name in FORCE_FAIL:
                raise RuntimeError("forced failure for testing")
            return fn(prompt), name
        except Exception as e:
            print(f"{name} failed: {str(e)[:100]}")
    return template_message(mode, today, tomorrow), "template"


def run(mode):
    try:
        weather = fetch_weather_with_retry()
    except Exception:
        safe_send("⚠️ Weather bot: I couldn't get the forecast this time, so there is no weather update. "
                  "Please check the weather another way today.")
        raise

    try:
        today = summarize_day(weather, 0)
        tomorrow = summarize_day(weather, 1)
        text, source = generate_message(mode, today, tomorrow)
        send_telegram(text)
        print(f"{mode} message sent via {source}")
    except Exception as e:
        safe_send(f"⚠️ Weather bot error ({type(e).__name__}). No weather update this time.")
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["morning", "evening"])
    run(parser.parse_args().mode)


if __name__ == "__main__":
    main()
