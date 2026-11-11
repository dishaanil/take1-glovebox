from datetime import datetime
from decimal import Decimal

import pytest

from ledger import (
    LedgerError,
    load_books,
    money_words,
    parse_amount,
    parse_time,
    time_words,
)

NOW = datetime(2026, 11, 6, 17, 30)  # 5:30 PM


def books():
    return load_books(NOW)


@pytest.mark.parametrize(
    ("said", "expected"),
    [("512.40", "512.40"), ("$1,204", "1204"), ("85 bucks", "85"), ("85", "85")],
)
def test_parse_amount(said, expected):
    assert parse_amount(said) == Decimal(expected)


@pytest.mark.parametrize("said", ["5 12 40", "", "a lot", "-5", "12.345", "250000"])
def test_parse_amount_refuses_unclear(said):
    with pytest.raises(LedgerError):
        parse_amount(said)


def test_money_words():
    assert (
        money_words(Decimal("512.40")) == "five hundred twelve dollars and forty cents"
    )
    assert money_words(Decimal("1")) == "one dollar"
    assert money_words(Decimal("2400")) == "two thousand four hundred dollars"
    assert money_words(Decimal("0.01")) == "zero dollars and one cent"


def test_time_words():
    assert time_words(datetime(2026, 1, 1, 19, 50)) == "seven fifty PM"
    assert time_words(datetime(2026, 1, 1, 11, 5)) == "eleven oh five AM"
    assert time_words(datetime(2026, 1, 1, 14, 0)) == "two PM"


def test_parse_time_without_am_pm_uses_most_recent_past():
    assert parse_time("7:50", NOW).hour == 7  # 7:50 PM is still in the future
    assert parse_time("4:10", NOW).hour == 16


def test_parse_time_departure_follows_arrival():
    arrived = datetime(2026, 11, 6, 7, 50)
    assert parse_time("11:05", NOW, after=arrived) == datetime(2026, 11, 6, 11, 5)
    assert parse_time("1:15", NOW, after=arrived) == datetime(2026, 11, 6, 13, 15)


def test_expense_is_pending_until_confirmed():
    b = books()
    e = b.log_expense("fuel", "512.40", "Love's", "Joplin", "140", "fuel_card")
    assert e.status == "pending"
    assert e.readback() == (
        "Fuel at Love's in Joplin, one hundred forty gallons, five hundred twelve "
        "dollars and forty cents, on the fuel card."
    )
    assert "netting two thousand four hundred dollars" in b.load_summary()
    b.confirm(["E1"])
    assert (
        "netting one thousand eight hundred eighty-seven dollars and sixty cents"
        in (b.load_summary())
    )


def test_correction_reopens_confirmed_expense():
    b = books()
    b.log_expense("toll", "12")
    b.confirm(["E1"])
    e = b.correct_expense("E1", amount="21")
    assert (e.status, e.amount) == ("pending", Decimal("21"))
    assert len(b.expenses) == 1


def test_lumper_is_billed_back_by_default():
    b = books()
    e = b.log_expense("lumper", "150", payment_method="cash")
    assert e.reimbursable
    b.confirm(["E1"])
    summary = b.load_summary()
    assert "netting two thousand four hundred dollars" in summary
    assert "one hundred fifty dollars gets billed back to Bluegate Logistics" in summary


def test_discarded_expense_cannot_be_confirmed():
    b = books()
    b.log_expense("food", "14")
    b.discard_expense("E1")
    with pytest.raises(LedgerError):
        b.confirm(["E1"])


def test_detention_after_free_time_in_quarter_hours():
    b = books()  # pickup appointment 7:00 AM, two hours free, $50/hour
    b.log_stop_times("pickup", "7:50", "11:05")
    _, minutes, amount = b.detention("pickup")
    assert (minutes, amount) == (75, Decimal("62.50"))
    assert b.detention_summary("pickup") == (
        "That's one hour and fifteen minutes of detention at Harlan Point Cold "
        "Storage, sixty-two dollars and fifty cents from Bluegate Logistics."
    )


def test_detention_clock_starts_at_appointment_when_early():
    b = books()
    b.log_stop_times("Harlan Point", "6:00", "9:30")  # early; clock starts at 7:00
    assert b.detention("pickup")[1] == 30
    assert "starts at your appointment" in b.detention_summary("pickup")


