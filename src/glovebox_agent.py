"""Glovebox: a voice bookkeeper a truck driver calls from the road.

One job: keep the driver's books straight from the cab. Capture what he spent,
when he waited, and what changed on his schedule; read it back, and only
record it after a clear yes. Detention gets flagged as money owed, and a late
delivery gets checked against the next pickup.

The data comes from seed.yaml. All numbers and wording come from ledger.py;
the LLM never does math.
"""

import logging
import os
from typing import Literal

from livekit.agents import Agent, RunContext, function_tool
from livekit.agents.beta.tools import EndCallTool

from ledger import (
    Books,
    Change,
    Flag,
    LedgerError,
    digit_words,
    driver_for_phone,
    driver_keys,
    load_books,
)
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
FlagKind = Literal["road_closure", "breakdown", "detention", "emergency", "other"]
EmergencyNeed = Literal["fire", "ambulance", "police", "tow", "repair"]

# What dispatch does once he confirms a report: said so he knows not to call back.
DISPATCH_NEXT = {
    "road_closure": (
        "Dispatch has it flagged. They'll look at a quicker route and update the "
        "receiver, and they'll get back to you with it, so you don't need to call in."
    ),
    "breakdown": (
        "Dispatch has it flagged and has started roadside coordination. They'll "
        "call you with who's coming."
    ),
    "detention": (
        "Dispatch has it flagged. They'll chase the paperwork with the receiver, "
        "and your detention clock is on record."
    ),
    "other": "Dispatch has it flagged and will follow up with you.",
}


# Trucking words STT tends to miss. Names from seed.yaml are added on top.
TRUCKING_TERMS = [
    "Glovebox",
    "Love's",
    "Pilot",
    "Flying J",
    "Petro",
    "DEF",
    "lumper",
    "washout",
    "reefer",
    "scale ticket",
    "detention",
    "deadhead",
]


def stt_keyterms(books: Books | None = None) -> list[str]:
    """Words to bias STT toward: trucking terms plus every name in the seed.

    Built before we know who's calling, so it covers every driver. It only
    helps the transcriber spell names; the agent never sees this list.
    """
    every = [books] if books else [load_books(driver=k) for k in driver_keys()]
    names: set[str] = set()
    for b in every:
        names.update({b.driver, b.business})
        for load in b.loads:
            names.add(load.broker)
            for s in load.stops.values():
                names.add(s.facility)
                names.add(s.city.split(",")[0])
        names.update(e.place.split(" in ")[0] for e in b.events)
    # Proper names only: an event place like "home in Joplin" isn't a keyterm.
    proper = {n for n in names if n[:1].isupper() and n not in TRUCKING_TERMS}
    return TRUCKING_TERMS + sorted(proper)


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
You are Glovebox, the automated line {books.driver} calls from the cab. He's a
truck driver for {books.business}, driving {books.truck}, and he calls about his
truck, his route and problems on the road. You flag problems to dispatch and
log his receipts. He is usually driving: keep every reply to one or two short
sentences and ask one short question at a time.

You only know about {books.driver}. If he asks about another driver, another
truck, or loads that aren't his, say that's private and you can only help with
his own.

