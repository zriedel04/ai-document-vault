"""The public retrieval eval set. Every question is about the fictional corpus in ``demo/corpus``.

Each question carries the evidence it should be answered from. A ``quote`` is a verbatim piece of
the ground-truth text of the source page (whitespace is normalised when checked), so a test can
prove that every cited source really says what the question claims it says.

Kinds of question, set in ``category``:

- ``lookup``: one fact in one document.
- ``multi_doc``: needs facts from more than one document.
- ``version``: documents disagree and the newest or authoritative one wins.
- ``ocr``: the answer exists only in a scanned PDF or a picture, so text extraction alone fails.
- ``numeric``: a number, sum or comparison from a table or receipt.
- ``paraphrase``: the question shares few words with the evidence.
- ``not_in_vault``: nothing in the corpus answers it; the right behaviour is to say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Category = Literal["lookup", "multi_doc", "version", "ocr", "numeric", "paraphrase", "not_in_vault"]

SYLLABUS = "courses/cse485/syllabus.pdf"
ASSIGN1 = "courses/cse485/assignment-1-project-proposal.pdf"
ASSIGN2 = "courses/cse485/assignment-2-requirements-spec.pdf"
LECTURE4 = "courses/cse485/lecture-04-slide-notes.pdf"
REQ_V1 = "project/requirements-v1.docx"
REQ_V2 = "project/requirements-v2.docx"
REVIEW = "project/design-review-feedback.docx"
SIGNOFF = "scans/advisor-signoff-form-scan.pdf"
MEETING = "scans/team-meeting-notes-scan.pdf"
MAIL_ASK = "emails/advisor-extension-request.txt"
MAIL_REPLY = "emails/advisor-reply.txt"
STANDUP_0908 = "notes/standup-2026-09-08.txt"
STANDUP_0915 = "notes/standup-2026-09-15.txt"
STANDUP_0922 = "notes/standup-2026-09-22.txt"
CONTRACT = "notes/team-contract.md"
JOURNAL = "notes/project-journal.md"
STUDY = "notes/study-plan.md"
API = "notes/api-design-notes.md"
GRADES = "data/grades.csv"
BUDGET = "data/project-budget.csv"
HOURS = "data/team-hours.csv"
BUILD_ERROR = "screenshots/build-error.png"
KANBAN = "screenshots/kanban-board.png"
CALENDAR = "screenshots/calendar-finals-week.png"
CHAT = "screenshots/chat-with-advisor.png"
BOOKS = "receipts/bookstore-receipt.jpg"
COFFEE = "receipts/coffee-receipt.jpg"
REPAIR = "receipts/laptop-repair-receipt.jpg"
PRINTING = "receipts/printing-receipt.jpg"
MATH_SYLLABUS = "courses/math241/syllabus.pdf"
OS_SYLLABUS = "courses/cs350/syllabus.pdf"
DEMO_DAY = "emails/class-announcement-demo-day.txt"


@dataclass(frozen=True)
class Source:
    path: str
    quote: str
    page: int | None = None  # 1-based; None for formats without pages


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    category: Category
    answer: str
    sources: tuple[Source, ...] = ()  # every passage the answer needs
    # Other passages that also give the answer: citing one of them is as good as citing a source
    # (the same fact in a second document). They are never required.
    alternatives: tuple[Source, ...] = ()
    distractors: tuple[str, ...] = ()  # documents that look relevant but are outdated or wrong
    tags: tuple[str, ...] = field(default=())

    @property
    def answerable(self) -> bool:
        return self.category != "not_in_vault"


def s(path: str, quote: str, page: int | None = None) -> Source:
    return Source(path, quote, page)


_Row = tuple[Category, str, str, tuple[Source, ...], tuple[str, ...], tuple[Source, ...]]


def _q(
    category: Category,
    question: str,
    answer: str,
    sources: tuple[Source, ...],
    distractors: tuple[str, ...] = (),
    alternatives: tuple[Source, ...] = (),
) -> _Row:
    return category, question, answer, sources, distractors, alternatives


_RAW: list[_Row] = [
    # --- lookup: course documents -----------------------------------------------------------
    _q(
        "lookup",
        "Who teaches CSE 485 and what is their email address?",
        "Dr. Priya Raman, priya.raman@lit.example.",
        (s(SYLLABUS, "Instructor: Dr. Priya Raman, priya.raman@lit.example.", 1),),
        (),
    ),
    _q(
        "lookup",
        "When and where does the capstone class meet?",
        "Tuesdays and Thursdays from 2:00 PM to 3:15 PM in Room 214 of Harbor Hall.",
        (
            s(
                SYLLABUS,
                "Class meets on Tuesdays and Thursdays from 2:00 PM to 3:15 PM in Room 214 of Harbor Hall.",
                1,
            ),
        ),
        (),
    ),
    _q(
        "lookup",
        "When are Dr. Raman's office hours?",
        "Wednesdays from 10:00 AM to 12:00 PM in Room 318 of Harbor Hall.",
        (
            s(
                SYLLABUS,
                "Office hours are on Wednesdays from 10:00 AM to 12:00 PM in Room 318 of Harbor Hall.",
                1,
            ),
        ),
        (),
    ),
    _q(
        "lookup",
        "Who is the teaching assistant and when can I reach them?",
        "Marcus Bell, on Fridays from 1:00 PM to 3:00 PM, online.",
        (
            s(
                SYLLABUS,
                "Teaching assistant: Marcus Bell. His office hours are on Fridays from 1:00 PM to 3:00 PM, online.",
                1,
            ),
        ),
        (),
    ),
    _q(
        "lookup",
        "What share of the final grade does the final report make up?",
        "25 percent.",
        (s(SYLLABUS, "Final report: 25 percent", 2),),
        (),
    ),
    _q(
        "lookup",
        "What is the penalty for handing work in late in CSE 485?",
        "10 percent per day for up to five days; after five days the work is not accepted.",
        (
            s(
                SYLLABUS,
                "Late work loses 10 percent per day, up to five days. After five days the work is not accepted.",
                2,
            ),
        ),
        (),
    ),
    _q(
        "lookup",
        "Is there a final exam in CSE 485?",
        "No, there is no final exam.",
        (s(SYLLABUS, "There is no final exam in this course.", 3),),
        (),
    ),
    _q(
        "lookup",
        "When is the design review?",
        "Friday, October 30, 2026.",
        (s(SYLLABUS, "Design review: Friday, October 30, 2026", 3),),
        alternatives=(s(REVIEW, "Design review held on Friday, October 30, 2026."),),
    ),
    _q(
        "lookup",
        "How many functional and non-functional requirements must the specification contain at minimum?",
        "At least 12 functional requirements and at least 5 non-functional requirements.",
        (
            s(
                ASSIGN2,
                "at least 12 functional requirements and at least 5 non-functional requirements",
                1,
            ),
        ),
        (),
    ),
    _q(
        "lookup",
        "In what format and where do I submit the project proposal?",
        "As a single PDF file through the course portal.",
        (s(ASSIGN1, "Submit the proposal as a single PDF file through the course portal.", 2),),
        (),
    ),
    _q(
        "lookup",
        "How long can the project proposal be, and what happens if it is longer?",
        "No more than two pages; each extra page loses 5 points.",
        (
            s(ASSIGN1, "no more than two pages", 1),
            s(ASSIGN1, "A proposal that exceeds two pages loses 5 points for each extra page.", 2),
        ),
        (),
    ),
    _q(
        "lookup",
        "In MoSCoW prioritisation, what does 'could have' mean?",
        "Nice to have if time allows.",
        (s(LECTURE4, "Could have: nice to have if time allows", 3),),
        (),
    ),
    _q(
        "lookup",
        "Which database did Jordan choose for the prototype?",
        "SQLite.",
        (s(STANDUP_0915, "Jordan: chose SQLite for the prototype."),),
        (),
    ),
    _q(
        "lookup",
        "How many items does a list endpoint return by default and what is the most a client can ask for?",
        "20 by default and at most 100 per page.",
        (
            s(
                API,
                "List endpoints return 20 items by default. A client may ask for at most 100 items per page with the limit parameter.",
            ),
        ),
        (),
    ),
    _q(
        "lookup",
        "When is the Operating Systems final?",
        "Wednesday, December 9 at 1:00 PM.",
        (s(STUDY, "Wednesday, December 9 at 1:00 PM: Operating Systems final"),),
        alternatives=(
            s(CALENDAR, "Wed Dec 9: Operating Systems final at 1:00 PM"),
            s(OS_SYLLABUS, "The final exam is on Wednesday, December 9, 2026 at 1:00 PM.", 2),
        ),
    ),
    _q(
        "lookup",
        "When is the Calculus II final exam?",
        "Tuesday, December 8 at 9:00 AM.",
        (s(MATH_SYLLABUS, "The final exam is on Tuesday, December 8, 2026 at 9:00 AM.", 2),),
        alternatives=(
            s(STUDY, "Tuesday, December 8 at 9:00 AM: Calculus II final"),
            s(CALENDAR, "Tue Dec 8: Calculus II final at 9:00 AM"),
        ),
    ),
    _q(
        "lookup",
        "How long is each team's prototype demo and how is the time split?",
        "12 minutes: 8 to present and 4 for questions.",
        (
            s(
                DEMO_DAY,
                "Each team has 12 minutes for its prototype demo on Friday, November 20: 8 minutes to present and 4 minutes for questions.",
            ),
        ),
    ),
    _q(
        "lookup",
        "Which comes first, the Calculus II final or the Operating Systems final?",
        "The Calculus II final (Tuesday, December 8 at 9:00 AM), one day before the Operating Systems final (Wednesday, December 9 at 1:00 PM).",
        (
            s(
                STUDY,
                "Tuesday, December 8 at 9:00 AM: Calculus II final - Wednesday, December 9 at 1:00 PM: Operating Systems final",
            ),
        ),
        alternatives=(
            s(
                CALENDAR,
                "Tue Dec 8: Calculus II final at 9:00 AM Wed Dec 9: Operating Systems final at 1:00 PM",
            ),
        ),
    ),
    # --- multi_doc and reasoning ------------------------------------------------------------
    _q(
        "lookup",
        "Why did Alex ask the instructor for more time on Assignment 2?",
        "Sam's laptop screen broke, the repair would take five business days, and Sam held the latest draft.",
        (
            s(
                MAIL_ASK,
                "Our teammate Sam's laptop screen broke today and the repair shop says the repair will take five business days. Sam holds the latest draft of our requirements specification.",
            ),
        ),
    ),
    _q(
        "multi_doc",
        "Did the extension request follow the syllabus rule about timing?",
        "Yes. The syllabus asks for requests at least 48 hours before the deadline and this one was sent six days before it.",
        (
            s(
                SYLLABUS,
                "Extension requests must be sent to the instructor at least 48 hours before the deadline.",
                2,
            ),
            s(MAIL_ASK, "this request is sent six days before it"),
        ),
        (),
    ),
    _q(
        "lookup",
        "Does the approved extension apply to the other assignments too?",
        "No, it applies to Assignment 2 only.",
        (s(MAIL_REPLY, "This applies to Assignment 2 only."),),
        (),
    ),
    _q(
        "lookup",
        "What did the instructor ask the team to put on the first page of the specification after the extension?",
        "A short note saying that the deadline was extended.",
        (
            s(
                MAIL_REPLY,
                "Please add a short note on the first page of the specification that says the deadline was extended.",
            ),
        ),
    ),
    _q(
        "multi_doc",
        "Which course components are not yet in the grades file?",
        "The final report (25 percent) and participation (10 percent).",
        (
            s(SYLLABUS, "Final report: 25 percent", 2),
            s(SYLLABUS, "Participation: 10 percent", 2),
            s(GRADES, "Peer evaluation,5,96,100"),
        ),
        (),
    ),
    _q(
        "lookup",
        "What was the design review score and what did the reviewer ask the team to add?",
        "92 out of 100; an index on the course code column for the group search query.",
        (
            s(REVIEW, "Design review score: 92 out of 100."),
            s(
                REVIEW,
                "The group search query needs an index on the course code column before the demo.",
            ),
        ),
    ),
    _q(
        "multi_doc",
        "How did the decision about in-app chat develop over time?",
        "It was an open question in a team standup and again in a team meeting, and it was later removed from the first release because it would take too long to build.",
        (
            s(
                STANDUP_0922,
                "Blockers: we need to decide whether the first release has in-app chat.",
            ),
            s(MEETING, "- Should the first release include in-app chat?", 2),
            s(
                JOURNAL,
                "We removed in-app chat from the first release because it would take too long to build.",
            ),
        ),
        (REQ_V1,),
    ),
    _q(
        "multi_doc",
        "How much did it cost to repair the laptop screen that broke on October 3?",
        "$187.92 (Sam Patel's repair, Quick Fix Computers). The journal dates the breakage and the receipt gives the bill.",
        (
            s(JOURNAL, "Sam's laptop screen broke."),
            s(REPAIR, "Customer: Sam Patel"),
            s(REPAIR, "Total $187.92"),
        ),
    ),
    # --- version conflicts ------------------------------------------------------------------
    _q(
        "version",
        "What is the maximum number of members in a study group in the current requirements?",
        "10 members.",
        (s(REQ_V2, "NFR-3: A study group shall have a maximum of 10 members."),),
        (REQ_V1,),
    ),
    _q(
        "version",
        "How quickly must search results load according to the submitted specification?",
        "Within 2 seconds.",
        (s(REQ_V2, "NFR-1: Search results shall load within 2 seconds."),),
        (REQ_V1,),
    ),
    _q(
        "version",
        "How many concurrent users must the system support?",
        "500 concurrent users.",
        (s(REQ_V2, "NFR-2: The system shall support 500 concurrent users."),),
        (REQ_V1, JOURNAL),
    ),
    _q(
        "version",
        "Is in-app chat part of the first release?",
        "No. In-app chat is out of scope for the first release.",
        (
            s(
                REQ_V2,
                "Payments, video calls, in-app chat and a native mobile application are out of scope.",
            ),
        ),
        (REQ_V1,),
    ),
    _q(
        "version",
        "Which requirements were added between version 1 and version 2 of the specification?",
        "FR-8 (export a meeting as an iCalendar file), FR-9 (leave a group), FR-10 (remove a member), FR-11 (report inappropriate content), FR-12 (import the course list from a CSV file) and NFR-6 (WCAG 2.1 AA contrast).",
        (
            s(
                REQ_V2,
                "FR-8: The system shall let a student export a scheduled meeting as an iCalendar file.",
            ),
            s(REQ_V2, "FR-9: The system shall let a student leave a study group."),
            s(REQ_V2, "FR-10: The system shall let a group owner remove a member from the group."),
            s(
                REQ_V2,
                "FR-11: The system shall let a student report inappropriate content to an administrator.",
            ),
            s(
                REQ_V2,
                "FR-12: The system shall let an administrator import the course list from a CSV file.",
            ),
            s(REQ_V2, "NFR-6: All pages shall meet the WCAG 2.1 level AA contrast requirements."),
        ),
        (REQ_V1,),
    ),
    _q(
        "version",
        "When is Assignment 2 due for Team Northwind?",
        "Monday, October 12 at 11:59 PM, after the approved extension.",
        (
            s(
                MAIL_REPLY,
                "Your team may submit Assignment 2 by Monday, October 12 at 11:59 PM with no late penalty.",
            ),
        ),
        (SYLLABUS, ASSIGN2),
    ),
    _q(
        "version",
        "When was version 2 of the requirements submitted?",
        "October 11, 2026.",
        (s(REQ_V2, "Version 2.0 (submitted), October 11, 2026."),),
        (REQ_V1,),
    ),
    # --- ocr: only in scans and pictures ----------------------------------------------------
    _q(
        "ocr",
        "What conditions did the advisor attach to approving the team's project?",
        "A team of three is approved, the scope is limited to a web application, and any change of scope needs written approval.",
        (
            s(SIGNOFF, "- A team of three students is approved", 1),
            s(SIGNOFF, "- The scope is limited to a web application", 1),
            s(SIGNOFF, "- Any change of scope needs written approval", 1),
        ),
        (),
    ),
    _q(
        "ocr",
        "How many groups will Jordan use to test search speed?",
        "1000 groups.",
        (s(MEETING, "- Jordan will test search with 1000 groups", 2),),
        (),
    ),
    _q(
        "ocr",
        "When and where was the next team meeting after October 2 held?",
        "Thursday, October 8 at 4:30 PM in library study room 3.",
        (
            s(MEETING, "Next meeting: Thursday, October 8 at 4:30 PM", 2),
            s(MEETING, "Place: library study room 3", 2),
        ),
        (),
    ),
    _q(
        "ocr",
        "What was Alex's action item at the October 2 meeting?",
        "Send requirements version 2 to the team by October 8.",
        (s(MEETING, "- Alex: send requirements version 2 to the team by October 8", 1),),
        (),
    ),
    _q(
        "ocr",
        "Which npm module could the build not find?",
        "express-session.",
        (s(BUILD_ERROR, "Module not found: Error: Can't resolve 'express-session'"),),
        (),
    ),
    _q(
        "ocr",
        "Which command did Alex run to fix the build error?",
        "npm install express-session",
        (s(BUILD_ERROR, "alex@laptop:~/studybuddy$ npm install express-session"),),
        (),
    ),
    _q(
        "ocr",
        "Which tasks are in progress on the sprint board and who owns them?",
        "Group page layout (Sam) and the join request endpoint (Jordan).",
        (
            s(KANBAN, "Group page layout (Sam)"),
            s(KANBAN, "Join request endpoint (Jordan)"),
        ),
        (),
    ),
    _q(
        "ocr",
        "What did Dr. Raman ask the team to fix before the final report?",
        "The search speed; Jordan is adding an index.",
        (
            s(
                CHAT,
                "Dr. Raman: Great demo today. Please fix the search speed before the final report.",
            ),
            s(CHAT, "Alex: Will do. Jordan is adding an index."),
        ),
        (),
    ),
    # --- numeric: receipts and tables -------------------------------------------------------
    _q(
        "numeric",
        "How much did the textbook cost at the bookstore including tax?",
        "$97.15 ($89.95 plus $7.20 tax).",
        (
            s(BOOKS, "Software Engineering in Practice 4th Ed $89.95"),
            s(BOOKS, "Tax (8%) $7.20"),
            s(BOOKS, "Total $97.15"),
        ),
        (),
    ),
    _q(
        "multi_doc",
        "Does the price on the bookstore receipt match the price in the CSE 485 syllabus?",
        "Yes, both give 89.95 dollars before tax.",
        (
            s(SYLLABUS, "The bookstore lists it at 89.95 dollars.", 3),
            s(BOOKS, "Software Engineering in Practice 4th Ed $89.95"),
        ),
        (),
    ),
    _q(
        "numeric",
        "What was the laptop repair bill before tax?",
        "$174.00 ($129.00 for the screen plus $45.00 labor).",
        (
            s(REPAIR, "Screen replacement $129.00"),
            s(REPAIR, "Labor $45.00"),
            s(REPAIR, "Subtotal $174.00"),
        ),
        (),
    ),
    _q(
        "numeric",
        "What is the total of the team's project budget?",
        "$82.90.",
        (s(BUDGET, "Total,82.90"),),
        (),
    ),
    _q(
        "multi_doc",
        "Does the poster printing receipt agree with the project budget?",
        "Yes, both show $7.50.",
        (
            s(PRINTING, "Poster prints 1 x $7.50"),
            s(BUDGET, "Poster printing,7.50"),
        ),
        (),
    ),
    _q(
        "numeric",
        "How many hours in total did Jordan log over the six weeks?",
        "41 hours.",
        (s(HOURS, "total,36,41,27"),),
        (),
    ),
    _q(
        "numeric",
        "In which week did Sam log the fewest hours?",
        "Week 5, with 2 hours.",
        (s(HOURS, "5,6,9,2"),),
        (),
    ),
    # --- paraphrase: few shared words -------------------------------------------------------
    _q(
        "paraphrase",
        "Who got the team's laptop fixed at a shop?",
        "Sam Patel, at Quick Fix Computers.",
        (
            s(REPAIR, "QUICK FIX COMPUTERS"),
            s(REPAIR, "Customer: Sam Patel"),
        ),
        (),
    ),
    _q(
        "paraphrase",
        "What is the CSE 485 rule if I turn work in a week after the deadline without asking?",
        "It is not accepted, because late work is only accepted for up to five days.",
        (
            s(
                SYLLABUS,
                "Late work loses 10 percent per day, up to five days. After five days the work is not accepted.",
                2,
            ),
        ),
        (),
    ),
    _q(
        "paraphrase",
        "Which framework was Jordan leaning toward at the start?",
        "Flask.",
        (s(STANDUP_0908, "Jordan: comparing web frameworks. Leaning toward Flask."),),
        (),
    ),
    # --- not in the vault -------------------------------------------------------------------
    _q(
        "not_in_vault",
        "What grade did the team get on the final report?",
        "Nothing in the vault says; the final report is due December 4 and has no score.",
        (),
        (GRADES,),
    ),
    _q(
        "not_in_vault",
        "What is Dr. Raman's phone number?",
        "Not in the vault; only an email address is given.",
        (),
        (),
    ),
    _q(
        "not_in_vault",
        "What is Jordan Lee's email address?",
        "Not in the vault.",
        (),
        (),
    ),
    _q(
        "not_in_vault",
        "When is the Operating Systems midterm exam?",
        "Not in the vault; only the final exam date is given.",
        (),
        (STUDY,),
    ),
    _q(
        "not_in_vault",
        "What is the web address of the team's code repository?",
        "Not in the vault.",
        (),
        (),
    ),
    _q(
        "not_in_vault",
        "How many students are enrolled in CSE 485?",
        "Not in the vault.",
        (),
        (SYLLABUS,),
    ),
    _q(
        "not_in_vault",
        "In which room is the Calculus II final?",
        "Not in the vault; only the date and time are given.",
        (),
        (STUDY, CALENDAR),
    ),
    _q(
        "not_in_vault",
        "How much did the team spend on domain registration for next year?",
        "Not in the vault; the budget only lists this year's domain name cost.",
        (),
        (BUDGET,),
    ),
    _q(
        "not_in_vault",
        "What did Sam say at the November 20 demo?",
        "Not in the vault.",
        (),
        (CHAT,),
    ),
]


def build_questions() -> list[Question]:
    return [
        Question(
            id=f"q{i:03d}",
            question=question,
            category=category,
            answer=answer,
            sources=sources,
            alternatives=alternatives,
            distractors=distractors,
        )
        for i, (category, question, answer, sources, distractors, alternatives) in enumerate(
            _RAW, start=1
        )
    ]
