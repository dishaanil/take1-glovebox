"""Glovebox's books for one call: one driver, one load, in memory.

All money math, time math and spoken wording lives here, not in the LLM. Tools
pass the caller's words through; this module parses them, stores records as
pending, and returns the exact sentence the agent reads back. A record counts
only after the caller says yes to that readback.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import yaml

CATEGORIES = (
    "fuel",
    "def",
    "toll",
    "scale_fee",
    "parking",
    "lumper",
    "repair",
    "washout",
    "food",
    "other",
)
CATEGORY_WORDS = {
    "fuel": "fuel",
    "def": "DEF",
    "toll": "tolls",
    "scale_fee": "a scale fee",
    "parking": "parking",
    "lumper": "a lumper fee",
    "repair": "a repair",
    "washout": "a washout",
    "food": "food",
    "other": "an expense",
}
# Billed back to the broker unless the caller says otherwise.
REIMBURSABLE_BY_DEFAULT = {"lumper"}
# Minutes to unload at a delivery before driving to the next pickup.
UNLOAD_MINUTES = 60
# How many schedule items to say before offering the rest.
SCHEDULE_PREVIEW = 3


class LedgerError(Exception):
    """Something the caller said that can't be recorded. The message is spoken."""


# ---- spoken numbers -------------------------------------------------------

_ONES = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]
_TENS = [
    "_",
    "_",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
]


