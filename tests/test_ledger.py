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


# ---- seed and schedule (NOW is 5:30 PM; 4471 delivers at 9 PM today) ----


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
        "Load four four seven one delivery at Southmere Distribution in Tulsa, today at nine PM.",
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
        "today at eleven PM instead of today at nine PM, because of traffic. That still "
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