def test_no_detention_inside_free_time():
    b = books()
    b.log_stop_times("pickup", "7:00", "8:45")
    assert b.detention_summary("pickup").startswith("No detention")


def test_claim_counts_in_load_summary():
    b = books()
    b.log_stop_times("pickup", "7:50", "11:05")
    b.load.stops["pickup"].claim_drafted = True
    assert "plus sixty-two dollars and fifty cents in detention" in b.load_summary()


def test_departure_needs_arrival():
    with pytest.raises(LedgerError):
        books().log_stop_times("delivery", None, "5:00")


def test_unknown_stop_asks_which():
    with pytest.raises(LedgerError, match="pickup or the delivery"):
        books().stop("Denver")


def test_placeholder_vendor_is_dropped():
    b = books()
    assert (
        b.log_expense("toll", "12", vendor="toll").readback()
        == "Tolls, twelve dollars."
    )
    assert b.log_expense("fuel", "80", vendor="Unknown").vendor is None


def test_load_number_is_spoken_digit_by_digit():
    assert books().load_summary().startswith("Load four four seven one pays")


# ---- seed and schedule (NOW is 5:30 PM; 4471 delivers at 4 PM today) ----


def test_seed_loads_from_yaml():
    b = books()
    assert [load.number for load in b.loads] == ["4471", "4482", "4490"]
    assert b.load.number == "4471"
    assert b.find_load("44 82").stops["pickup"].appointment == datetime(
        2026, 11, 7, 6, 0
    )
    assert [e.what for e in b.events] == [
        "Oil change and PM service",
        "Home time starts",
    ]


def test_schedule_covers_the_next_three_days():
    summary = books().full_schedule()
    for expected in (
        "Load four four seven one delivery at Southmere Distribution in Tulsa, today at four PM.",
        "Load four four eight two pickup at Corliss Packaging in Muskogee, tomorrow at six AM.",
        "Oil change and PM service at Benton Truck and Trailer Repair in Joplin, Sunday at eight AM",
        "Load four four nine zero delivery at Orrin Valley Foods in Little Rock, Monday at seven AM.",
        "Home time starts",
    ):
        assert expected in summary
    assert "Harlan Point" not in summary  # this morning's pickup is behind him


def test_schedule_preview_offers_the_rest():
    summary = books().schedule_summary()
    assert summary.startswith("Load four four seven one delivery")
    assert summary.endswith("Then four more after that. Want the rest?")
    assert "Joplin" not in summary


def test_schedule_for_one_day():
    summary = books().schedule_summary("2026-11-07")
    assert summary.startswith("Load four four eight two pickup")
    assert "Southmere" not in summary


def test_running_late_with_room_to_spare():
    c = books().report_change(
        "running_late", stop="delivery", new_time="11", note="traffic"
    )
    assert c.new_time == datetime(2026, 11, 6, 23, 0)
    assert c.readback() == (
        "Load four four seven one delivery at Southmere Distribution: you're now expected "
        "today at eleven PM instead of today at four PM, because of traffic. That still "
        "leaves time for your six AM pickup at Corliss Packaging."
    )


def test_running_late_puts_next_pickup_at_risk():
    b = books()
    c = b.report_change(
        "running_late", "4471", "Southmere", "5 AM", "2026-11-07", "reefer alarm"
    )
    assert c.new_time == datetime(2026, 11, 7, 5, 0)
    assert c.readback().endswith(
        "Heads up, that puts your six AM pickup at Corliss Packaging at risk. With "
        "unloading and the drive, you'd get there around seven thirty AM."
    )


def test_moved_pickup_is_checked_against_the_late_delivery():
    b = books()
    b.confirm(
        [b.report_change("running_late", "4471", "delivery", "5 AM", "2026-11-07").ref]
    )
    tight = b.report_change("appointment_moved", "4482", "pickup", "7")
    assert tight.readback().endswith(
        "Heads up, after your Southmere Distribution delivery you'd get there around "
        "seven thirty AM, so that's still tight."
    )
    fixed = b.report_change("appointment_moved", "4482", "pickup", "9")
    assert fixed.readback().endswith(
        "That works with your Southmere Distribution delivery."
    )


