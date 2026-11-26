"""Shared formatting, identifiers and person-form validation."""
from __future__ import annotations

import calendar
import json
import re
import secrets
import time
from datetime import datetime, timezone

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

GENDERS = {"male": "Male", "female": "Female", "other": "Other"}
GENDER_ICONS = {"male": "♂", "female": "♀", "other": "◇"}

PRIVACY_GROUPS = {
    "birth": "Birth details",
    "death": "Death details",
    "memorial": "Resting place & memorial",
    "contact": "Contact details",
    "address": "Residential address",
    "notes": "Notes",
    "photo": "Profile photo",
}

# Field -> privacy group (used for masking).
FIELD_GROUP = {
    "birth_day": "birth", "birth_month": "birth", "birth_year": "birth", "birth_qualifier": "birth",
    "death_day": "death", "death_month": "death", "death_year": "death", "death_qualifier": "death",
    "resting_place": "memorial", "memorial_note": "memorial",
    "email": "contact", "phone": "contact", "phone_alt": "contact",
    "address_line1": "address", "address_line2": "address", "city": "address",
    "region": "address", "postal_code": "address", "country": "address",
    "notes": "notes", "avatar": "photo",
}

TEXT_FIELDS = {
    "given_name": 120,
    "middle_names": 160,
    "surname": 120,
    "maiden_name": 120,
    "nickname": 80,
    "resting_place": 240,
    "memorial_note": 4000,
    "notes": 12000,
    "email": 254,
    "phone": 60,
    "phone_alt": 60,
    "address_line1": 240,
    "address_line2": 240,
    "city": 120,
    "region": 120,
    "postal_code": 32,
    "country": 120,
}

BIRTH_KEYS = ("birth_day", "birth_month", "birth_year", "birth_qualifier")
DEATH_KEYS = ("death_day", "death_month", "death_year", "death_qualifier")
QUALIFIERS = {"exact", "about", "before", "after"}

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def now() -> int:
    return int(time.time())


def new_id() -> str:
    return secrets.token_hex(12)


def clean_text(value: str) -> str:
    return CONTROL_CHARS.sub("", value).strip()


def fget(form, name: str) -> str:
    value = form.get(name)
    if value is None:
        return ""
    if hasattr(value, "filename"):
        return ""
    return str(value)


def _parse_int(value: str, label: str, lo: int, hi: int, errors: list[str]) -> int | None:
    if value == "":
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number.")
        return None
    if num < lo or num > hi:
        errors.append(f"{label} must be between {lo} and {hi}.")
        return None
    return num


def _date_group(form, prefix: str, label: str, errors: list[str]) -> dict:
    day = _parse_int(fget(form, prefix + "_day"), f"{label} day", 1, 31, errors)
    month = _parse_int(fget(form, prefix + "_month"), f"{label} month", 1, 12, errors)
    year = _parse_int(fget(form, prefix + "_year"), f"{label} year", 1000, 2200, errors)
    qualifier = fget(form, prefix + "_qualifier") or "exact"
    if qualifier not in QUALIFIERS:
        errors.append(f"{label} date qualifier is invalid.")
        qualifier = "exact"
    if day is not None and month is None:
        errors.append(f"{label}: please choose a month when a day is given.")
    if day and month:
        max_day = calendar.monthrange(year or 2000, month)[1]
        if day > max_day:
            errors.append(f"{label}: day {day} does not exist in that month.")
    if not (day or month or year):
        return {f"{prefix}_day": None, f"{prefix}_month": None, f"{prefix}_year": None,
                f"{prefix}_qualifier": "exact"}
    return {f"{prefix}_day": day, f"{prefix}_month": month, f"{prefix}_year": year,
            f"{prefix}_qualifier": qualifier}


