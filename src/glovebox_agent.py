"""Glovebox: a voice bookkeeper a truck driver calls from the road.

One job: keep the driver's books straight from the cab. Capture what he spent,
when he waited, and what changed on his schedule; read it back, and only
record it after a clear yes. Detention gets flagged as money owed, and a late
delivery gets checked against the next pickup.

The data comes from seed.yaml. All numbers and wording come from ledger.py;
the LLM never does math.
"""

import logging
from typing import Literal

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.beta.tools import EndCallTool

from ledger import Books, LedgerError, digit_words, load_books
from prompts import build_instructions

logger = logging.getLogger("glovebox-agent")

Category = Literal[
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
]
PaymentMethod = Literal["card", "cash", "fuel_card"]
ChangeKind = Literal["running_late", "appointment_moved", "cancelled", "issue"]


def job_prompt(books: Books) -> str:
    load = books.load
    stops = "; ".join(
        f"{s.kind} at {s.facility} in {s.city}, appointment {s.appointment:%-I:%M %p}"
        for s in load.stops.values()
    )
    upcoming = "\n".join(
        f"- {t:%A %Y-%m-%d %-I:%M %p}: {line}" for t, line, _ in books.schedule()
    )
    return f"""
You are Glovebox, an automated bookkeeping assistant for {books.business}, a
one-truck trucking business. You are on the phone with {books.driver}, the owner
and driver. He is usually driving, so keep it short.

His current load is {load.number} for {load.broker}: {stops}. It pays a flat
rate. Detention: {load.free_minutes // 60} hours free, then billed by the quarter hour.

His schedule for the next three days (for knowing which load or stop he
means; for answers, use get_schedule):
{upcoming}

Your job on every call: log what he tells you, read it back, and record it
only after he says yes.

1. Expenses (fuel, DEF, tolls, scale fees, parking, lumper, repairs, washout,
   food): call log_expense once per expense, passing amounts as digits like
   "512.40". If he mentions several at once, log all of them first, then read
   them all back together in one reply.
2. Read back using each tool result's readback text word for word, then ask
   "Is that right?" and nothing else.
3. Any clear yes ("yes", "yep", "yup", "that's right", "ten-four") means call
   confirm_records with the refs you read back. A correction is not a yes: use
   correct_expense on the same ref, read back the new readback, and ask again.
   Never log a second expense to fix the first.
4. Stop times: call log_stop_times. When he has both times for a stop, call
   check_detention and say its result. Only if he wants it, call
   create_detention_claim.
5. "How's this load doing?" means get_load_summary; say its result.
6. Schedule questions ("what's tomorrow look like?") mean get_schedule.
7. Schedule changes and problems mean report_schedule_change: running late,
   an appointment moved by the shipper or receiver, a load cancelled, or an
   issue like a breakdown or a reefer alarm. Read back its readback, including
   any heads-up about the next load, then ask "Is that right?" and confirm
   with confirm_records after a yes, like expenses. Acknowledge how he feels in
   a few words if he's frustrated, but don't promise anyone will be told.
8. When he's done, ask "Anything else?" once. If he paid cash, remind him to
   snap the receipt when he's parked. Then say goodbye and end the call.

Rules:
- Every number you say comes from a tool result. Never add, subtract or
  estimate money or time yourself.
- Never ask him to read, look at or type anything while driving.
- Refs like E1 or S1 are for your tools only. Never say them out loud.
- Never record times or amounts he says are made up or padded, and never
  inflate a detention claim. Say you can only log what really happened.
- You don't do hours-of-service logs, taxes, legal advice, directions or
  dispatch. Say so in one sentence and offer what you can do.
- If he asks for a person, call transfer_to_human.
"""