def test_change_applies_only_after_confirm():
    b = books()
    c = b.report_change("appointment_moved", "4482", "pickup", "8")
    pickup = b.find_load("4482").stops["pickup"]
    assert c.new_time == datetime(2026, 11, 7, 8, 0)  # closest to the old 6 AM
    assert pickup.appointment.hour == 6
    b.confirm([c.ref])
    assert pickup.appointment.hour == 8
    assert "Corliss Packaging in Muskogee, tomorrow at eight AM" in b.schedule_summary()


def test_cancelled_load_shows_on_schedule():
    b = books()
    b.confirm([b.report_change("cancelled", "4490", note="a closed shipper").ref])
    assert (
        "Load four four nine zero pickup at Tamsin Foods Warehouse in Joplin, Sunday at two PM, cancelled."
        in (b.full_schedule())
    )


def test_issue_needs_a_note_and_reads_back():
    b = books()
    with pytest.raises(LedgerError):
        b.report_change("issue")
    c = b.report_change("issue", "4471", note="reefer is throwing a code")
    assert (
        c.readback() == "Noting on load four four seven one: reefer is throwing a code."
    )


def test_unknown_load_is_refused():
    with pytest.raises(LedgerError, match="four four four four"):
        books().report_change("cancelled", "4444")


def test_has_pending_covers_expenses_and_changes():
    b = books()
    assert not b.has_pending()
    b.log_expense("toll", "12")
    assert b.has_pending()
    b.confirm(["E1"])
    assert not b.has_pending()
    c = b.report_change("issue", note="a low tire")
    assert b.has_pending()
    b.confirm([c.ref])
    assert not b.has_pending()


def test_issue_with_a_new_time_is_a_delay():
    c = books().report_change(
        "issue", "4471", "delivery", "5:00 AM", "2026-11-07", "reefer alarm"
    )
    assert (c.kind, c.new_time) == ("running_late", datetime(2026, 11, 7, 5, 0))
    assert "because of reefer alarm" in c.readback()
    assert "at risk" in c.readback()


def test_new_report_replaces_unconfirmed_one_for_the_same_stop():
    b = books()
    eight = b.report_change("appointment_moved", "4482", "pickup", "8")
    nine = b.report_change("appointment_moved", "4482", "pickup", "9")
    assert eight.status == "discarded"
    assert nine.replaces == eight.ref
    with pytest.raises(LedgerError):
        b.confirm([eight.ref])
    b.confirm([nine.ref])
    assert b.find_load("4482").stops["pickup"].appointment.hour == 9
    assert not b.has_pending()


# ---- dispatch flags (NOW is 5:30 PM) ----


def test_road_closure_readback_has_every_detail_he_gave():
    b = books()
    f = b.report_flag(
        "road_closure",
        location="I-44",
        direction="eastbound",
        mile_marker="212",
        delay_minutes=120,
        reason="a hazmat crash",
        request="a quicker route approved and the receiver told",
    )
    assert f.status == "pending"
    assert f.readback() == (
        "Eastbound closure on I-44 at mile marker 212, because of a hazmat crash. "
        "You're looking at about two hours of delay. Load four four seven one. "
        "You want a quicker route approved and the receiver told."
    )
    b.confirm([f.ref])
    assert f.status == "confirmed"


def test_closure_leaves_out_what_he_didnt_say():
    f = books().report_flag("road_closure", location="I-44")
    assert f.readback() == "A closure on I-44. Load four four seven one."


def test_breakdown_keeps_location_and_doesnt_diagnose():
    f = books().report_flag(
        "breakdown",
        location="I-44 westbound near mile marker 180",
        truck_disabled=True,
        safe=True,
        reason="engine lost power",
        request="a tow",
    )
    assert f.readback() == (
        "Truck 1 is disabled at I-44 westbound near mile marker 180, and you're "
        "safely off the road. Load four four seven one. What happened: engine lost "
        "power. You're asking for a tow."
    )


