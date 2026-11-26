"""Deterministic and validated LLM structured opening hours extraction and schedule resolution."""

from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import date, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contracts.llm import DayOpeningHours, StructuredOpeningHours

logger = logging.getLogger(__name__)

_DAY_NAMES: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

_DAY_ALIASES: dict[str, str] = {
    "mon": "Monday",
    "monday": "Monday",
    "mondays": "Monday",
    "tue": "Tuesday",
    "tues": "Tuesday",
    "tuesday": "Tuesday",
    "tuesdays": "Tuesday",
    "wed": "Wednesday",
    "wednesday": "Wednesday",
    "wednesdays": "Wednesday",
    "thu": "Thursday",
    "thur": "Thursday",
    "thurs": "Thursday",
    "thursday": "Thursday",
    "thursdays": "Thursday",
    "fri": "Friday",
    "friday": "Friday",
    "fridays": "Friday",
    "sat": "Saturday",
    "saturday": "Saturday",
    "saturdays": "Saturday",
    "sun": "Sunday",
    "sunday": "Sunday",
    "sundays": "Sunday",
}

_MONTH_NUMS: dict[str, int] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_HHMM_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SINGLE_TIME_RE = re.compile(
    r"(?<!\d)(\d{1,2})(?::|\.)?(\d{2})?\s*(am|pm|hrs?)?(?!\d)", re.IGNORECASE
)
_TIME_RANGE_RE = re.compile(
    r"(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|hrs?)?)\s*(?:-|to|–|—|till|until)\s*"
    r"(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|hrs?)?)",
    re.IGNORECASE,
)


class ExtractedHoursPayload(BaseModel):
    """Normalized source backed opening hours payload."""

    model_config = ConfigDict(frozen=True)

    place_name: str | None = None
    source_url: str | None = None
    source_kind: str = "PAGE_CONTENT"
    local_timezone: str = "Asia/Kolkata"
    opens: str | None = None
    closes: str | None = None
    ticketing_opens: str | None = None
    ticketing_closes: str | None = None
    last_entry: str | None = None
    days_covered: list[str] = Field(default_factory=list)
    closed_days: list[str] = Field(default_factory=list)
    dates_covered: str | None = None
    weekly_schedule: list[dict[str, Any]] = Field(default_factory=list)
    activity_schedules: dict[str, dict[str, Any]] = Field(default_factory=dict)
    raw_excerpt: str | None = None


def normalize_time_to_hhmm(raw: str | None, *, default_meridiem: str | None = None) -> str | None:
    """Normalizes common 12h or 24h time expressions into strict HH:MM 24h strings."""
    if not raw or not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower().replace(".", ":")
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm|hrs?)?\s*", cleaned)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or "0")
    meridiem = (m.group(3) or "").lower()
    if not meridiem and m.group(2) is None:
        # Bare integers without minutes or AM/PM are too ambiguous outside explicit range pairs
        if default_meridiem is None:
            return None
        meridiem = default_meridiem.lower()

    if minute < 0 or minute > 59:
        return None
    if meridiem in ("pm",):
        if 13 <= hour <= 23:
            # Tolerate 24h hours carrying a redundant pm suffix (e.g. 18.00pm)
            pass
        elif hour < 1 or hour > 12:
            return None
        elif hour != 12:
            hour += 12
    elif meridiem in ("am",):
        if hour < 1 or hour > 12:
            return None
        if hour == 12:
            hour = 0
    else:
        if hour < 0 or hour > 23:
            return None
    return f"{hour:02d}:{minute:02d}"


def parse_hhmm_time(val: str | None) -> time | None:
    """Parses strict HH:MM string into datetime.time."""
    if not val or not isinstance(val, str):
        return None
    cleaned = val.strip()
    if not _HHMM_RE.match(cleaned):
        return None
    parts = cleaned.split(":")
    return time(int(parts[0]), int(parts[1]))


def _canonical_day(token: str) -> str | None:
    return _DAY_ALIASES.get(token.strip().lower().rstrip(",."))


def _expand_day_range(start_token: str, end_token: str) -> list[str]:
    start_day = _canonical_day(start_token)
    end_day = _canonical_day(end_token)
    if not start_day or not end_day:
        return []
    start_idx = _DAY_NAMES.index(start_day)
    end_idx = _DAY_NAMES.index(end_day)
    result: list[str] = []
    idx = start_idx
    while True:
        result.append(_DAY_NAMES[idx])
        if idx == end_idx:
            break
        idx = (idx + 1) % 7
        if len(result) > 7:
            break
    return result