def validate_person_form(form, partial: bool = False) -> tuple[dict, list[str]]:
    """Validate a person form into a clean dict.

    In partial mode only fields actually present in the submission are
    returned, so a user editing one field cannot clobber private fields that
    were masked in their form.
    """
    data: dict = {}
    errors: list[str] = []
    present = set(form.keys())

    for field, max_len in TEXT_FIELDS.items():
        if field not in present:
            continue
        value = clean_text(fget(form, field))
        if len(value) > max_len:
            errors.append(f"{field.replace('_', ' ').title()} is too long (max {max_len}).")
        data[field] = value

    if "email" in data and data["email"]:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", data["email"]):
            errors.append("Please provide a valid email address.")

    if "gender" in present:
        gender = fget(form, "gender")
        if gender not in GENDERS:
            errors.append("Please choose a valid gender.")
        else:
            data["gender"] = gender

    living = None
    if "_living_present" in present:
        living = 1 if form.get("living") else 0
        data["is_living"] = living

    birth_present = any(k in present for k in BIRTH_KEYS)
    death_present = any(k in present for k in DEATH_KEYS)
    if partial:
        birth_present = birth_present and any(k in present for k in BIRTH_KEYS)
    if birth_present:
        data.update(_date_group(form, "birth", "Birth", errors))
    if death_present and (living != 1):
        data.update(_date_group(form, "death", "Death", errors))
    if living == 1:
        # Living people never carry death data.
        for key in DEATH_KEYS:
            data[key] = None if key != "death_qualifier" else "exact"
        data["resting_place"] = ""
        data["memorial_note"] = ""

    if "_privacy_present" in present:
        visibility = fget(form, "contact_visibility") or "all"
        if visibility not in ("all", "family", "private"):
            errors.append("Invalid contact visibility setting.")
            visibility = "all"
        private = [g for g in form.getlist("private_fields") if g in PRIVACY_GROUPS]
        data["contact_visibility"] = visibility
        data["private_fields"] = json.dumps(private)

    by, dy = data.get("birth_year"), data.get("death_year")
    if by and dy and dy < by:
        errors.append("Date of passing cannot be before date of birth.")

    if not partial and not (data.get("given_name") or data.get("surname")):
        errors.append("Please provide at least a given name or a family name.")

    # Normalise blank date strings to NULL-friendly values.
    for key in list(data.keys()):
        if data[key] == "" and key.endswith(("_qualifier",)):
            data[key] = "exact"
    return data, errors


def full_name(person) -> str:
    parts = [person["given_name"], person["middle_names"], person["surname"]]
    return " ".join(p for p in parts if p).strip() or person["nickname"] or "Unnamed person"


def display_name(person) -> str:
    parts = [person["given_name"], person["surname"]]
    name = " ".join(p for p in parts if p).strip()
    if not name:
        name = person["nickname"] or "Unnamed person"
    return name


def initials(person) -> str:
    g = (person["given_name"] or person["nickname"] or "?").strip()
    s = (person["surname"] or "").strip()
    out = (g[:1] + s[:1]).upper()
    return out or "?"


def format_parts(day, month, year, qualifier="exact") -> str:
    core = ""
    if day and month and year:
        core = f"{day} {MONTHS[month]} {year}"
    elif month and year:
        core = f"{MONTHS[month]} {year}"
    elif year:
        core = str(year)
    elif day and month:
        core = f"{day} {MONTHS[month]}"
    elif month:
        core = MONTHS[month]
    if not core:
        return ""
    if qualifier == "about":
        return "c. " + core
    if qualifier == "before":
        return "before " + core
    if qualifier == "after":
        return "after " + core
    return core


def lifespan(person) -> str:
    birth_year = person["birth_year"]
    death_year = person["death_year"]
    if person["is_living"]:
        if birth_year:
            return "b. " + format_parts(None, None, birth_year, person["birth_qualifier"])
        return "Living"
    if birth_year and death_year:
        prefix = "c. " if person["birth_qualifier"] == "about" else ""
        death = str(death_year)
        if person["death_qualifier"] == "about":
            death = "c. " + death
        return f"{prefix}{birth_year}–{death}"
    if death_year:
        prefix = "c. " if person["death_qualifier"] == "about" else ""
        return "† " + prefix + str(death_year)
    if birth_year:
        return f"{birth_year}–?"
    return "Deceased"


def birth_text(person) -> str:
    return format_parts(person["birth_day"], person["birth_month"], person["birth_year"],
                        person["birth_qualifier"]) or "Not recorded"


def death_text(person) -> str:
    return format_parts(person["death_day"], person["death_month"], person["death_year"],
                        person["death_qualifier"]) or "Not recorded"


def search_text(data: dict) -> str:
    parts = [data.get("given_name"), data.get("middle_names"), data.get("surname"),
             data.get("maiden_name"), data.get("nickname")]
    return " ".join(p.strip() for p in parts if p and p.strip()).lower()


def rel_time(ts: int | None) -> str:
    if not ts:
        return ""
    delta = now() - int(ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} min ago"
    if delta < 86400:
        return f"{delta // 3600} h ago"
    if delta < 86400 * 30:
        return f"{delta // 86400} d ago"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def sql_to_iso(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def safe_next(target: str | None) -> str:
    if not target:
        return "/tree"
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/tree"