def number_words(n: int) -> str:
    """512 -> 'five hundred twelve'. Whole numbers below a billion."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return f"{_ONES[hundreds]} hundred" + (f" {number_words(rest)}" if rest else "")
    for size, name in ((1_000_000, "million"), (1_000, "thousand")):
        if n >= size:
            high, rest = divmod(n, size)
            return f"{number_words(high)} {name}" + (
                f" {number_words(rest)}" if rest else ""
            )
    raise ValueError(n)


def money_words(amount: Decimal) -> str:
    """Decimal('512.40') -> 'five hundred twelve dollars and forty cents'."""
    cents_total = int((amount * 100).to_integral_value(ROUND_HALF_UP))
    dollars, cents = divmod(abs(cents_total), 100)
    sign = "minus " if cents_total < 0 else ""
    words = f"{number_words(dollars)} dollar{'s' if dollars != 1 else ''}"
    if cents:
        words += f" and {number_words(cents)} cent{'s' if cents != 1 else ''}"
    return sign + words


def quantity_words(value: Decimal) -> str:
    """Decimal('140') -> 'one hundred forty', Decimal('62.5') -> 'sixty-two point five'."""
    whole = int(value)
    frac = f"{value:f}".partition(".")[2].rstrip("0")
    words = number_words(whole)
    if frac:
        words += " point " + " ".join(_ONES[int(d)] for d in frac)
    return words


def time_words(t: datetime) -> str:
    """7:50 PM -> 'seven fifty PM', 11:05 -> 'eleven oh five AM', 2:00 -> 'two PM'."""
    hour = t.hour % 12 or 12
    suffix = "AM" if t.hour < 12 else "PM"
    if t.minute == 0:
        return f"{number_words(hour)} {suffix}"
    minute = f"oh {_ONES[t.minute]}" if t.minute < 10 else number_words(t.minute)
    return f"{number_words(hour)} {minute} {suffix}"


def duration_words(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{number_words(hours)} hour{'s' if hours != 1 else ''}")
    if mins:
        parts.append(f"{number_words(mins)} minute{'s' if mins != 1 else ''}")
    return " and ".join(parts) or "no time"


def digit_words(digits: str) -> str:
    """'4471' -> 'four four seven one', the way a load number is said."""
    return " ".join(_ONES[int(d)] for d in digits if d.isdigit())


# ---- parsing the caller's words ------------------------------------------


def _vendor(vendor: str | None, category: str) -> str | None:
    """Drops placeholders the model sometimes passes instead of a business name."""
    if not vendor:
        return None
    name = vendor.strip()
    junk = {
        "unknown",
        "none",
        "n/a",
        category,
        category.replace("_", " "),
        "toll",
        "tolls",
    }
    return None if name.lower() in junk else name


def parse_amount(text: str) -> Decimal:
    """'512.40', '$512.40', '1,204', '85' -> Decimal. Refuses anything unclear."""
    cleaned = text.strip().replace("$", "").replace(",", "")
    cleaned = re.sub(r"\s*(dollars?|bucks?)$", "", cleaned, flags=re.I)
    try:
        value = Decimal(cleaned)
    except InvalidOperation as err:
        raise LedgerError(
            "I didn't catch the amount clearly. How much was it?"
        ) from err
    if value <= 0 or value >= 100_000 or value.as_tuple().exponent < -2:
        raise LedgerError("That amount doesn't sound right. How much was it?")
    return value


def parse_quantity(text: str) -> Decimal:
    try:
        value = Decimal(text.strip().replace(",", ""))
    except InvalidOperation as err:
        raise LedgerError("How many gallons was that?") from err
    if value <= 0 or value >= 1000:
        raise LedgerError("That gallon count doesn't sound right. How many gallons?")
    return value


_TIME = re.compile(r"^\s*(\d{1,2})(?::?(\d{2}))?\s*([ap])?\.?\s*m?\.?\s*$", re.I)


def parse_time(
    text: str,
    day: datetime,
    after: datetime | None = None,
    near: datetime | None = None,
) -> datetime:
    """'7:50', '7:50 PM', '1105', '11 am' on `day`.

    Without AM or PM, picks the reading closest to `near` (an old appointment),
    or the first one after `after` (an arrival), otherwise the most recent one
    that isn't in the future.
    """
    m = _TIME.match(text)
    if not m:
        raise LedgerError("What time was that, like seven fifty or two PM?")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if not (1 <= hour <= 12 or (0 <= hour <= 23 and not m.group(3))) or minute > 59:
        raise LedgerError("That time doesn't sound right. What time was it?")
    base = day.replace(hour=0, minute=0, second=0, microsecond=0)
    if m.group(3):
        h24 = hour % 12 + (12 if m.group(3).lower() == "p" else 0)
        return base + timedelta(hours=h24, minutes=minute)
    if hour > 12 or hour == 0:
        return base + timedelta(hours=hour, minutes=minute)
    options = [
        base + timedelta(hours=hour % 12, minutes=minute),
        base + timedelta(hours=hour % 12 + 12, minutes=minute),
    ]
    if near is not None:
        return min(options, key=lambda o: abs(o - near))
    if after is not None:
        later = [o for o in options if o > after]
        if later:
            return min(later)
        return options[1] + timedelta(days=1) if options[1] <= after else options[1]
    past = [o for o in options if o <= day]
    return max(past) if past else options[0]


# ---- records --------------------------------------------------------------


@dataclass
class Expense:
    ref: str
    category: str
    amount: Decimal
    vendor: str | None = None
    city: str | None = None
    gallons: Decimal | None = None
    payment_method: str | None = None
    reimbursable: bool = False
    status: str = "pending"  # pending | confirmed | discarded

    def readback(self) -> str:
        what = CATEGORY_WORDS[self.category]
        where = ""
        if self.vendor and self.city:
            where = f" at {self.vendor} in {self.city}"
        elif self.vendor:
            where = f" at {self.vendor}"
        elif self.city:
            where = f" in {self.city}"
        gallons = (
            f", {quantity_words(self.gallons)} gallons"
            if self.gallons is not None
            else ""
        )
        paid = {
            "cash": ", paid cash",
            "card": ", on the card",
            "fuel_card": ", on the fuel card",
        }
        method = paid.get(self.payment_method or "", "")
        billed = ", billed back to the broker" if self.reimbursable else ""
        line = f"{what}{where}{gallons}, {money_words(self.amount)}{method}{billed}."
        return line[0].upper() + line[1:]


@dataclass
class Stop:
    load_number: str
    kind: str  # pickup | delivery
    facility: str
    city: str
    appointment: datetime
    eta: datetime | None = None  # set when he's running late
    arrived: datetime | None = None
    departed: datetime | None = None
    claim_drafted: bool = False


@dataclass
class Load:
    number: str
    broker: str
    pay: Decimal
    free_minutes: int
    detention_rate: Decimal  # per hour, billed in quarter hours
    status: str  # in_transit | scheduled | cancelled
    # Drive time from the previous stop on the schedule to this load's pickup.
    deadhead_minutes: int
    stops: dict[str, Stop]


@dataclass
class Event:
    """Something on the schedule that isn't a load: service, home time."""

    what: str
    place: str
    start: datetime