def _parse_time_pair(start_raw: str, end_raw: str) -> tuple[str | None, str | None]:
    # Propagate trailing meridiem if omitted on the opening side (e.g. "9 - 5 PM" vs "9 AM - 5 PM")
    end_norm = normalize_time_to_hhmm(end_raw)
    if end_norm is None:
        return None, None
    start_norm = normalize_time_to_hhmm(start_raw)
    if start_norm is None:
        # Infer AM for start when end is PM
        end_hour = int(end_norm.split(":")[0])
        inferred = "am" if end_hour >= 12 else None
        start_norm = normalize_time_to_hhmm(start_raw, default_meridiem=inferred)
    if start_norm is None:
        return None, None
    t_s = parse_hhmm_time(start_norm)
    t_e = parse_hhmm_time(end_norm)
    if t_s is None or t_e is None or t_e <= t_s:
        return None, None
    return start_norm, end_norm


_SECONDARY_ACTIVITY_RE = re.compile(
    r"\b(?:"
    r"sound\s+and\s+light(?:\s+program)?(?:\s+show)?|"
    r"son\s+et\s+lumiere|"
    r"illumination(?:\s+timings?)?|"
    r"light\s+show|"
    r"laser\s+show|"
    r"cultural\s+show|"
    r"boating(?:\s+timings?)?|"
    r"safari(?:\s+timings?)?"
    r")\b",
    re.IGNORECASE,
)
_GENERAL_ADMISSION_RE = re.compile(
    r"\b(?:"
    r"palace\s+entrance(?:\s+timings?)?|"
    r"general\s+admission|"
    r"visiting\s+hours|"
    r"entrance\s+timings?|"
    r"entry\s+timings?|"
    r"museum\s+timings?|"
    r"garden\s+timings?"
    r")\b",
    re.IGNORECASE,
)


def _segment_text_by_activity(normalized_text: str) -> dict[str, str]:
    """Splits page text into general_admission vs named secondary activity sections."""
    # Split on sentence boundaries or explicit activity heading boundaries
    raw_segments = re.split(
        r"(?<=[.!?])\s+|(?=\b(?:Sound\s+and\s+Light\s+Show|Illumination\s+timings?|Palace\s+Entrance\s+Timings?|General\s+Admission|Visiting\s+Hours)\b)",
        normalized_text,
        flags=re.IGNORECASE,
    )
    buckets: dict[str, list[str]] = {"general_admission": []}
    current_activity = "general_admission"

    for seg in raw_segments:
        s = seg.strip()
        if not s:
            continue
        has_gen = bool(_GENERAL_ADMISSION_RE.search(s))
        sec_m = _SECONDARY_ACTIVITY_RE.search(s)
        if has_gen and not sec_m:
            current_activity = "general_admission"
        elif sec_m and not has_gen:
            sec_raw = sec_m.group(0).lower()
            if "illumination" in sec_raw:
                current_activity = "illumination"
            elif "sound" in sec_raw or "son et lumiere" in sec_raw or "light" in sec_raw or "laser" in sec_raw:
                current_activity = "sound_and_light_show"
            elif "boating" in sec_raw:
                current_activity = "boating"
            elif "safari" in sec_raw:
                current_activity = "safari"
            else:
                current_activity = "secondary_show"
        elif "click to book" in s.lower() and current_activity == "general_admission":
            pass
        buckets.setdefault(current_activity, []).append(s)

    return {k: " ".join(v).strip() for k, v in buckets.items() if v}


