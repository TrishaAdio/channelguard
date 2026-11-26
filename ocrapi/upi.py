from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from statistics import median

from .models import Line

IST = timezone(timedelta(hours=5, minutes=30))
MAX_AMOUNT = Decimal("10000000")

NUMBER = r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
SYMBOL_AMOUNT_RE = re.compile(rf"(?:₹|\brs\.?|\binr\b|\brupees?\b)\s*({NUMBER})(?![\d,])", re.IGNORECASE)
MISREAD_SYMBOL_RE = re.compile(rf"^([?zZ7F%&<{{€¥$2])\s*({NUMBER})$")
BARE_AMOUNT_RE = re.compile(rf"^({NUMBER})$")
AMOUNT_LABEL_RE = re.compile(rf"\b(?:amount|amt|total)\b\s*(?:paid|sent|debited)?\s*[:\-]?\s*(?:₹|rs\.?|inr)?\s*({NUMBER})?\s*$", re.IGNORECASE)
DISTRACTOR_RE = re.compile(r"cash\s*back|reward|balance|\bfee|charges|limit|scratch|\bwon\b|offer|coins|points|discount|emi", re.IGNORECASE)

UTR_LABEL_RE = re.compile(
    r"\b(?:utr(?:\s*(?:no|number|id))?|upi\s*ref(?:erence)?(?:\s*(?:no|number|id))?|"
    r"upi\s*(?:transaction|txn)\s*id|rrn|bank\s*ref(?:erence)?(?:\s*(?:no|number|id))?|"
    r"ref(?:erence)?\s*(?:no|number|id))\b\.?\s*[:\-#]?\s*(.*)$",
    re.IGNORECASE,
)
TXN_LABEL_RE = re.compile(
    r"\b(?:google\s+transaction\s+id|transaction\s+id|txn\s*id|order\s*id|transaction\s+ref(?:erence)?)\b"
    r"\.?\s*[:\-#]?\s*(.*)$",
    re.IGNORECASE,
)
PHONEPE_TXN_RE = re.compile(r"\bT\d{21}\b")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]{7,}")
VPA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-_]{1,255}@[A-Za-z][A-Za-z0-9]{1,63}")
ACCOUNT_RE = re.compile(r"(?:[xX*•●.]{2,}|a/c\s*(?:no\.?)?\s*[xX*]*)\s*(\d{4})\b", re.IGNORECASE)
PAYEE_LABEL_RE = re.compile(
    r"^(?:paid\s+to|sent\s+to|money\s+sent\s+to|payment\s+to|transferred\s+to|to)\b\s*[:\-]?\s*(.*)$",
    re.IGNORECASE,
)
PAYER_LABEL_RE = re.compile(
    r"^(?:from|paid\s+by|sender|debited\s+from|sent\s+from)\b\s*[:\-]?\s*(.*)$",
    re.IGNORECASE,
)
BANK_IN_PARENS_RE = re.compile(r"^(.*?)\s*\(([^)]+)\)\s*$")

STATUS_WORDS = {
    "failed": re.compile(r"\b(?:failed|failure|declined|unsuccessful|rejected|cancelled|reversed)\b", re.IGNORECASE),
    "pending": re.compile(r"\b(?:pending|processing|in\s+progress|awaiting)\b", re.IGNORECASE),
    "success": re.compile(
        r"\b(?:successful(?:ly)?|success|completed|paid\s+successfully|payment\s+done|sent\s+successfully)\b",
        re.IGNORECASE,
    ),
}