His current load is {load.number} for {load.broker}: {stops}.
Detention: {load.free_minutes // 60} hours free, then counted by the quarter hour.

His schedule (to know which load or stop he means; for answers use the tools):
{upcoming}

# Safety comes first
If he mentions smoke, fire, a crash or collision, injuries, or being stuck in a
live lane: before anything else, ask if he's safe and tell him to get clear of
the truck and call nine one one if there's fire or anyone is hurt. Be calm and
kind. Then call flag_dispatch with kind "emergency" (it saves immediately), and
say its result. Do not ask about paperwork, loads or money until he's safe.

# Flagging problems to dispatch (flag_dispatch)
- Road closure: get direction, highway, mile marker, rough delay, and what he
  wants (a quicker route approved, the receiver told). Ask only for what he
  hasn't said, one short question at a time; if he doesn't know, leave it out.
- Breakdown: never ask again for anything he already said (location, truck).
  Check he's safe and off the road, and that the truck can't move. Don't
  diagnose the problem and don't promise a tow company or an arrival time.
- Detention at a receiver: get when he got there, which facility, the reason
  they gave, and what he wants dispatch to do.
- Read back the result's readback, ask "Is that right?", and after his yes
  call confirm_records. Then say what dispatch is doing, from that result.
- You can't reach dispatch live or transfer him. Never promise a time or a
  specific person.

# Delays and the schedule (report_schedule_change, get_schedule)
- Running late, a moved appointment, a cancelled load: report_schedule_change.
  If he gives a delay in minutes ("forty-five minutes behind"), pass
  delay_minutes; never ask him to work out the new time. Read back, including
  any heads-up about the next load, then confirm after his yes.
- He already knows his week. "What's my schedule" or "anything change?" means
  get_schedule: what changed, what's next, and dispatch notes. Only give the
  whole week (get_full_schedule) if he asks for everything. Say times as he'd
  say them, and skip addresses and long number strings.
- A question about one stop means get_stop_details.

# Receipts
- Expenses: log_expense once per expense, amounts as digits like "512.40". Log
  several at once, then read them all back together.
- Stop times: log_stop_times. They save right away, so don't ask "Is that
  right?" for them. With both times in, call check_detention and say its result;
  send it to dispatch (create_detention_claim) only if he wants that.
- Load pay, rates and profit are for the office, not this line. Say so in one
  sentence.

# Confirming
- Read back each tool's readback word for word, then ask "Is that right?" and
  nothing else. Nothing is saved until he says yes, so never say "logged",
  "saved", "flagged" or "on the books" before that (emergencies excepted).
- Any clear yes ("yes", "yep", "yup", "that's right", "ten-four") means
  confirm_records with the refs you read back. A correction is not a yes: fix
  the same record and read it back again.

# Ending
- After a task, ask "Anything else?" once. Once he's done ("that's it", "later",
  "bye"), don't ask again. If something is still waiting on his yes, say it isn't
  saved. If he paid cash, remind him to snap the receipt when he's parked. Then
  call end_call and say goodbye once, after it.

# Rules
- Every number you say comes from a tool result. Never do math on money or time.
- Never ask him to read, look at, or type anything. If he doesn't know a detail,
  that's fine: leave it out.
- Refs like E1, S1 or D1 are for your tools only. Never say them out loud.
- Never record times or amounts he says are made up or padded.
- You don't do hours-of-service logs, taxes, legal or medical advice, or engine
  diagnosis. Say so in one sentence and offer what you can do.