def _extract_schedule_fields(normalized_text: str) -> dict[str, Any]:
    """Extracts opening hours, ticketing cutoffs, closed days, and days covered from one activity text."""
    closed_days: list[str] = []
    day_pattern = r"(?:mon(?:days?)?|tue(?:sdays?)?|wed(?:nesdays?)?|thu(?:rsdays?)?|fri(?:days?)?|sat(?:urdays?)?|sun(?:days?)?)"

    for m in re.finditer(
        rf"(?:closed\s+(?:on\s+)?|except\s+)({day_pattern}(?:\s*(?:-|to|–|,|and|&)\s*{day_pattern})*)",
        normalized_text,
        re.IGNORECASE,
    ):
        span_text = m.group(1)
        range_closed = re.search(
            rf"({day_pattern})\s*(?:-|to|–)\s*({day_pattern})", span_text, re.IGNORECASE
        )
        if range_closed:
            for exp_day in _expand_day_range(range_closed.group(1), range_closed.group(2)):
                if exp_day not in closed_days:
                    closed_days.append(exp_day)
        else:
            for tok in re.findall(day_pattern, span_text, re.IGNORECASE):
                canon_day = _canonical_day(tok)
                if canon_day and canon_day not in closed_days:
                    closed_days.append(canon_day)

    for m in re.finditer(rf"({day_pattern})\s+closed\b", normalized_text, re.IGNORECASE):
        trail_day = _canonical_day(m.group(1))
        if trail_day and trail_day not in closed_days:
            closed_days.append(trail_day)

    # Extract ticketing hours window separately from venue opening hours
    ticketing_opens: str | None = None
    ticketing_closes: str | None = None
    masked_text = normalized_text

    ticket_range_m = re.search(
        r"(?:ticketing(?:\s+hours)?|ticket\s+(?:counter|window|sales?)|tickets?\s+issued)[^.;\n]{0,40}?"
        + _TIME_RANGE_RE.pattern,
        normalized_text,
        re.IGNORECASE,
    )
    if ticket_range_m:
        t_o, t_c = _parse_time_pair(ticket_range_m.group(1), ticket_range_m.group(2))
        if t_o and t_c:
            ticketing_opens, ticketing_closes = t_o, t_c
            masked_text = masked_text.replace(ticket_range_m.group(0), " ")
    else:
        ticket_close_m = re.search(
            r"(?:ticket\s+(?:counter|window)\s+closes?(?:\s+at)?|tickets?\s+(?:available\s+)?(?:till|until))\s+"
            r"(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|hrs?)?)",
            normalized_text,
            re.IGNORECASE,
        )
        if ticket_close_m:
            tc = normalize_time_to_hhmm(ticket_close_m.group(1))
            if tc:
                ticketing_closes = tc
                masked_text = masked_text.replace(ticket_close_m.group(0), " ")

    # Extract last entry cutoff separately
    last_entry: str | None = None
    last_entry_m = re.search(
        r"(?:last\s+(?:entry|admission|ticket))(?:\s+(?:at|by|is|:))?\s*"
        r"(\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm|hrs?)?)",
        masked_text,
        re.IGNORECASE,
    )
    if last_entry_m:
        le = normalize_time_to_hhmm(last_entry_m.group(1))
        if le:
            last_entry = le
            masked_text = masked_text.replace(last_entry_m.group(0), " ")

    # Extract primary opening and closing range from remaining text
    opens: str | None = None
    closes: str | None = None
    for m in _TIME_RANGE_RE.finditer(masked_text):
        o_val, c_val = _parse_time_pair(m.group(1), m.group(2))
        if o_val and c_val:
            opens, closes = o_val, c_val
            break

    # Extract days covered while ignoring ranges preceded by closed or except
    days_covered: list[str] = []
    if re.search(r"\b(?:daily|all\s+days|every\s+day|everyday)\b", normalized_text, re.IGNORECASE):
        days_covered = [d for d in _DAY_NAMES if d not in closed_days]
    else:
        for day_range_m in re.finditer(
            rf"(?<!closed\s)(?<!except\s)({day_pattern})\s*(?:-|to|–|through)\s*({day_pattern})",
            normalized_text,
            re.IGNORECASE,
        ):
            prefix = normalized_text[max(0, day_range_m.start() - 16) : day_range_m.start()].lower()
            if "closed" in prefix or "except" in prefix:
                continue
            for exp_d in _expand_day_range(day_range_m.group(1), day_range_m.group(2)):
                if exp_d not in closed_days and exp_d not in days_covered:
                    days_covered.append(exp_d)
        if not days_covered and closed_days:
            days_covered = [d for d in _DAY_NAMES if d not in closed_days]

    # Extract seasonal date range if present
    dates_covered: str | None = None
    month_pat = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    season_m = re.search(
        rf"({month_pat})\s*(?:-|to|–)\s*({month_pat})",
        normalized_text,
        re.IGNORECASE,
    )
    if season_m:
        dates_covered = f"{season_m.group(1)} to {season_m.group(2)}"
    elif re.search(r"\b(?:all\s+year|year\s+round)\b", normalized_text, re.IGNORECASE):
        dates_covered = "All year"

    weekly_schedule: list[dict[str, Any]] = []
    if days_covered or closed_days:
        for d in _DAY_NAMES:
            if d in closed_days:
                weekly_schedule.append({"day": d, "is_closed": True})
            elif not days_covered or d in days_covered:
                if opens and closes:
                    weekly_schedule.append(
                        {
                            "day": d,
                            "is_closed": False,
                            "opens": opens,
                            "closes": closes,
                            "ticketing_opens": ticketing_opens,
                            "ticketing_closes": ticketing_closes,
                            "last_entry": last_entry,
                            "dates_covered": dates_covered,
                        }
                    )

    return {
        "opens": opens,
        "closes": closes,
        "ticketing_opens": ticketing_opens,
        "ticketing_closes": ticketing_closes,
        "last_entry": last_entry,
        "days_covered": days_covered,
        "closed_days": closed_days,
        "dates_covered": dates_covered,
        "weekly_schedule": weekly_schedule,
    }