APPS = (
    ("PhonePe", re.compile(r"phone\s*pe", re.IGNORECASE)),
    ("Google Pay", re.compile(r"google\s*pay|\bg\s?pay\b", re.IGNORECASE)),
    ("Paytm", re.compile(r"\bpaytm\b", re.IGNORECASE)),
    ("BHIM", re.compile(r"\bbhim\b", re.IGNORECASE)),
    ("Amazon Pay", re.compile(r"amazon\s*pay", re.IGNORECASE)),
    ("CRED", re.compile(r"\bcred\b", re.IGNORECASE)),
    ("MobiKwik", re.compile(r"mobikwik", re.IGNORECASE)),
    ("WhatsApp Pay", re.compile(r"whatsapp", re.IGNORECASE)),
    ("Navi", re.compile(r"\bnavi\b", re.IGNORECASE)),
    ("super.money", re.compile(r"super\.?\s*money", re.IGNORECASE)),
    ("Freecharge", re.compile(r"freecharge", re.IGNORECASE)),
)
VPA_HANDLE_APPS = {
    "ybl": "PhonePe", "ibl": "PhonePe", "axl": "PhonePe",
    "okaxis": "Google Pay", "okhdfcbank": "Google Pay", "okicici": "Google Pay", "oksbi": "Google Pay",
    "paytm": "Paytm", "ptyes": "Paytm", "ptaxis": "Paytm", "pthdfc": "Paytm", "ptsbi": "Paytm",
    "upi": "BHIM", "apl": "Amazon Pay", "yapl": "Amazon Pay", "axisb": "CRED", "ikwik": "MobiKwik",
}

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sept?|oct|nov|dec)[a-z]*\.?"
DATE_PATTERNS = (
    ("dmy_text", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{MONTH},?\s*(\d{{4}})?", re.IGNORECASE)),
    ("mdy_text", re.compile(rf"\b{MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s*(\d{{4}})?", re.IGNORECASE)),
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("dmy_num", re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})\b")),
)
TIME_RE = re.compile(r"\b(\d{1,2})[:.](\d{2})(?:[:.](\d{2}))?\s*([ap])\.?\s*m\b\.?|\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b",
                     re.IGNORECASE)

DIGIT_FIXES = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "|": "1",
                             "i": "1", "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "g": "9"})


@dataclass
class Field:
    value: object = None
    confidence: float = 0.0
    source: str = ""

    def as_dict(self) -> dict:
        return {"value": self.value, "confidence": round(self.confidence, 3), "source": self.source}


@dataclass
class Receipt:
    fields: dict[str, Field]
    warnings: list[str] = field(default_factory=list)


def _valid_grouping(number: str) -> bool:
    whole = number.split(".")[0]
    if "," not in whole:
        return True
    groups = whole.split(",")
    if not 1 <= len(groups[0]) <= 3 or len(groups[-1]) != 3:
        return False
    middle = groups[1:-1]
    return all(len(g) == 2 for g in middle) or all(len(g) == 3 for g in middle)


def _money(number: str) -> Decimal | None:
    if not _valid_grouping(number):
        return None
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    if value <= 0 or value > MAX_AMOUNT:
        return None
    return value.quantize(Decimal("0.01"))