class GloveboxAgent(Agent):
    def __init__(self, books: Books | None = None) -> None:
        self.books = books or load_books()
        super().__init__(
            instructions=build_instructions(job_prompt(self.books)),
            tools=[EndCallTool(ignore_on_enter=True)],
        )

    async def on_enter(self) -> None:
        load = self.books.load
        city = load.stops["delivery"].city.split(",")[0]
        # Fixed greeting: no LLM round trip before the first word.
        self.session.say(
            f"Hey {self.books.driver}, it's Glovebox, your automated bookkeeper. "
            f"You're on load {digit_words(load.number)} to {city}. What've you got?"
        )

    @function_tool
    async def log_expense(
        self,
        context: RunContext,
        category: Category,
        amount: str,
        vendor: str | None = None,
        city: str | None = None,
        gallons: str | None = None,
        payment_method: PaymentMethod | None = None,
        reimbursable: bool | None = None,
    ) -> str:
        """Log one expense as pending. Call once per expense he actually
        mentioned, with only the details he said, then read back.

        Args:
            category: What it was for. A fee for using a truck scale is scale_fee.
            amount: Dollars as digits, like "512.40" or "85".
            vendor: The business name, like "Love's", only if he said one.
            city: City, if said.
            gallons: Gallons for fuel or DEF as digits, if said.
            payment_method: How it was paid, if said.
            reimbursable: Only if he says whether it's billed back to the broker.
        """
        try:
            e = self.books.log_expense(
                category, amount, vendor, city, gallons, payment_method, reimbursable
            )
        except LedgerError as err:
            return str(err)
        logger.info("log_expense %s %s %s", e.ref, e.category, e.amount)
        return f"ref {e.ref}, pending. readback: {e.readback()}"

    @function_tool
    async def correct_expense(
        self,
        context: RunContext,
        ref: str,
        category: Category | None = None,
        amount: str | None = None,
        vendor: str | None = None,
        city: str | None = None,
        gallons: str | None = None,
        payment_method: PaymentMethod | None = None,
        reimbursable: bool | None = None,
    ) -> str:
        """Fix an expense from this call. Pass only what changes, then read back.

        Args:
            ref: The expense ref, like "E1".
        """
        try:
            e = self.books.correct_expense(
                ref,
                category=category,
                amount=amount,
                vendor=vendor,
                city=city,
                gallons=gallons,
                payment_method=payment_method,
                reimbursable=reimbursable,
            )
        except LedgerError as err:
            return str(err)
        logger.info("correct_expense %s %s", e.ref, e.amount)
        return f"ref {e.ref}, pending again. readback: {e.readback()}"

    @function_tool
    async def discard_expense(self, context: RunContext, ref: str) -> str:
        """Remove an expense from this call when he says it shouldn't be there.

        Args:
            ref: The expense ref, like "E1".
        """
        try:
            e = self.books.discard_expense(ref)
        except LedgerError as err:
            return str(err)
        logger.info("discard_expense %s", e.ref)
        return "Removed. Say: Got it, that one's off the books."

    @function_tool
    async def confirm_records(self, context: RunContext, refs: list[str]) -> str:
        """Record expenses after reading them back and hearing a clear yes.

        Args:
            refs: The refs he just confirmed, like ["E1", "E2"].
        """
        try:
            done = self.books.confirm(refs)
        except LedgerError as err:
            return str(err)
        logger.info("confirm_records %s", [r.ref for r in done])
        receipt = (
            " He paid cash: before the call ends, remind him to snap the receipt "
            "when he's parked."
            if any(getattr(r, "payment_method", None) == "cash" for r in done)
            else ""
        )
        return f"Recorded. Say: Got it, that's on the books.{receipt}"

    @function_tool
    async def log_stop_times(
        self,
        context: RunContext,
        stop: str,
        arrived_at: str | None = None,
        departed_at: str | None = None,
    ) -> str:
        """Record when he got to and/or left a stop.

        Args:
            stop: "pickup", "delivery", or the facility name.
            arrived_at: Arrival time as said, like "7:50" or "7:50 AM".
            departed_at: Departure time as said, like "11:05".
        """
        try:
            s = self.books.log_stop_times(stop, arrived_at, departed_at)
        except LedgerError as err:
            return str(err)
        logger.info("log_stop_times %s %s %s", s.kind, s.arrived, s.departed)
        ready = " Both times are in: call check_detention." if s.departed else ""
        return f"Saved. readback: {self.books.stop_readback(s)}{ready}"

    @function_tool
    async def check_detention(self, context: RunContext, stop: str) -> str:
        """Check whether the wait at a stop is billable detention.

        Args:
            stop: "pickup", "delivery", or the facility name.
        """
        try:
            summary = self.books.detention_summary(stop)
            _, minutes, _ = self.books.detention(stop)
        except LedgerError as err:
            return str(err)
        ask = " Then ask if he wants a detention claim drafted." if minutes else ""
        return f"Say: {summary}{ask}"

    @function_tool
    async def create_detention_claim(self, context: RunContext, stop: str) -> str:
        """Draft a detention claim for a stop, only after he says he wants one.

        Args:
            stop: "pickup", "delivery", or the facility name.
        """
        try:
            s, minutes, _ = self.books.detention(stop)
        except LedgerError as err:
            return str(err)
        if not minutes:
            return "No detention at that stop, so there's nothing to claim."
        s.claim_drafted = True
        logger.info("create_detention_claim %s %s min", s.kind, minutes)
        return (
            f"Say: Done, the claim to {self.books.load.broker} is drafted with your "
            "in and out times. It goes out with the invoice."
        )

    @function_tool
    async def get_schedule(self, context: RunContext, date: str | None = None) -> str:
        """His loads, stops and appointments, including any changes from this call.

        Args:
            date: One day as an ISO date (YYYY-MM-DD), converting words like
                "tomorrow" or "Sunday" using today's date. Only when he names a
                day; leave it out for "what's coming up" or "what's my schedule".
        """
        try:
            return f"Say this naturally, briefly: {self.books.schedule_summary(date)}"
        except LedgerError as err:
            return str(err)

    @function_tool
    async def get_full_schedule(self, context: RunContext) -> str:
        """Everything left on the next three days, when he asks for the rest."""
        return f"Say this naturally: {self.books.full_schedule()}"

    @function_tool
    async def report_schedule_change(
        self,
        context: RunContext,
        kind: ChangeKind,
        load_number: str | None = None,
        stop: str | None = None,
        new_time: str | None = None,
        new_date: str | None = None,
        note: str | None = None,
    ) -> str:
        """A change or problem on his schedule. Pending until he confirms the readback.

        Args:
            kind: running_late (his new ETA), appointment_moved (the shipper or
                receiver changed the time), cancelled (a whole load), or issue
                (anything else: breakdown, reefer alarm, road closed).
            load_number: The load, like "4482". Leave it out for the current load.
            stop: "pickup", "delivery", or the facility name.
            new_time: The new time as said, like "7" or "6:30 PM".
            new_date: Only if he names a different day: ISO date (YYYY-MM-DD).
            note: The reason or problem in two to five words, like "a reefer
                alarm", "traffic on I-44" or "the receiver's request". Not who
                called or the new time.
        """
        try:
            c = self.books.report_change(
                kind, load_number, stop, new_time, new_date, note or ""
            )
        except LedgerError as err:
            return str(err)
        logger.info("report_schedule_change %s %s %s", c.ref, c.kind, c.new_time)
        return f"ref {c.ref}, pending. readback: {c.readback()}"

    @function_tool
    async def get_load_summary(self, context: RunContext) -> str:
        """How the current load is doing: pay, confirmed costs, detention and net."""
        return f"Say: {self.books.load_summary()}"

    @function_tool
    async def transfer_to_human(self, context: RunContext) -> str:
        """He asked for a person. There's no one to transfer to; offer a callback."""
        logger.info("transfer_to_human")
        return (
            "Say: I can't transfer you, but I'll flag this so your bookkeeper calls "
            "you back today. Then ask if there's anything else."
        )