def extract_structured_hours_from_text(
    text: str,
    *,
    source_url: str | None = None,
    local_timezone: str = "Asia/Kolkata",
    place_name: str | None = None,
    source_kind: str = "PAGE_CONTENT",
) -> ExtractedHoursPayload | None:
    """Deterministically extracts opening, ticketing, last entry, and closed days from page text."""
    if not text or not isinstance(text, str):
        return None
    normalized_text = " ".join(text.split())
    if not normalized_text:
        return None

    buckets = _segment_text_by_activity(normalized_text)
    gen_text = buckets.get("general_admission") or ""
    primary_fields = _extract_schedule_fields(gen_text) if gen_text else {}

    activity_schedules: dict[str, dict[str, Any]] = {}
    for act_name, act_text in buckets.items():
        if act_name == "general_admission":
            continue
        act_fields = _extract_schedule_fields(act_text)
        if any(
            act_fields.get(k)
            for k in ("opens", "closes", "closed_days", "days_covered", "last_entry", "ticketing_closes")
        ):
            activity_schedules[act_name] = act_fields

    if not primary_fields or (
        not primary_fields.get("opens")
        and not primary_fields.get("closes")
        and not primary_fields.get("closed_days")
        and not primary_fields.get("last_entry")
        and not primary_fields.get("ticketing_closes")
    ):
        if not activity_schedules:
            primary_fields = _extract_schedule_fields(normalized_text)
        else:
            return None

    opens = primary_fields.get("opens")
    closes = primary_fields.get("closes")
    ticketing_opens = primary_fields.get("ticketing_opens")
    ticketing_closes = primary_fields.get("ticketing_closes")
    last_entry = primary_fields.get("last_entry")
    days_covered = primary_fields.get("days_covered") or []
    closed_days = primary_fields.get("closed_days") or []
    dates_covered = primary_fields.get("dates_covered")
    weekly_schedule = primary_fields.get("weekly_schedule") or []

    if not opens and not closes and not closed_days and not last_entry and not ticketing_closes:
        return None

    return ExtractedHoursPayload(
        place_name=place_name,
        source_url=source_url,
        source_kind=source_kind,
        local_timezone=local_timezone,
        opens=opens,
        closes=closes,
        ticketing_opens=ticketing_opens,
        ticketing_closes=ticketing_closes,
        last_entry=last_entry,
        days_covered=days_covered,
        closed_days=closed_days,
        dates_covered=dates_covered,
        weekly_schedule=weekly_schedule,
        activity_schedules=activity_schedules,
        raw_excerpt=normalized_text[:500],
    )


def _extract_json_block(raw_text: str) -> dict[str, Any] | None:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