def _looks_like_id_or_time(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    if "," not in text and len(digits) >= 7:
        return True
    return bool(re.fullmatch(r"\d{1,2}[:.]\d{2}", text)) or bool(re.fullmatch(r"(19|20)\d{2}", text))


def _amount_field(lines: list[Line]) -> tuple[Field, list[str]]:
    heights = [line.height for line in lines] or [1]
    typical = median(heights)
    candidates: list[tuple[Decimal, float, str]] = []

    def add(number: str, weight: float, index: int):
        value = _money(number)
        if value is None:
            return
        line = lines[index]
        context = line.text + " " + (lines[index - 1].text if index else "")
        if DISTRACTOR_RE.search(context):
            weight *= 0.3
        prominence = min(line.height / typical, 2.0)
        score = weight * line.score * (0.8 + 0.1 * prominence)
        candidates.append((value, score, line.text))

    for index, line in enumerate(lines):
        text = line.text.strip()
        compact = text.replace(" ", "")
        symbol_hits = list(SYMBOL_AMOUNT_RE.finditer(text))
        for match in symbol_hits:
            add(match.group(1), 1.0, index)
        if symbol_hits:
            continue
        label = AMOUNT_LABEL_RE.search(text)
        if label:
            if label.group(1):
                add(label.group(1), 0.95, index)
            elif index + 1 < len(lines):
                nxt = lines[index + 1].text.replace(" ", "")
                for regex in (BARE_AMOUNT_RE, MISREAD_SYMBOL_RE):
                    hit = regex.match(nxt)
                    if hit:
                        add(hit.group(hit.lastindex), 0.9, index + 1)
            continue
        misread = MISREAD_SYMBOL_RE.match(compact)
        if misread and not _looks_like_id_or_time(misread.group(2)):
            add(misread.group(2), 0.85 if not misread.group(1).isdigit() else 0.5, index)
        bare = BARE_AMOUNT_RE.match(compact)
        if bare and not _looks_like_id_or_time(compact):
            add(bare.group(1), 0.45, index)

    if not candidates:
        return Field(), []

    grouped: dict[Decimal, list[tuple[float, str]]] = {}
    for value, score, source in candidates:
        grouped.setdefault(value, []).append((score, source))
    ranked = sorted(
        ((min(0.99, max(s for s, _ in hits) + 0.12 * (len(hits) - 1)), value, hits) for value, hits in grouped.items()),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_value, best_hits = ranked[0]
    warnings = []
    if len(ranked) > 1 and ranked[1][0] > 0.8 * best_score:
        best_score *= 0.7
        warnings.append(f"amount: ambiguous between {best_value} and {ranked[1][1]}")
    source = max(best_hits, key=lambda hit: hit[0])[1]
    return Field(format(best_value, "f"), best_score, source), warnings


def _fix_digits(text: str) -> str:
    def repair(match: re.Match) -> str:
        token = match.group(0)
        digits = sum(ch.isdigit() for ch in token)
        compact = token.replace(" ", "")
        if digits >= 0.75 * len(compact):
            return compact.translate(DIGIT_FIXES)
        return token
    return re.sub(r"[0-9OoDQIl|iSsBZzg](?:[0-9OoDQIl|iSsBZzg ]*[0-9OoDQIl|iSsBZzg])?", repair, text)


def _utr_in(text: str) -> str | None:
    match = re.search(r"(?<![\dA-Za-z])\d{12}(?!\d)", _fix_digits(text))
    return match.group(0) if match else None


def _utr_field(lines: list[Line]) -> Field:
    for index, line in enumerate(lines):
        label = UTR_LABEL_RE.search(line.text)
        if not label:
            continue
        for offset, weight in ((0, 1.0), (1, 0.97), (2, 0.85)):
            if index + offset >= len(lines):
                break
            target = label.group(1) if offset == 0 else lines[index + offset].text
            utr = _utr_in(target)
            if utr:
                return Field(utr, weight * lines[index + offset].score, lines[index + offset].text)
    for line in lines:
        if TXN_LABEL_RE.search(line.text) and not UTR_LABEL_RE.search(line.text):
            continue
        match = re.search(r"(?<![\dA-Za-z])\d{12}(?!\d)", line.text.replace(" ", ""))
        if match:
            return Field(match.group(0), 0.6 * line.score, line.text)
    return Field()


def _transaction_id_field(lines: list[Line]) -> Field:
    for line in lines:
        hit = PHONEPE_TXN_RE.search(line.text.replace(" ", ""))
        if hit:
            return Field(hit.group(0), line.score, line.text)
    for index, line in enumerate(lines):
        label = TXN_LABEL_RE.search(line.text)
        if not label or re.search(r"\bupi\s*(?:transaction|txn)\s*id", line.text, re.IGNORECASE):
            continue
        for offset in (0, 1):
            if index + offset >= len(lines):
                break
            target = label.group(1) if offset == 0 else lines[index + offset].text
            token = TOKEN_RE.search(target.replace(" ", ""))
            if token and (offset == 1 or any(ch.isdigit() for ch in token.group(0))):
                return Field(token.group(0), 0.95 * lines[index + offset].score, lines[index + offset].text)
    return Field()


def _status_field(lines: list[Line]) -> Field:
    heights = [line.height for line in lines] or [1]
    typical = median(heights)
    found: dict[str, tuple[float, str]] = {}
    for line in lines:
        if len(line.text.split()) > 6:
            continue
        for status, regex in STATUS_WORDS.items():
            if regex.search(line.text):
                score = line.score * min(1.0, 0.85 + 0.1 * line.height / typical)
                if score > found.get(status, (0, ""))[0]:
                    found[status] = (score, line.text)
    for status in ("failed", "pending", "success"):
        if status in found:
            score, source = found[status]
            return Field(status, score, source)
    return Field()


def _resolve_year(day: int, month: int, now: datetime) -> date | None:
    for year in (now.year, now.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate <= now.date() + timedelta(days=1):
            return candidate
    return None


def _date_in(text: str, now: datetime) -> date | None:
    for kind, regex in DATE_PATTERNS:
        match = regex.search(text)
        if not match:
            continue
        try:
            if kind == "dmy_text":
                day, month, year = int(match.group(1)), MONTHS[match.group(2).lower()[:3]], match.group(3)
            elif kind == "mdy_text":
                month, day, year = MONTHS[match.group(1).lower()[:3]], int(match.group(2)), match.group(3)
            elif kind == "iso":
                year, month, day = match.group(1), int(match.group(2)), int(match.group(3))
            else:
                day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
                if len(year) == 2:
                    year = "20" + year
            if year:
                return date(int(year), month, day)
            return _resolve_year(day, month, now)
        except (ValueError, KeyError):
            continue
    return None


def _time_in(text: str) -> tuple[int, int, int] | None:
    for match in TIME_RE.finditer(text):
        if match.group(1):
            hour, minute, second = int(match.group(1)), int(match.group(2)), int(match.group(3) or 0)
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if match.group(4).lower() == "p" else 0)
        else:
            hour, minute, second = int(match.group(5)), int(match.group(6)), int(match.group(7) or 0)
        if hour < 24 and minute < 60 and second < 60:
            return hour, minute, second
    return None


def _datetime_field(lines: list[Line], now: datetime) -> Field:
    best: tuple[int, date | None, tuple | None, Line] | None = None
    for index, line in enumerate(lines):
        if UTR_LABEL_RE.search(line.text) or TXN_LABEL_RE.search(line.text):
            continue
        found_date = _date_in(line.text, now)
        found_time = _time_in(line.text)
        if found_date and not found_time and index + 1 < len(lines):
            found_time = _time_in(lines[index + 1].text) if not _date_in(lines[index + 1].text, now) else None
        rank = (2 if found_date else 0) + (1 if found_time else 0)
        if found_date and (best is None or rank > best[0]):
            best = (rank, found_date, found_time, line)
    if best is None:
        return Field()
    _, found_date, found_time, line = best
    if found_time:
        stamp = datetime(found_date.year, found_date.month, found_date.day, *found_time, tzinfo=IST)
        return Field(stamp.isoformat(), line.score, line.text)
    return Field(found_date.isoformat(), 0.85 * line.score, line.text)


def _clean_name(text: str) -> str | None:
    text = VPA_RE.sub("", text).strip(" :-,")
    text = re.sub(r"\s{2,}", " ", text)
    if not text or len(text) > 60 or not re.search(r"[A-Za-z]{2}", text):
        return None
    if ACCOUNT_RE.search(text) or re.fullmatch(r"[\d +\-]+", text):
        return None
    if any(regex.search(text) for regex in STATUS_WORDS.values()):
        return None
    return text


def _party(lines: list[Line], label_re: re.Pattern, stop_re: re.Pattern) -> dict[str, Field]:
    result = {"name": Field(), "vpa": Field(), "bank": Field(), "account_last4": Field()}
    for index, line in enumerate(lines):
        label = label_re.match(line.text.strip())
        if not label:
            continue
        window = [(label.group(1), line)]
        for nxt in lines[index + 1:index + 4]:
            if stop_re.match(nxt.text.strip()) or UTR_LABEL_RE.search(nxt.text) or TXN_LABEL_RE.search(nxt.text):
                break
            window.append((nxt.text, nxt))
        for text, source in window:
            if not text.strip():
                continue
            bank = BANK_IN_PARENS_RE.match(text.strip())
            if bank and not result["bank"].value:
                result["bank"] = Field(bank.group(2).strip(), source.score, source.text)
                text = bank.group(1)
            vpa = VPA_RE.search(text)
            if vpa and not result["vpa"].value:
                result["vpa"] = Field(vpa.group(0), source.score, source.text)
            account = ACCOUNT_RE.search(text)
            if account and not result["account_last4"].value:
                result["account_last4"] = Field(account.group(1), source.score, source.text)
            name = _clean_name(re.sub(r"^upi\s*id\s*[:\-]?", "", text, flags=re.IGNORECASE))
            if name and not result["name"].value and not re.match(r"upi\s*id", text, re.IGNORECASE):
                result["name"] = Field(name, source.score * (1.0 if source is line else 0.95), source.text)
        if result["name"].value and result["vpa"].value:
            break
    return result


def _app_field(lines: list[Line], vpas: list[str], txn: str | None) -> Field:
    for line in lines:
        for app, regex in APPS:
            if regex.search(line.text):
                return Field(app, 0.95 * line.score, line.text)
    if txn and PHONEPE_TXN_RE.fullmatch(txn):
        return Field("PhonePe", 0.8, txn)
    return Field()


def _utr_date_hint(utr: str | None, stamp: str | None) -> bool | None:
    if not utr or not stamp:
        return None
    day = date.fromisoformat(stamp[:10])
    if int(utr[0]) != day.year % 10:
        return False
    day_of_year = int(utr[1:4])
    return abs(day_of_year - day.timetuple().tm_yday) <= 1


def parse(lines: list[Line], now: datetime | None = None) -> Receipt:
    now = now or datetime.now(IST)
    amount, warnings = _amount_field(lines)
    utr = _utr_field(lines)
    txn = _transaction_id_field(lines)
    if txn.value == utr.value:
        txn = Field()
    payee = _party(lines, PAYEE_LABEL_RE, PAYER_LABEL_RE)
    payer = _party(lines, PAYER_LABEL_RE, PAYEE_LABEL_RE)
    vpas = [v for v in (payee["vpa"].value, payer["vpa"].value) if v]
    fields = {
        "amount": amount,
        "status": _status_field(lines),
        "utr": utr,
        "transaction_id": txn,
        "datetime": _datetime_field(lines, now),
        "payee_name": payee["name"],
        "payee_vpa": payee["vpa"],
        "payer_name": payer["name"],
        "payer_vpa": payer["vpa"],
        "payer_bank": payer["bank"],
        "payer_account_last4": payer["account_last4"] if payer["account_last4"].value else payee["account_last4"],
        "app": _app_field(lines, vpas, txn.value),
    }
    if not fields["app"].value:
        for vpa in vpas:
            handle = vpa.split("@", 1)[1].lower()
            if handle in VPA_HANDLE_APPS:
                fields["app"] = Field(VPA_HANDLE_APPS[handle], 0.5, vpa)
                break
    if not fields["payer_account_last4"].value:
        for line in lines:
            account = ACCOUNT_RE.search(line.text)
            if account:
                fields["payer_account_last4"] = Field(account.group(1), 0.8 * line.score, line.text)
                break
    return Receipt(fields, warnings)


KEY_FIELDS = ("amount", "status", "utr", "datetime")


def merge(receipts: list[Receipt]) -> Receipt:
    if len(receipts) == 1:
        return receipts[0]
    warnings: list[str] = []
    merged: dict[str, Field] = {}
    for name in receipts[0].fields:
        options = [r.fields[name] for r in receipts if r.fields[name].value is not None]
        if not options:
            merged[name] = Field()
            continue
        votes: dict[object, list[Field]] = {}
        for option in options:
            votes.setdefault(option.value, []).append(option)
        ranked = sorted(votes.items(), key=lambda item: (len(item[1]), max(f.confidence for f in item[1])),
                        reverse=True)
        value, agreeing = ranked[0]
        best = max(agreeing, key=lambda f: f.confidence)
        confidence = best.confidence
        for extra in sorted(agreeing, key=lambda f: f.confidence, reverse=True)[1:]:
            confidence += (1 - confidence) * 0.5 * extra.confidence
        if len(ranked) > 1:
            rival_value, rival = ranked[1]
            rival_best = max(rival, key=lambda f: f.confidence)
            if len(rival) == len(agreeing) and rival_best.confidence > best.confidence:
                value, best, confidence = rival_value, rival_best, rival_best.confidence
                rival_value = ranked[0][0]
            if name in KEY_FIELDS:
                warnings.append(f"{name}: passes disagree ({value} vs {rival_value})")
            confidence *= 0.8
        merged[name] = Field(value, min(confidence, 0.999), best.source)
    for receipt in receipts:
        for warning in receipt.warnings:
            if warning not in warnings and not warning.endswith("not found"):
                warnings.append(warning)
    return Receipt(merged, warnings)


def checks(receipt: Receipt, now: datetime | None = None) -> tuple[dict, list[str]]:
    now = now or datetime.now(IST)
    fields = receipt.fields
    warnings = list(receipt.warnings)
    stamp = fields["datetime"].value
    future = None
    age_hours = None
    if stamp:
        if "T" in stamp:
            moment = datetime.fromisoformat(stamp)
            future = moment > now + timedelta(minutes=10)
        else:
            moment = datetime.fromisoformat(stamp + "T00:00:00+05:30")
            future = moment.date() > now.date()
        age_hours = round((now - moment).total_seconds() / 3600, 2)
        if future:
            warnings.append("datetime: receipt time is in the future")
    utr_hint = _utr_date_hint(fields["utr"].value, stamp)
    if utr_hint is False:
        warnings.append("utr: its date prefix does not match the receipt date (common on edited screenshots, "
                        "but some banks use other formats)")
    for name in KEY_FIELDS:
        current = fields[name]
        if current.value is None:
            warnings.append(f"{name}: not found")
        elif current.confidence < 0.8:
            warnings.append(f"{name}: low confidence ({current.confidence:.2f})")
    if fields["status"].value not in (None, "success"):
        warnings.append(f"status: payment is {fields['status'].value}")
    complete = all(fields[n].value is not None and fields[n].confidence >= 0.8 for n in KEY_FIELDS)
    result = {
        "complete": complete,
        "is_success": fields["status"].value == "success",
        "in_future": future,
        "age_hours": age_hours,
        "utr_matches_date": utr_hint,
    }
    return result, warnings