@dataclass
class Change:
    """A schedule change or problem he reported. Applied only after his yes."""

    ref: str
    load_number: str | None
    kind: str  # running_late | appointment_moved | cancelled | issue
    note: str
    stop_kind: str | None = None
    new_time: datetime | None = None
    status: str = "pending"  # pending | confirmed | discarded
    readback_text: str = ""

    def readback(self) -> str:
        return self.readback_text


def day_words(t: datetime, now: datetime) -> str:
    days = (t.date() - now.date()).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days, f"{t:%A}")


@dataclass
class Books:
    driver: str
    business: str
    truck: str
    loads: list[Load]
    events: list[Event]
    now: datetime
    expenses: list[Expense] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)

    @property
    def load(self) -> Load:
        """The load he's on now: in transit, else the next scheduled one."""
        for status in ("in_transit", "scheduled"):
            for load in self.loads:
                if load.status == status:
                    return load
        raise LedgerError("You don't have a load on the books right now.")

    def find_load(self, number: str | None) -> Load:
        if not number:
            return self.load
        digits = "".join(ch for ch in number if ch.isdigit())
        for load in self.loads:
            if load.number == digits:
                return load
        raise LedgerError(
            f"I don't have a load {digit_words(digits) if digits else number}. Which load?"
        )

    # -- expenses --

    def _expense(self, ref: str) -> Expense:
        for e in self.expenses:
            if e.ref.lower() == ref.strip().lower() and e.status != "discarded":
                return e
        raise LedgerError("I can't find that one. Which expense do you mean?")

    def log_expense(
        self,
        category: str,
        amount: str,
        vendor: str | None = None,
        city: str | None = None,
        gallons: str | None = None,
        payment_method: str | None = None,
        reimbursable: bool | None = None,
    ) -> Expense:
        if category not in CATEGORIES:
            category = "other"
        expense = Expense(
            ref=f"E{len(self.expenses) + 1}",
            category=category,
            amount=parse_amount(amount),
            vendor=_vendor(vendor, category),
            city=city or None,
            gallons=parse_quantity(gallons) if gallons else None,
            payment_method=payment_method,
            reimbursable=(
                category in REIMBURSABLE_BY_DEFAULT
                if reimbursable is None
                else reimbursable
            ),
        )
        self.expenses.append(expense)
        return expense

    def correct_expense(self, ref: str, **changes: str | bool | None) -> Expense:
        e = self._expense(ref)
        if e.status == "confirmed":
            # Reopen it: a corrected record needs a fresh yes.
            e.status = "pending"
        if changes.get("category") in CATEGORIES:
            e.category = str(changes["category"])
        if changes.get("amount"):
            e.amount = parse_amount(str(changes["amount"]))
        if changes.get("gallons"):
            e.gallons = parse_quantity(str(changes["gallons"]))
        for name in ("vendor", "city", "payment_method"):
            if changes.get(name):
                setattr(e, name, changes[name])
        if changes.get("reimbursable") is not None:
            e.reimbursable = bool(changes["reimbursable"])
        return e

    def discard_expense(self, ref: str) -> Expense:
        e = self._expense(ref)
        e.status = "discarded"
        return e

    def _record(self, ref: str) -> Expense | Change:
        if ref.strip().upper().startswith("S"):
            for c in self.changes:
                if c.ref.lower() == ref.strip().lower() and c.status != "discarded":
                    return c
            raise LedgerError("I can't find that change. Which one do you mean?")
        return self._expense(ref)

    def confirm(self, refs: list[str]) -> list[Expense | Change]:
        found = [self._record(r) for r in refs]
        for record in found:
            if isinstance(record, Change) and record.status == "pending":
                self._apply(record)
            record.status = "confirmed"
        return found

    def pending(self) -> list[Expense]:
        return [e for e in self.expenses if e.status == "pending"]

    # -- stops and detention --

    def stop(self, name: str, load_number: str | None = None) -> Stop:
        key = name.strip().lower()
        loads = (
            [self.find_load(load_number)] if load_number else [self.load, *self.loads]
        )
        for load in loads:
            for kind, s in load.stops.items():
                if key == kind and load is loads[0]:
                    return s
                if key in s.facility.lower() or s.facility.lower() in key:
                    return s
        raise LedgerError("Was that the pickup or the delivery?")

    def _load_of(self, s: Stop) -> Load:
        return self.find_load(s.load_number)

    def log_stop_times(
        self,
        stop: str,
        arrived_at: str | None,
        departed_at: str | None,
        load_number: str | None = None,
    ) -> Stop:
        s = self.stop(stop, load_number)
        if not arrived_at and not departed_at:
            raise LedgerError("What time did you get there?")
        if arrived_at:
            s.arrived = parse_time(arrived_at, self.now)
        if departed_at:
            if s.arrived is None:
                raise LedgerError("What time did you get there?")
            s.departed = parse_time(departed_at, self.now, after=s.arrived)
        if s.arrived and s.departed and s.departed <= s.arrived:
            s.departed = None
            raise LedgerError("You left before you got there? What time did you leave?")
        return s

    def stop_readback(self, s: Stop) -> str:
        parts = [f"At {s.facility}"]
        if s.arrived:
            parts.append(f"in at {time_words(s.arrived)}")
        if s.departed:
            parts.append(f"out at {time_words(s.departed)}")
        return ", ".join(parts) + "."

    def detention(
        self, stop: str, load_number: str | None = None
    ) -> tuple[Stop, int, Decimal]:
        """(stop, billable minutes, amount). Clock starts at the later of arrival
        and appointment; billed in quarter hours after free time."""
        s = self.stop(stop, load_number)
        load = self._load_of(s)
        if not (s.arrived and s.departed):
            raise LedgerError(
                "I need both times for that stop. When did you get in and when did you leave?"
            )
        start = max(s.arrived, s.appointment)
        waited = int((s.departed - start).total_seconds() // 60)
        billable = max(0, waited - load.free_minutes) // 15 * 15
        amount = (load.detention_rate * billable / 60).quantize(Decimal("0.01"))
        return s, billable, amount

    def detention_summary(self, stop: str, load_number: str | None = None) -> str:
        s, billable, amount = self.detention(stop, load_number)
        load = self._load_of(s)
        if billable == 0:
            return (
                f"No detention at {s.facility}. You were inside the "
                f"{duration_words(load.free_minutes)} of free time."
            )
        late = (
            " The clock starts at your appointment time."
            if s.arrived < s.appointment
            else ""
        )
        return (
            f"That's {duration_words(billable)} of detention at {s.facility}, "
            f"{money_words(amount)} from {load.broker}.{late}"
        )

    # -- schedule --

    def _when(self, t: datetime) -> str:
        return f"{day_words(t, self.now)} at {time_words(t)}"

    def schedule(self, days: int = 3) -> list[tuple[datetime, str, bool]]:
        """(time, spoken line, done) for every stop and event from today through
        `days`. Done: behind him (left the stop, picked up, or already happened)."""
        start = self.now.replace(hour=0, minute=0)
        end = start + timedelta(days=days + 1)
        items: list[tuple[datetime, str, bool]] = []
        for load in self.loads:
            for s in load.stops.values():
                if not (start <= s.appointment < end):
                    continue
                line = (
                    f"load {digit_words(load.number)} {s.kind} at {s.facility} "
                    f"in {s.city.split(',')[0]}, {self._when(s.appointment)}"
                )
                if load.status == "cancelled":
                    line += ", cancelled"
                elif s.departed:
                    line += ", done"
                elif s.eta:
                    line += f", you're expected {self._when(s.eta)}"
                done = bool(s.departed) or (
                    load.status == "in_transit" and s.kind == "pickup"
                )
                items.append((s.appointment, line, done))
        for e in self.events:
            if start <= e.start < end:
                line = f"{e.what} at {e.place}, {self._when(e.start)}"
                items.append((e.start, line, e.start < self.now))
        return sorted(items)

    def schedule_summary(self, date: str | None = None) -> str:
        items = self.schedule()
        if date:
            try:
                day = datetime.fromisoformat(date).date()
            except ValueError as err:
                raise LedgerError("Which day do you mean?") from err
            items = [i for i in items if i[0].date() == day]
            if not items:
                return "Nothing on the schedule that day."
        else:
            items = [i for i in items if not i[2]] or items
        more = ""
        if not date and len(items) > SCHEDULE_PREVIEW:
            more = (
                f" Then {number_words(len(items) - SCHEDULE_PREVIEW)} more after that."
                " Want the rest?"
            )
            items = items[:SCHEDULE_PREVIEW]
        lines = " ".join(line[0].upper() + line[1:] + "." for _, line, _ in items)
        return lines + more

    def full_schedule(self) -> str:
        items = [i for i in self.schedule() if not i[2]]
        return " ".join(line[0].upper() + line[1:] + "." for _, line, _ in items)

    def _next_pickup_after(
        self, t: datetime, load_number: str
    ) -> tuple[Load, Stop] | None:
        later = [
            (load, load.stops["pickup"])
            for load in self.loads
            if load.number != load_number
            and load.status != "cancelled"
            and load.stops["pickup"].appointment > t - timedelta(hours=12)
        ]
        return min(later, key=lambda ls: ls[1].appointment, default=None)

    def _ready_for(self, load: Load, after_delivery: datetime) -> datetime:
        # An hour to unload, then the drive to this load's pickup.
        return after_delivery + timedelta(
            minutes=UNLOAD_MINUTES + load.deadhead_minutes
        )

    def _knock_on(self, s: Stop, new_time: datetime) -> str:
        """Whether a new time at `s` still fits the stop before or after it."""
        if s.kind == "delivery":
            nxt = self._next_pickup_after(s.appointment, s.load_number)
            if not nxt:
                return ""
            load, pickup = nxt
            ready = self._ready_for(load, new_time)
            if ready <= pickup.appointment:
                return (
                    f" That still leaves time for your {time_words(pickup.appointment)} "
                    f"pickup at {pickup.facility}."
                )
            return (
                f" Heads up, that puts your {time_words(pickup.appointment)} pickup at "
                f"{pickup.facility} at risk. With unloading and the drive, you'd get "
                f"there around {time_words(ready)}."
            )
        # A moved pickup: check it against the delivery before it.
        load = self.find_load(s.load_number)
        before = [
            other.stops["delivery"]
            for other in self.loads
            if other is not load
            and other.status != "cancelled"
            and other.stops["delivery"].appointment < s.appointment
        ]
        if not before:
            return ""
        prev = max(before, key=lambda d: d.appointment)
        ready = self._ready_for(load, prev.eta or prev.appointment)
        if ready <= new_time:
            return f" That works with your {prev.facility} delivery."
        return (
            f" Heads up, after your {prev.facility} delivery you'd get there around "
            f"{time_words(ready)}, so that's still tight."
        )

    def report_change(
        self,
        kind: str,
        load_number: str | None = None,
        stop: str | None = None,
        new_time: str | None = None,
        new_date: str | None = None,
        note: str = "",
    ) -> Change:
        ref = f"S{len(self.changes) + 1}"
        note = note.strip()
        reason = f", because of {note}" if note else ""
        if kind == "issue":
            load = self.find_load(load_number) if load_number else None
            on = f" on load {digit_words(load.number)}" if load else ""
            if not note:
                raise LedgerError("What's going on?")
            change = Change(ref, load.number if load else None, kind, note)
            change.readback_text = f"Noting{on}: {note}."
        elif kind == "cancelled":
            load = self.find_load(load_number)
            change = Change(ref, load.number, kind, note)
            change.readback_text = f"Load {digit_words(load.number)} for {load.broker} is cancelled{reason}."
        elif kind in ("running_late", "appointment_moved"):
            if not new_time:
                raise LedgerError("What time are we looking at now?")
            s = self.stop(stop or "delivery", load_number)
            base = s.appointment.replace(hour=0, minute=0)
            if new_date:
                try:
                    base = datetime.fromisoformat(new_date).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                except ValueError as err:
                    raise LedgerError("Which day is that?") from err
            if kind == "running_late" and not new_date:
                # Late means later than he was expected.
                when = parse_time(new_time, base, after=s.eta or s.appointment)
            else:
                when = parse_time(new_time, base, near=base + _clock_of(s.appointment))
            change = Change(ref, s.load_number, kind, note, s.kind, when)
            what = f"Load {digit_words(s.load_number)} {s.kind} at {s.facility}"
            if kind == "running_late":
                change.readback_text = (
                    f"{what}: you're now expected {self._when(when)} instead of "
                    f"{self._when(s.eta or s.appointment)}{reason}."
                    + self._knock_on(s, when)
                )
            else:
                change.readback_text = (
                    f"{what} moved from {self._when(s.appointment)} to "
                    f"{self._when(when)}{reason}." + self._knock_on(s, when)
                )
        else:
            raise LedgerError("Is that a delay, a moved appointment, or a problem?")
        self.changes.append(change)
        return change

    def _apply(self, c: Change) -> None:
        load = self.find_load(c.load_number) if c.load_number else None
        if c.kind == "cancelled" and load:
            load.status = "cancelled"
        elif c.kind == "running_late" and load and c.stop_kind:
            load.stops[c.stop_kind].eta = c.new_time
        elif c.kind == "appointment_moved" and load and c.stop_kind:
            load.stops[c.stop_kind].appointment = c.new_time  # type: ignore[assignment]

    # -- money questions --

    def load_summary(self) -> str:
        load = self.load
        costs = sum(
            (
                e.amount
                for e in self.expenses
                if e.status == "confirmed" and not e.reimbursable
            ),
            Decimal(0),
        )
        billed_back = sum(
            (
                e.amount
                for e in self.expenses
                if e.status == "confirmed" and e.reimbursable
            ),
            Decimal(0),
        )
        claims = sum(
            (
                self.detention(k, load.number)[2]
                for k, s in load.stops.items()
                if s.claim_drafted
            ),
            Decimal(0),
        )
        net = load.pay + claims - costs
        line = (
            f"Load {digit_words(load.number)} pays {money_words(load.pay)}"
            f"{f' plus {money_words(claims)} in detention' if claims else ''}. "
            f"Confirmed costs so far are {money_words(costs)}, "
            f"so you're netting {money_words(net)}."
        )
        if billed_back:
            line += f" Another {money_words(billed_back)} gets billed back to {load.broker}."
        if self.pending():
            line += " That doesn't count anything you haven't confirmed yet."
        return line


# ---- seed -----------------------------------------------------------------

SEED_FILE = Path(__file__).resolve().parent.parent / "seed.yaml"


def _clock_of(t: datetime) -> timedelta:
    return timedelta(hours=t.hour, minutes=t.minute)


def _clock(value: str | int) -> timedelta:
    """'07:00' -> 7 hours. YAML reads an unquoted 07:00 as 420 (minutes)."""
    if isinstance(value, int):
        return timedelta(minutes=value)
    hours, _, minutes = str(value).partition(":")
    return timedelta(hours=int(hours), minutes=int(minutes or 0))


def load_books(now: datetime | None = None, path: Path = SEED_FILE) -> Books:
    """Build a fresh ledger from seed.yaml. Days in the file count from today."""
    now = (now or datetime.now()).replace(second=0, microsecond=0)
    today = now.replace(hour=0, minute=0)
    seed = yaml.safe_load(path.read_text())

    def at(item: dict) -> datetime:
        return today + timedelta(days=int(item["day"])) + _clock(item["time"])

    loads = []
    for raw in seed["loads"]:
        number = str(raw["number"])
        loads.append(
            Load(
                number=number,
                broker=raw["broker"],
                pay=Decimal(str(raw["pay"])),
                free_minutes=int(raw.get("free_time_minutes", 120)),
                detention_rate=Decimal(str(raw.get("detention_per_hour", 50))),
                status=raw.get("status", "scheduled"),
                deadhead_minutes=int(raw.get("deadhead_minutes", 0)),
                stops={
                    kind: Stop(
                        number,
                        kind,
                        stop["facility"],
                        stop["city"],
                        appointment=at(stop),
                    )
                    for kind in ("pickup", "delivery")
                    for stop in [raw[kind]]
                },
            )
        )
    events = [Event(e["what"], e["place"], at(e)) for e in seed.get("events", [])]
    return Books(
        driver=seed["driver"],
        business=seed["business"],
        truck=seed["truck"],
        loads=loads,
        events=events,
        now=now,
    )
