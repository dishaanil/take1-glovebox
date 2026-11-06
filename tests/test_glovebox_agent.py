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
        "Kestrel Line Freight",
    ):
        assert name in terms
    assert len(terms) == len(set(terms))
    assert "home" not in terms