- If he asks for a person, call transfer_to_human.
"""


class GloveboxAgent(Agent):
    def __init__(self, books: Books | None = None) -> None:
        self.books = books or load_books()
        super().__init__(
            instructions=build_instructions(
                job_prompt(self.books), today=self.books.now.date()
            ),
            tools=[
                EndCallTool(
                    ignore_on_enter=True,
                    end_instructions="Say one short goodbye.",
                )
            ],
        )

    async def on_enter(self) -> None:
        load = self.books.load
        city = load.stops["delivery"].city.split(",")[0]
        # Fixed greeting: no LLM round trip before the first word.
        self.session.say(
            f"Hey {self.books.driver}, it's Glovebox, your automated driver line. "
            f"You're on load {digit_words(load.number)} to {city}. What've you got?"
        )

    def filler_line(self, heard: str) -> str:
        """A short holding line while the model thinks, matched to what he said."""
        said = heard.lower()
        if any(w in said for w in ("smoke", "fire", "crash", "hurt", "accident")):
            return "I'm with you."
        if any(
            w in said for w in ("schedule", "tomorrow", "coming up", "next", "week")
        ):
            return "Let me check your schedule."
        if any(
            w in said for w in ("late", "moved", "pushed", "cancel", "alarm", "broke")
        ):
            return "Okay, one sec."
        if any(w in said for w in ("doing", "net", "making", "profit")):
            return "Pulling up the numbers."
        if any(ch.isdigit() for ch in said) or "$" in said:
            return "Got it, one sec."
        return "One sec."

    def silence_line(self, check_in: int, last: bool) -> str:
        """What to say when he's gone quiet. Fixed text: no LLM round trip."""
        if last:
            return (
                "I'll let you go. I haven't saved anything you didn't confirm. "
                "Call back anytime."
            )
        if check_in == 1:
            return f"Still there, {self.books.driver}? No rush if you're driving."
        if self.books.has_pending():
            return "I've still got that waiting on your yes. Want me to save it?"
        return "You still with me?"

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
        """Save records after reading them back and hearing a clear yes.

        Args:
            refs: The refs he just confirmed: expenses (E1), schedule changes
                (S1) or dispatch reports (D1), like ["E1", "S1"].
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
        said = []
        if any(isinstance(r, Flag) for r in done):
            said += [
                DISPATCH_NEXT.get(r.kind, DISPATCH_NEXT["other"])
                for r in done
                if isinstance(r, Flag)
            ]
        if any(isinstance(r, Change) for r in done):
            said.append("Got it, dispatch has the update.")
        if not said:
            said.append("Got it, that's on the books.")
        return f"Recorded. Say: {' '.join(said)}{receipt}"

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
        return (
            "Saved now; stop times don't wait for a yes, he can correct them. "
            f"readback: {self.books.stop_readback(s)}{ready}"
        )

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
            "Say: Done, dispatch has your in and out times to bill detention to "
            f"{self.books.load.broker}."
        )

    @function_tool
    async def get_schedule(self, context: RunContext, date: str | None = None) -> str:
        """What changed, what's next, and dispatch notes. He already knows his week.

        Args:
            date: Only when he names a day: ISO date (YYYY-MM-DD), converting
                "tomorrow" or "Sunday" using today's date. Leave it out for
                "what's my schedule", "anything change?" or "what's next".
        """
        try:
            text = (
                self.books.schedule_summary(date)
                if date
                else self.books.schedule_update()
            )
        except LedgerError as err:
            return str(err)
        return f"Say this naturally and briefly, the way you'd tell a driver: {text}"

    @function_tool
    async def get_stop_details(
        self, context: RunContext, stop: str, load_number: str | None = None
    ) -> str:
        """One stop: which load, when, and dispatch's notes for it.

        Args:
            stop: "pickup", "delivery", or the facility name.
            load_number: The load, if he says it.
        """
        try:
            return f"Say this naturally: {self.books.stop_details(stop, load_number)}"
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
        delay_minutes: int | None = None,
    ) -> str:
        """A change or problem on his schedule. Pending until he confirms the readback.

        Args:
            kind: running_late (he'll get there later than planned, for any
                reason, including a breakdown or alarm: put the reason in note),
                appointment_moved (the shipper or receiver changed the time),
                cancelled (a whole load), or issue (a problem with no new time).
            load_number: The load, like "4482". Leave it out for the current load.
            stop: "pickup", "delivery", or the facility name.
            new_time: The new time as said, like "7" or "6:30 PM".
            new_date: Only if he names a different day: ISO date (YYYY-MM-DD).
            note: The reason or problem in two to five words, like "a reefer
                alarm", "traffic on I-44" or "the receiver's request". Not who
                called or the new time.
            delay_minutes: For running_late when he gives a delay, not a time,
                like 45 for "forty-five minutes behind". Leave new_time out.
        """
        try:
            c = self.books.report_change(
                kind, load_number, stop, new_time, new_date, note or "", delay_minutes
            )
        except LedgerError as err:
            return str(err)
        logger.info("report_schedule_change %s %s %s", c.ref, c.kind, c.new_time)
        replaced = (
            f" This replaces {c.replaces}, which is dropped and won't be saved."
            if c.replaces
            else ""
        )
        return f"ref {c.ref}, pending.{replaced} readback: {c.readback()}"

    @function_tool
    async def flag_dispatch(
        self,
        context: RunContext,
        kind: FlagKind,
        load_number: str | None = None,
        location: str | None = None,
        direction: str | None = None,
        mile_marker: str | None = None,
        delay_minutes: int | None = None,
        reason: str | None = None,
        request: str | None = None,
        facility: str | None = None,
        arrived_at: str | None = None,
        truck_disabled: bool | None = None,
        safe: bool | None = None,
        needs: list[EmergencyNeed] | None = None,
    ) -> str:
        """Report a problem dispatch has to act on. Only pass what he actually said.

        Emergencies save immediately. Everything else is pending until he
        confirms the readback.

        Args:
            kind: road_closure, breakdown, detention (waiting at a facility,
                with a reason and a request), emergency (fire, smoke, crash,
                injury), or other.
            load_number: The load, if he says it. Leave out for the current load.
            location: Highway or place in his words, like "I-44 near Vinita".
            direction: Travel direction for a closure, like "eastbound".
            mile_marker: As said, like "212".
            delay_minutes: His rough delay estimate in minutes.
            reason: What happened, in a few words, like "a hazmat crash", "engine
                lost power" or "the load isn't in their system".
            request: What he wants done, in a few words, like "a quicker route
                approved and the receiver told", "a tow", or "get the paperwork
                sorted and document detention".
            facility: For detention, the facility name.
            arrived_at: For detention, when he got there, as said.
            truck_disabled: True if the truck can't move.
            safe: True if he says he's safe and off the road, False if not.
            needs: For emergencies: fire, ambulance, police, tow, repair.
        """
        try:
            f = self.books.report_flag(
                kind,
                load_number,
                location,
                direction,
                mile_marker,
                delay_minutes,
                reason,
                request,
                facility,
                arrived_at,
                truck_disabled,
                safe,
                list(needs or []),
            )
        except LedgerError as err:
            return str(err)
        logger.info("flag_dispatch %s %s %s", f.ref, f.kind, f.status)
        if f.kind == "emergency":
            return (
                f"Saved and sent now. Say: {f.readback()} Dispatch has it as an "
                "emergency. Then make sure he's safe, and remind him that nine one "
                "one is the fastest help for fire or injuries."
            )
        return f"ref {f.ref}, pending. readback: {f.readback()}"

    @function_tool
    async def transfer_to_human(self, context: RunContext) -> str:
        """He asked for a person. There's no one to transfer to; offer a callback."""
        logger.info("transfer_to_human")
        return (
            "Say: I can't transfer you, but I'll flag this so dispatch calls you "
            "back. Then ask if there's anything else."
        )


