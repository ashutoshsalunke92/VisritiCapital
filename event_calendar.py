"""
Event Calendar & Notifications (PDF Module 07). Checks for high-impact news days
before entry and flags open positions that span a gap event.

Two sources:
  1. Static YAML: config/event_calendar/india_fixed_events.yaml — manually
     maintained list of India-specific events known in advance (RBI MPC dates,
     Union Budget). These don't need a live feed.
  2. Finnhub economic-calendar API (free tier, no credit card) — supplements with
     global macro events (Fed decisions, US CPI/NFP) that can gap Indian markets.
     Falls back gracefully to static-only if Finnhub is unreachable.

Notification: Telegram outbound-only for now (no interactive buttons per the PDF's
current scope decision). A critical alert on an event-gap sends a Telegram message
AND writes a pending_confirmations.yaml flag file — the system pauses that runner
until the flag is manually cleared (or cleared via the CLI menu's "Resume" option).
"""
import os
import yaml
import requests
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_EVENTS_PATH = os.path.join(_THIS_DIR, "config", "event_calendar", "india_fixed_events.yaml")
_PENDING_CONF_PATH = os.path.join(_THIS_DIR, "config", "pending_confirmations.yaml")


@dataclass
class CalendarEvent:
    name: str
    event_date: date
    impact: str   # "HIGH" | "MEDIUM"
    source: str   # "static" | "finnhub"


class EventCalendar:
    def __init__(self, finnhub_api_key: Optional[str] = None):
        self.finnhub_api_key = finnhub_api_key
        self._static_events: list[CalendarEvent] = []
        self._loaded = False

    def _load_static(self):
        if not os.path.isfile(_STATIC_EVENTS_PATH):
            return []
        with open(_STATIC_EVENTS_PATH, "r") as f:
            data = yaml.safe_load(f) or {}
        events = []
        for e in data.get("fixed_events", []):
            try:
                events.append(CalendarEvent(
                    name=e["name"],
                    event_date=datetime.strptime(e["date"], "%Y-%m-%d").date(),
                    impact=e.get("impact", "HIGH"),
                    source="static",
                ))
            except Exception:
                pass
        return events

    def _load_finnhub(self, window_days: int = 7) -> list[CalendarEvent]:
        if not self.finnhub_api_key:
            return []
        today = date.today()
        from_str = today.strftime("%Y-%m-%d")
        to_str = (today + timedelta(days=window_days)).strftime("%Y-%m-%d")
        try:
            resp = requests.get(
                "https://finnhub.io/api/v1/calendar/economic",
                params={"from": from_str, "to": to_str, "token": self.finnhub_api_key},
                timeout=10,
            )
            if resp.status_code != 200:
                return []
            events = []
            for e in resp.json().get("economicCalendar", []):
                impact = e.get("impact", "").upper()
                if impact not in ("HIGH",):
                    continue  # only block on high-impact global events
                events.append(CalendarEvent(
                    name=e.get("event", "Unknown"),
                    event_date=datetime.strptime(e["date"], "%Y-%m-%d").date(),
                    impact=impact,
                    source="finnhub",
                ))
            return events
        except Exception:
            return []  # Finnhub unreachable → degrade gracefully to static-only

    def _ensure_loaded(self):
        if not self._loaded:
            self._static_events = self._load_static()
            self._loaded = True

    def is_high_impact_day(self, check_date: date) -> tuple[bool, list[CalendarEvent]]:
        """Returns (is_flagged, [events on that day]). Checks static file always;
        adds Finnhub only when checking near-future dates (to avoid hammering the API)."""
        self._ensure_loaded()
        days_ahead = (check_date - date.today()).days
        finnhub_events = self._load_finnhub(window_days=days_ahead + 2) if 0 <= days_ahead <= 14 else []
        all_events = self._static_events + finnhub_events
        matched = [e for e in all_events if e.event_date == check_date and e.impact == "HIGH"]
        return bool(matched), matched

    def upcoming_high_impact(self, window_days: int = 14) -> list[CalendarEvent]:
        """Returns high-impact events in the next `window_days` days."""
        self._ensure_loaded()
        today = date.today()
        finnhub = self._load_finnhub(window_days=window_days)
        all_events = self._static_events + finnhub
        return [e for e in all_events
                if e.impact == "HIGH" and today <= e.event_date <= today + timedelta(days=window_days)]


class PendingConfirmations:
    """File-backed pause/resume flag for event-gap situations. When the calendar
    flags an open position spanning a high-impact event, trading_engine.py calls
    pause_runner(runner_id). The runner stays paused (no new entries, no adjustment
    rolls) until resume_runner(runner_id) is called — either via the CLI menu's
    'Resume paused runner' option or a direct file edit."""

    def __init__(self, path: str = _PENDING_CONF_PATH):
        self.path = path

    def _load(self) -> dict:
        if not os.path.isfile(self.path):
            return {}
        with open(self.path, "r") as f:
            return yaml.safe_load(f) or {}

    def _save(self, data: dict):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            yaml.dump(data, f)

    def is_paused(self, runner_id: str) -> bool:
        return self._load().get(runner_id, {}).get("awaiting_confirmation", False)

    def pause_runner(self, runner_id: str, reason: str):
        data = self._load()
        data[runner_id] = {"awaiting_confirmation": True, "reason": reason,
                            "paused_at": datetime.now().isoformat()}
        self._save(data)

    def resume_runner(self, runner_id: str):
        data = self._load()
        if runner_id in data:
            data[runner_id]["awaiting_confirmation"] = False
            data[runner_id]["resumed_at"] = datetime.now().isoformat()
            self._save(data)

    def list_paused(self) -> list[dict]:
        data = self._load()
        return [{"runner_id": k, **v} for k, v in data.items()
                if v.get("awaiting_confirmation")]


class Notifier:
    """Telegram outbound-only notifications. Non-blocking: send() logs to console
    and returns even if Telegram call fails — a failed notification must never stall
    trading logic (per PDF Module 07)."""

    def __init__(self, bot_token: Optional[str], chat_id: Optional[str]):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._enabled = bool(bot_token and chat_id)

    def send(self, message: str, level: str = "info"):
        prefix = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "•")
        full_msg = f"{prefix} {message}"
        print(f"[NOTIFY/{level.upper()}] {full_msg}")
        if not self._enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": full_msg},
                timeout=5,
            )
        except Exception as e:
            print(f"[NOTIFY] Telegram send failed (non-blocking): {e}")
