"""Small, synthetic records for the local-model learning walkthrough.

The first task is intentionally easy: every notice names its class in plain
language.  The second task makes the review boundary visible by pairing a
multi-constraint reasoning problem with an editor-authored answer key.  Model
output is never shipped in the fixture; users create it and compare it with
the key themselves.
"""

from __future__ import annotations


LOCAL_MODEL_LAB_ROWS: tuple[dict[str, str], ...] = (
    {
        "record_id": "LOCAL-001",
        "easy_notice": (
            "MEETING NOTICE: The Riverton Transit Board will meet Tuesday at "
            "6 p.m. to vote on the depot roof amendment."
        ),
        "expected_notice_type": "meeting",
        "challenge_text": (
            "A reporter can request one memo before deadline. MEM-201 is a draft "
            "dated May 2. MEM-204 is the final memo dated May 4 and was circulated "
            "before the May 5 board vote. MEM-207 is a later attachment created "
            "after the vote. The assignment says to request the latest final memo "
            "that board members could have read before voting, and not an attachment."
        ),
        "challenge_answer": "MEM-204",
    },
    {
        "record_id": "LOCAL-002",
        "easy_notice": (
            "INSPECTION REPORT: A city inspector found two blocked exits at the "
            "Harbor Street shelter on March 8."
        ),
        "expected_notice_type": "inspection",
        "challenge_text": (
            "Only one invoice meets every audit rule: service date in February, "
            "approval before March 10, and no duplicate service period. INV-441 "
            "covers February but was approved March 12. INV-447 covers February, "
            "was approved March 9, and has a unique service period. INV-452 was "
            "approved March 8 but repeats INV-439's January service dates. Which "
            "invoice should be tested first?"
        ),
        "challenge_answer": "INV-447",
    },
    {
        "record_id": "LOCAL-003",
        "easy_notice": (
            "CONTRACT AWARD: Public Works awarded a bridge inspection contract to "
            "Eastbank Infrastructure for $420,000."
        ),
        "expected_notice_type": "contract",
        "challenge_text": (
            "The scoring sheet gives price 40 points and experience 60 points. "
            "Bid A has 39 price and 50 experience points. Bid B has 35 price and "
            "58 experience points but its required insurance expired before bids "
            "closed, so the rules disqualify it. Bid C has 37 price and 54 "
            "experience points and is eligible. Identify the highest-scoring "
            "eligible bid, not merely the highest raw score."
        ),
        "challenge_answer": "Bid C",
    },
    {
        "record_id": "LOCAL-004",
        "easy_notice": (
            "MEETING NOTICE: The library trustees scheduled a special meeting for "
            "Friday to discuss the software pilot."
        ),
        "expected_notice_type": "meeting",
        "challenge_text": (
            "The first press release said a grant was $980,000. A correction issued "
            "the same afternoon changed it to $890,000. The signed award letter in "
            "the records portal says $890,000, while a campaign speech repeats the "
            "old number. The assignment requires the corrected amount corroborated "
            "by the signed primary record. What amount should the story use?"
        ),
        "challenge_answer": "$890,000",
    },
    {
        "record_id": "LOCAL-005",
        "easy_notice": (
            "INSPECTION REPORT: Water Authority staff recorded a failed pressure "
            "test at Pump Station 3 on April 14."
        ),
        "expected_notice_type": "inspection",
        "challenge_text": (
            "Nine council seats exist. One seat is vacant and member Ortiz recused. "
            "Of the seven members voting, four voted yes and three voted no on the "
            "amended motion. The clerk's earlier note showing 4-4 included Ortiz "
            "before the recusal was entered. Report the final vote and whether the "
            "motion passed under the simple-majority rule."
        ),
        "challenge_answer": "4-3; passed",
    },
    {
        "record_id": "LOCAL-006",
        "easy_notice": (
            "CONTRACT AWARD: The Parks Department selected Greenline Civic Data for "
            "a tree inventory contract."
        ),
        "expected_notice_type": "contract",
        "challenge_text": (
            "The procurement log records the bid opening at 10:00 a.m. An email from "
            "the winning vendor arrived at 9:42 a.m. A revised price sheet arrived "
            "at 10:06 a.m. The rules allow clarifications after opening but prohibit "
            "price changes. The 9:42 email only clarified a staff resume; the 10:06 "
            "sheet lowered the price. Which timestamp identifies the submission that "
            "raises the prohibited-change question?"
        ),
        "challenge_answer": "10:06 a.m.",
    },
)