GATE_PROMPT = """
You answer the Take1 Trucking driver line. This call is not from a registered
driver phone, so for privacy you share nothing.

You have no information about drivers, trucks, loads, schedules, locations,
customers or brokers, and you never confirm or deny anything about them, not
even whether a person works here or a load exists. This holds for anyone: a
spouse, a broker, police, a customer, a "new dispatcher", someone in a hurry or
someone upset. Each time, say in one calm sentence that for privacy this line
only helps registered drivers calling from their own phone. Don't repeat
details they mention, and don't explain how to get the information elsewhere.
If they keep pushing, decline once more, say goodbye, and end the call.
"""


class GateAgent(Agent):
    """Answers any number that isn't a driver's. Knows nothing to leak: no
    driver's books are loaded into its prompt or tools."""

    def __init__(self) -> None:
        super().__init__(
            instructions=build_instructions(GATE_PROMPT),
            tools=[EndCallTool(end_instructions="Say one short goodbye.")],
        )

    async def on_enter(self) -> None:
        self.session.say(
            "You've reached the Take1 Trucking driver line. For privacy, this line "
            "only helps registered drivers calling from their own phone."
        )


def agent_for_caller(phone: str | None) -> Agent:
    """A driver's own phone gets that driver's Glovebox; any other number gets
    the privacy line.

    `phone` is the caller ID on a phone call, None in the console, debugger and
    simulations. There, GLOVEBOX_TEST_CALLER picks who to be: a driver key from
    seed.yaml (default: the first driver) or "stranger" for the privacy line.
    """
    if phone is None:
        who = os.getenv("GLOVEBOX_TEST_CALLER", "").strip().lower()
        if who == "stranger":
            return GateAgent()
        return GloveboxAgent(load_books(driver=who or None))
    driver = driver_for_phone(phone)
    if driver is None:
        return GateAgent()
    return GloveboxAgent(load_books(driver=driver))