def test_receiver_detention_starts_the_clock_at_the_appointment():
    b = books()  # Southmere appointment 4 PM
    f = b.report_flag(
        "detention",
        facility="Southmere",
        arrived_at="3:30",
        reason="the load isn't in their system",
        request="get the paperwork sorted and document detention",
    )
    assert f.clock_start == datetime(2026, 11, 6, 16, 0)
    assert f.readback() == (
        "At Southmere Distribution, in since three thirty PM. The detention clock "
        "started at four PM, so that's one hour and thirty minutes so far. Load four "
        "four seven one. Reason they gave: the load isn't in their system. You want "
        "dispatch to get the paperwork sorted and document detention."
    )
    assert b.find_load("4471").stops["delivery"].arrived == datetime(
        2026, 11, 6, 15, 30
    )


def test_detention_needs_an_arrival_time():
    with pytest.raises(LedgerError, match="What time did you get to Southmere"):
        books().report_flag("detention", facility="Southmere")


def test_emergency_saves_immediately():
    b = books()
    f = b.report_flag(
        "emergency",
        location="the shoulder of I-44 near Vinita",
        reason="smoke from the engine",
        needs=["fire", "tow"],
        safe=True,
    )
    assert f.status == "confirmed"
    assert not b.has_pending()
    assert f.readback() == (
        "Emergency flagged to dispatch: smoke from the engine at the shoulder of I-44 "
        "near Vinita. Needs the fire department and a tow. You're at a safe spot."
    )


def test_traffic_delay_in_minutes_does_the_clock_math():
    c = books().report_change(
        "running_late", stop="delivery", delay_minutes=45, note="a multi-car crash"
    )
    # The 4 PM appointment has passed at 5:30, so the delay counts from now.
    assert c.new_time == datetime(2026, 11, 6, 18, 15)
    assert c.readback().startswith(
        "Load four four seven one delivery at Southmere Distribution: you're now "
        "expected today at six fifteen PM instead of today at four PM, because "
        "of a multi-car crash."
    )


def test_traffic_delay_counts_from_the_appointment_when_its_ahead():
    c = books().report_change(
        "running_late", "4482", "pickup", delay_minutes=45, note="a crash"
    )
    assert c.new_time == datetime(2026, 11, 7, 6, 45)


def test_issue_with_a_delay_is_a_delay():
    c = books().report_change("issue", delay_minutes=30, note="traffic")
    assert c.kind == "running_late"


# ---- schedule the way a driver hears it ----


def test_schedule_update_with_nothing_changed():
    assert books().schedule_update() == (
        "Nothing's changed. Next up: load four four seven one delivery at Southmere "
        "Distribution in Tulsa, today at four PM. From dispatch: Southmere wants a call "
        "thirty minutes out. At Corliss, check in at the guard shack, then dock four."
    )


def test_schedule_update_leads_with_what_changed():
    b = books()
    b.confirm([b.report_change("appointment_moved", "4482", "pickup", "9").ref])
    update = b.schedule_update()
    assert update.startswith(
        "What's changed: Load four four eight two pickup at Corliss Packaging moved "
        "from tomorrow at six AM to tomorrow at nine AM."
    )


def test_stop_details_include_dispatch_notes():
    assert books().stop_details("Corliss") == (
        "Corliss Packaging in Muskogee is the pickup for load four four eight two, "
        "tomorrow at six AM. At Corliss, check in at the guard shack, then dock four."
    )


# ---- drivers are kept apart ----


def test_each_driver_gets_only_their_own_books(monkeypatch):
    from ledger import driver_for_phone

    monkeypatch.setenv("GLOVEBOX_PHONES_MIKE", "+1 555 010 0123")
    monkeypatch.setenv("GLOVEBOX_PHONES_DAVID", "555-010-0456")

    mike, david = books(), load_books(NOW, driver="david")
    assert (mike.driver, mike.business) == ("Mike", "Take1 Trucking")
    assert [load.number for load in david.loads] == ["5203", "5210"]
    assert not {load.number for load in mike.loads} & {
        load.number for load in david.loads
    }
    assert "Dellacourt" not in mike.full_schedule()
    assert "Southmere" not in david.full_schedule()
    assert driver_for_phone("+1 (555) 010-0123") == "mike"
    assert driver_for_phone("5550100123") == "mike"
    assert driver_for_phone("+15550100456") == "david"
    assert driver_for_phone("+1 212 555 0100") is None
    assert driver_for_phone("") is None