async def extract_structured_hours_with_llm(
    llm_client: Any | None,
    *,
    place_name: str,
    destination_name: str,
    page_text: str,
    source_url: str,
    local_timezone: str = "Asia/Kolkata",
) -> ExtractedHoursPayload | None:
    """Uses configured LLM client to parse structured opening hours from page text with strict validation."""
    if llm_client is None or not hasattr(llm_client, "generate") or not page_text or not page_text.strip():
        return None

    system_prompt = (
        "Extract ONLY factual opening hours, closed days, ticketing hours, and last entry cutoffs "
        "present in the supplied page excerpt. Respond strictly with valid JSON containing keys: "
        '"weekly_schedule" (list of {day, is_closed, opens, closes, ticketing_opens, ticketing_closes, last_entry}), '
        '"timezone", "dates_covered". Use 24 hour HH:MM format. If hours are absent, return {"weekly_schedule": []}.'
    )
    prompt = (
        f"Place: {place_name}\nDestination: {destination_name}\n"
        f"Source URL: {source_url}\nTimezone fallback: {local_timezone}\n\n"
        f"Page Text:\n{page_text[:3500]}"
    )

    try:
        raw_output = await llm_client.generate(
            prompt,
            system_prompt=system_prompt,
            max_tokens=800,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("LLM structured hours extraction failed for '%s': %s", place_name, exc)
        return None

    payload = _extract_json_block(raw_output)
    if not payload:
        return None

    raw_days = payload.get("weekly_schedule")
    if raw_days is None:
        raw_days = payload.get("days")
    if not isinstance(raw_days, list) or not raw_days:
        return None

    validated_days: list[DayOpeningHours] = []
    for entry in raw_days:
        if not isinstance(entry, dict):
            return None
        day_canonical = _canonical_day(str(entry.get("day") or ""))
        if not day_canonical:
            return None
        is_closed = bool(entry.get("is_closed", False))
        opens_val = str(entry.get("opens") or ("00:00" if is_closed else "")).strip()
        closes_val = str(entry.get("closes") or ("00:00" if is_closed else "")).strip()
        if not _HHMM_RE.match(opens_val) or not _HHMM_RE.match(closes_val):
            return None
        if not is_closed:
            t_o = parse_hhmm_time(opens_val)
            t_c = parse_hhmm_time(closes_val)
            if t_o is None or t_c is None or t_c <= t_o:
                return None

        tick_o = entry.get("ticketing_opens")
        tick_c = entry.get("ticketing_closes")
        last_e = entry.get("last_entry")
        for opt_t in (tick_o, tick_c, last_e):
            if opt_t is not None and not _HHMM_RE.match(str(opt_t).strip()):
                return None

        try:
            day_obj = DayOpeningHours(
                day=day_canonical,
                opens=opens_val,
                closes=closes_val,
                is_closed=is_closed,
                dates_covered=entry.get("dates_covered") or payload.get("dates_covered"),
                ticketing_opens=str(tick_o).strip() if tick_o else None,
                ticketing_closes=str(tick_c).strip() if tick_c else None,
                last_entry=str(last_e).strip() if last_e else None,
                note=entry.get("note"),
            )
            validated_days.append(day_obj)
        except ValidationError:
            return None

    try:
        structured = StructuredOpeningHours(
            days=validated_days,
            timezone=str(payload.get("timezone") or local_timezone),
            source_url=source_url,
            dates_covered=payload.get("dates_covered"),
        )
    except ValidationError:
        return None

    open_days = [d for d in structured.days if not d.is_closed]
    closed = [d.day for d in structured.days if d.is_closed]
    primary = open_days[0] if open_days else None

    return ExtractedHoursPayload(
        place_name=place_name,
        source_url=source_url,
        source_kind="LLM_STRUCTURED",
        local_timezone=structured.timezone or local_timezone,
        opens=primary.opens if primary else None,
        closes=primary.closes if primary else None,
        ticketing_opens=primary.ticketing_opens if primary else None,
        ticketing_closes=primary.ticketing_closes if primary else None,
        last_entry=primary.last_entry if primary else None,
        days_covered=[d.day for d in open_days],
        closed_days=closed,
        dates_covered=structured.dates_covered,
        weekly_schedule=[d.model_dump() for d in structured.days],
        raw_excerpt=page_text[:500],
    )


def _date_in_seasonal_range(target_date: date, dates_covered: str | None) -> bool:
    if not dates_covered:
        return True
    month_pat = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    m = re.search(rf"{month_pat}\s*(?:-|to|–)\s*{month_pat}", dates_covered, re.IGNORECASE)
    if not m:
        return True
    m1 = _MONTH_NUMS.get(m.group(1).lower())
    m2 = _MONTH_NUMS.get(m.group(2).lower())
    if m1 is None or m2 is None:
        return True
    cur = target_date.month
    if m1 <= m2:
        return m1 <= cur <= m2
    return cur >= m1 or cur <= m2


def resolve_hours_for_dates(
    typed_val: dict[str, Any],
    visit_dates: list[date] | None = None,
    local_timezone: str = "Asia/Kolkata",
) -> dict[str, Any]:
    """Resolves verified schedule windows and closed days against target local travel dates."""
    closed_days_norm = {
        c
        for raw in (typed_val.get("closed_days") or [])
        if (c := _canonical_day(str(raw))) is not None
    }
    days_covered_norm = [
        c
        for raw in (typed_val.get("days_covered") or [])
        if (c := _canonical_day(str(raw))) is not None
    ]
    dates_covered = typed_val.get("dates_covered")
    weekly = typed_val.get("weekly_schedule") or []
    weekly_by_day: dict[str, dict[str, Any]] = {}
    for item in weekly:
        if isinstance(item, dict) and item.get("day"):
            cday = _canonical_day(str(item["day"]))
            if cday:
                weekly_by_day[cday] = item

    if not visit_dates:
        return {
            "all_closed": False,
            "closed_visit_dates": [],
            "open_visit_dates": [],
            "unknown_visit_dates": [],
            "opens": typed_val.get("opens") or typed_val.get("open"),
            "closes": typed_val.get("closes") or typed_val.get("close"),
            "ticketing_opens": typed_val.get("ticketing_opens"),
            "ticketing_closes": typed_val.get("ticketing_closes"),
            "last_entry": typed_val.get("last_entry"),
            "closed_days": sorted(closed_days_norm),
            "days_covered": days_covered_norm,
            "dates_covered": dates_covered,
            "local_timezone": typed_val.get("local_timezone") or local_timezone,
            "source_url": typed_val.get("source_url"),
        }

    open_dates: list[dict[str, Any]] = []
    closed_dates: list[dict[str, Any]] = []
    unknown_dates: list[dict[str, Any]] = []
    has_explicit_day_coverage = bool(days_covered_norm) or bool(weekly_by_day)

    for d in visit_dates:
        weekday_name = calendar.day_name[d.weekday()]
        day_entry = weekly_by_day.get(weekday_name)
        in_season = _date_in_seasonal_range(d, dates_covered)
        is_explicitly_closed = (
            weekday_name in closed_days_norm
            or (day_entry is not None and bool(day_entry.get("is_closed")))
            or not in_season
        )
        if is_explicitly_closed:
            reason = (
                f"Closed on {weekday_name}"
                if weekday_name in closed_days_norm or (day_entry and day_entry.get("is_closed"))
                else f"Outside seasonal window ({dates_covered})"
            )
            closed_dates.append({"date": d.isoformat(), "weekday": weekday_name, "reason": reason})
            continue

        is_covered_open = (
            not has_explicit_day_coverage
            or weekday_name in days_covered_norm
            or (day_entry is not None and not bool(day_entry.get("is_closed")))
        )
        if not is_covered_open:
            unknown_dates.append(
                {
                    "date": d.isoformat(),
                    "weekday": weekday_name,
                    "reason": f"Date not covered by schedule ({', '.join(days_covered_norm)})",
                }
            )
            continue

        o = (day_entry or {}).get("opens") or typed_val.get("opens") or typed_val.get("open")
        c = (day_entry or {}).get("closes") or typed_val.get("closes") or typed_val.get("close")
        t_o = (day_entry or {}).get("ticketing_opens") or typed_val.get("ticketing_opens")
        t_c = (day_entry or {}).get("ticketing_closes") or typed_val.get("ticketing_closes")
        le = (day_entry or {}).get("last_entry") or typed_val.get("last_entry")
        if o and c:
            open_dates.append(
                {
                    "date": d.isoformat(),
                    "weekday": weekday_name,
                    "opens": o,
                    "closes": c,
                    "ticketing_opens": t_o,
                    "ticketing_closes": t_c,
                    "last_entry": le,
                }
            )
        else:
            unknown_dates.append(
                {
                    "date": d.isoformat(),
                    "weekday": weekday_name,
                    "reason": f"Missing opening/closing times for {weekday_name}",
                }
            )

    first_open = open_dates[0] if open_dates else {}
    return {
        "all_closed": len(open_dates) == 0 and len(closed_dates) > 0 and len(unknown_dates) == 0,
        "closed_visit_dates": closed_dates,
        "open_visit_dates": open_dates,
        "unknown_visit_dates": unknown_dates,
        "opens": first_open.get("opens") if open_dates else None,
        "closes": first_open.get("closes") if open_dates else None,
        "ticketing_opens": first_open.get("ticketing_opens") if open_dates else None,
        "ticketing_closes": first_open.get("ticketing_closes") if open_dates else None,
        "last_entry": first_open.get("last_entry") if open_dates else None,
        "closed_days": sorted(closed_days_norm),
        "days_covered": days_covered_norm,
        "dates_covered": dates_covered,
        "local_timezone": typed_val.get("local_timezone") or local_timezone,
        "source_url": typed_val.get("source_url"),
    }
