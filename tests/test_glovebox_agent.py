from datetime import datetime

import pytest

from glovebox_agent import GloveboxAgent
from ledger import load_books


@pytest.fixture
def agent():
    return GloveboxAgent(load_books(datetime(2026, 11, 6, 17, 30)))


@pytest.mark.parametrize(
    ("heard", "line"),
    [
        ("what's coming up tomorrow", "Let me check your schedule."),
        ("reefer alarm, I'm running late", "Okay, one sec."),
        ("how's this load doing", "Pulling up the numbers."),
        ("fuel was 512.40", "Got it, one sec."),
        ("", "One sec."),
    ],
)
def test_filler_matches_what_he_said(agent, heard, line):
    assert agent.filler_line(heard) == line


def test_silence_lines_know_about_unconfirmed_records(agent):
    assert (
        agent.silence_line(1, last=False)
        == "Still there, Mike? No rush if you're driving."
    )
    assert agent.silence_line(2, last=False) == "You still with me?"
    agent.books.log_expense("toll", "12")
    assert "waiting on your yes" in agent.silence_line(2, last=False)
    assert "haven't saved anything you didn't confirm" in agent.silence_line(
        3, last=True
    )


def test_keyterms_come_from_the_seed(agent):
    from glovebox_agent import stt_keyterms

    terms = stt_keyterms(agent.books)
    for name in (
        "Love's",
        "Southmere Distribution",
        "Prairie Run Logistics",
        "Muskogee",
        "Benton Truck and Trailer Repair",
        "Take1 Trucking",
    ):
        assert name in terms
    assert len(terms) == len(set(terms))
    assert "home" not in terms


# ---- privacy: who gets in ----


def test_mikes_phone_gets_mikes_books_only(monkeypatch):
    from glovebox_agent import GloveboxAgent, agent_for_caller

    monkeypatch.setenv("GLOVEBOX_PHONES_MIKE", "+1 555 010 0123")
    a = agent_for_caller("+15550100123")
    assert isinstance(a, GloveboxAgent)
    assert a.books.driver == "Mike"
    assert "Southmere" in a.instructions
    assert "David" not in a.instructions and "Dellacourt" not in a.instructions


def test_unknown_or_hidden_number_gets_the_privacy_line():
    from glovebox_agent import GateAgent, agent_for_caller

    assert isinstance(agent_for_caller("+12125550199"), GateAgent)
    assert isinstance(agent_for_caller(""), GateAgent)  # withheld caller ID


def test_console_picks_who_to_be(monkeypatch):
    from glovebox_agent import GateAgent, agent_for_caller

    monkeypatch.delenv("GLOVEBOX_TEST_CALLER", raising=False)
    assert agent_for_caller(None).books.driver == "Mike"
    monkeypatch.setenv("GLOVEBOX_TEST_CALLER", "david")
    david = agent_for_caller(None)
    assert david.books.driver == "David"
    assert "Southmere" not in david.instructions and "4471" not in david.instructions
    monkeypatch.setenv("GLOVEBOX_TEST_CALLER", "stranger")
    assert isinstance(agent_for_caller(None), GateAgent)


def test_privacy_line_knows_nothing_to_leak():
    from glovebox_agent import GateAgent

    prompt = GateAgent().instructions
    for secret in ("Mike", "David", "4471", "5203", "Southmere", "Dellacourt", "555"):
        assert secret not in prompt


def test_driver_agent_has_dispatch_and_schedule_tools_but_no_profit(agent):
    names = {t.info.name for t in agent.tools if hasattr(t, "info")}
    assert {"flag_dispatch", "get_schedule", "get_stop_details"} <= names
    assert "get_load_summary" not in names


def test_keyterms_cover_every_driver():
    from glovebox_agent import stt_keyterms

    terms = stt_keyterms()
    assert {"Southmere Distribution", "Dellacourt Farm", "Mike", "David"} <= set(terms)
