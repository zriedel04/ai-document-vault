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
    sources: tuple[Source, ...] = ()
    distractors: tuple[str, ...] = ()  # documents that look relevant but are outdated or wrong
    tags: tuple[str, ...] = field(default=())

    @property
    def answerable(self) -> bool:
        return self.category != "not_in_vault"


def s(path: str, quote: str, page: int | None = None) -> Source:
    return Source(path, quote, page)


_RAW: list[tuple[Category, str, str, tuple[Source, ...], tuple[str, ...]]] = [
    # --- lookup: course documents -----------------------------------------------------------
    (
        "lookup",
        "Who teaches CSE 485 and what is their email address?",
        "Dr. Priya Raman, priya.raman@lit.example.",
        (s(SYLLABUS, "Instructor: Dr. Priya Raman, priya.raman@lit.example.", 1),),
        (),
    ),
    (
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
    (
        "lookup",
        "When are the instructor's office hours?",
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
    (
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
    (
        "lookup",
        "What share of the final grade does the final report make up?",
        "25 percent.",
        (s(SYLLABUS, "Final report: 25 percent", 2),),
        (),
    ),
    (
        "lookup",
        "What is the penalty for handing work in late?",
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
    (
        "lookup",
        "Is there a final exam in the course?",
        "No, there is no final exam.",
        (s(SYLLABUS, "There is no final exam in this course.", 3),),
        (),
    ),
    (
        "lookup",
        "When is the design review?",
        "Friday, October 30, 2026.",
        (
            s(SYLLABUS, "Design review: Friday, October 30, 2026", 3),
            s(REVIEW, "Design review held on Friday, October 30, 2026."),
        ),
        (),
    ),
    (
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
    (
        "lookup",
        "In what format and where do I submit the project proposal?",
        "As a single PDF file through the course portal.",
        (s(ASSIGN1, "Submit the proposal as a single PDF file through the course portal.", 2),),
        (),
    ),
    (
        "lookup",
        "How long can the project proposal be, and what happens if it is longer?",
        "No more than two pages; each extra page loses 5 points.",
        (
            s(ASSIGN1, "no more than two pages", 1),
            s(ASSIGN1, "A proposal that exceeds two pages loses 5 points for each extra page.", 2),
        ),
        (),
    ),
    (
        "lookup",
        "In MoSCoW prioritisation, what does 'could have' mean?",
        "Nice to have if time allows.",
        (s(LECTURE4, "Could have: nice to have if time allows", 3),),
        (),
    ),
    (
        "lookup",
        "What are the qualities of a good requirement?",
        "It is unambiguous, testable, necessary and feasible.",
        (s(LECTURE4, "A good requirement is unambiguous, testable, necessary and feasible.", 2),),
        (),
    ),
    (
        "lookup",
        "Which database did Jordan choose for the prototype?",
        "SQLite.",
        (s(STANDUP_0915, "Jordan: chose SQLite for the prototype."),),
        (),
    ),
    (
        "lookup",
        "What happens if the team vote on a disagreement is tied?",
        "The team asks Dr. Raman.",
        (s(CONTRACT, "If the vote is tied, the team asks Dr. Raman."),),
        (),
    ),
    (
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
    (
        "lookup",
        "What does the API return when a group does not exist?",
        "404.",
        (s(API, "A request for a group that does not exist returns 404."),),
        (),
    ),
    (
        "lookup",
        "When is the Operating Systems final?",
        "Wednesday, December 9 at 1:00 PM.",
        (
            s(STUDY, "Wednesday, December 9 at 1:00 PM: Operating Systems final"),
            s(CALENDAR, "Wed Dec 9: Operating Systems final at 1:00 PM"),
        ),
        (),
    ),
    # --- multi_doc and reasoning ------------------------------------------------------------
    (
        "multi_doc",
        "Why did Alex ask the instructor for more time on Assignment 2?",
        "Sam's laptop screen broke, the repair would take five business days, and Sam held the latest draft.",
        (
            s(
                MAIL_ASK,
                "Our teammate Sam's laptop screen broke today and the repair shop says the repair will take five business days. Sam holds the latest draft of our requirements specification.",
            ),
            s(JOURNAL, "Sam's laptop screen broke. The repair shop quoted five business days."),
        ),
        (),
    ),
    (
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
    (
        "multi_doc",
        "Does the approved extension apply to the other assignments too?",
        "No, it applies to Assignment 2 only.",
        (s(MAIL_REPLY, "This applies to Assignment 2 only."),),
        (),
    ),
    (
        "multi_doc",
        "What did the instructor ask the team to put on the first page of the specification after the extension?",
        "A short note saying that the deadline was extended.",
        (
            s(
                MAIL_REPLY,
                "Please add a short note on the first page of the specification that says the deadline was extended.",
            ),
            s(
                REQ_V2,
                "The deadline for this assignment was extended to October 12, 2026 by the instructor.",
            ),
        ),
        (),
    ),
    (
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
    (
        "multi_doc",
        "What was the design review score and what did the reviewer ask the team to add?",
        "92 out of 100; an index on the course code column for the group search query.",
        (
            s(REVIEW, "Design review score: 92 out of 100."),
            s(
                REVIEW,
                "The group search query needs an index on the course code column before the demo.",
            ),
        ),
        (),
    ),
    (
        "multi_doc",
        "How did the decision about in-app chat develop over time?",
        "It was an open decision on September 22 and again at the October 2 meeting, and chat was removed from the first release in version 2 of the requirements on October 11.",
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
    (
        "multi_doc",
        "Which teammate's computer broke and what did the repair cost in total?",
        "Sam Patel's laptop; the repair cost $187.92.",
        (
            s(JOURNAL, "Sam's laptop screen broke."),
            s(REPAIR, "Customer: Sam Patel"),
            s(REPAIR, "Total $187.92"),
        ),
        (),
    ),
    # --- version conflicts ------------------------------------------------------------------
    (
        "version",
        "What is the maximum number of members in a study group in the current requirements?",
        "10 members (version 2; version 1 said 8).",
        (s(REQ_V2, "NFR-3: A study group shall have a maximum of 10 members."),),
        (REQ_V1,),
    ),
    (
        "version",
        "How quickly must search results load according to the submitted specification?",
        "Within 2 seconds (version 1 said 3 seconds).",
        (s(REQ_V2, "NFR-1: Search results shall load within 2 seconds."),),
        (REQ_V1,),
    ),
    (
        "version",
        "How many concurrent users must the system support?",
        "500 (version 2). Version 1 and the project journal mention 200.",
        (s(REQ_V2, "NFR-2: The system shall support 500 concurrent users."),),
        (REQ_V1, JOURNAL),
    ),
    (
        "version",
        "Is in-app chat part of the first release?",
        "No. It was in version 1 but version 2 lists it as out of scope.",
        (
            s(
                REQ_V2,
                "Payments, video calls, in-app chat and a native mobile application are out of scope.",
            ),
            s(
                JOURNAL,
                "We removed in-app chat from the first release because it would take too long to build.",
            ),
        ),
        (REQ_V1,),
    ),
    (
        "version",
        "Which requirements were added between version 1 and version 2 of the specification?",
        "Exporting a meeting as an iCalendar file, leaving a group, removing a member, reporting inappropriate content, importing a course list from CSV (FR-8 to FR-12) and WCAG 2.1 AA contrast (NFR-6).",
        (
            s(
                REQ_V2,
                "FR-8: The system shall let a student export a scheduled meeting as an iCalendar file.",
            ),
            s(
                REQ_V2,
                "FR-12: The system shall let an administrator import the course list from a CSV file.",
            ),
            s(REQ_V2, "NFR-6: All pages shall meet the WCAG 2.1 level AA contrast requirements."),
        ),
        (REQ_V1,),
    ),
    (
        "version",
        "When is Assignment 2 due for Team Northwind?",
        "Monday, October 12, 2026 at 11:59 PM, after the approved extension (the syllabus says October 9).",
        (
            s(
                MAIL_REPLY,
                "Your team may submit Assignment 2 by Monday, October 12 at 11:59 PM with no late penalty.",
            ),
            s(
                REQ_V2,
                "The deadline for this assignment was extended to October 12, 2026 by the instructor.",
            ),
        ),
        (SYLLABUS, ASSIGN2),
    ),
    (
        "version",
        "When was version 2 of the requirements submitted?",
        "October 11, 2026.",
        (
            s(REQ_V2, "Version 2.0 (submitted), October 11, 2026."),
            s(JOURNAL, "Submitted version 2 of the requirements."),
        ),
        (REQ_V1,),
    ),
    # --- ocr: only in scans and pictures ----------------------------------------------------
    (
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
    (
        "ocr",
        "How many groups will Jordan use to test search speed?",
        "1000 groups.",
        (s(MEETING, "- Jordan will test search with 1000 groups", 2),),
        (),
    ),
    (
        "ocr",
        "When and where was the next team meeting after October 2 held?",
        "Thursday, October 8 at 4:30 PM in library study room 3.",
        (
            s(MEETING, "Next meeting: Thursday, October 8 at 4:30 PM", 2),
            s(MEETING, "Place: library study room 3", 2),
        ),
        (),
    ),
    (
        "ocr",
        "What was Alex's action item at the October 2 meeting?",
        "Send requirements version 2 to the team by October 8.",
        (s(MEETING, "- Alex: send requirements version 2 to the team by October 8", 1),),
        (),
    ),
    (
        "ocr",
        "Which npm module could the build not find?",
        "express-session.",
        (s(BUILD_ERROR, "Module not found: Error: Can't resolve 'express-session'"),),
        (),
    ),
    (
        "ocr",
        "Which command did Alex run to fix the build error?",
        "npm install express-session",
        (s(BUILD_ERROR, "alex@laptop:~/studybuddy$ npm install express-session"),),
        (),
    ),
    (
        "ocr",
        "Which tasks are in progress on the sprint board and who owns them?",
        "Group page layout (Sam) and the join request endpoint (Jordan).",
        (
            s(KANBAN, "Group page layout (Sam)"),
            s(KANBAN, "Join request endpoint (Jordan)"),
        ),
        (),
    ),
    (
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
    (
        "numeric",
        "How much did the textbook cost at the bookstore including tax?",
        "$97.15 ($89.95 plus $7.20 tax).",
        (
            s(BOOKS, "Software Engineering in Practice 4th Ed $89.95"),
            s(BOOKS, "Total $97.15"),
        ),
        (),
    ),
    (
        "numeric",
        "Does the price on the bookstore receipt match the price in the syllabus?",
        "Yes, both give 89.95 dollars before tax.",
        (
            s(SYLLABUS, "The bookstore lists it at 89.95 dollars.", 3),
            s(BOOKS, "Software Engineering in Practice 4th Ed $89.95"),
        ),
        (),
    ),
    (
        "numeric",
        "What was the laptop repair bill before tax?",
        "$174.00 ($129.00 for the screen plus $45.00 labor).",
        (s(REPAIR, "Subtotal $174.00"),),
        (),
    ),
    (
        "numeric",
        "What is the total of the team's project budget?",
        "$82.90.",
        (s(BUDGET, "Total,82.90"),),
        (),
    ),
    (
        "numeric",
        "Does the poster printing receipt agree with the project budget?",
        "Yes, both show $7.50.",
        (
            s(PRINTING, "Poster prints 1 x $7.50"),
            s(BUDGET, "Poster printing,7.50"),
        ),
        (),
    ),
    (
        "numeric",
        "How many hours in total did Jordan log over the six weeks?",
        "41 hours.",
        (s(HOURS, "total,36,41,27"),),
        (),
    ),
    (
        "numeric",
        "In which week did Sam log the fewest hours?",
        "Week 5, with 2 hours.",
        (s(HOURS, "5,6,9,2"),),
        (),
    ),
    # --- paraphrase: few shared words -------------------------------------------------------
    (
        "paraphrase",
        "Who got the team's laptop fixed at a shop?",
        "Sam Patel, at Quick Fix Computers.",
        (
            s(REPAIR, "QUICK FIX COMPUTERS"),
            s(REPAIR, "Customer: Sam Patel"),
        ),
        (),
    ),
    (
        "paraphrase",
        "What is the rule if I turn work in a week after the deadline without asking?",
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
    (
        "paraphrase",
        "Which framework was Jordan leaning toward at the start?",
        "Flask.",
        (s(STANDUP_0908, "Jordan: comparing web frameworks. Leaning toward Flask."),),
        (),
    ),
    # --- not in the vault -------------------------------------------------------------------
    (
        "not_in_vault",
        "What grade did the team get on the final report?",
        "Nothing in the vault says; the final report is due December 4 and has no score.",
        (),
        (GRADES,),
    ),
    (
        "not_in_vault",
        "What is Dr. Raman's phone number?",
        "Not in the vault; only an email address is given.",
        (),
        (),
    ),
    (
        "not_in_vault",
        "What is Jordan Lee's email address?",
        "Not in the vault.",
        (),
        (),
    ),
    (
        "not_in_vault",
        "When is the Operating Systems midterm exam?",
        "Not in the vault; only the final exam date is given.",
        (),
        (STUDY,),
    ),
    (
        "not_in_vault",
        "What is the web address of the team's code repository?",
        "Not in the vault.",
        (),
        (),
    ),
    (
        "not_in_vault",
        "How many students are enrolled in CSE 485?",
        "Not in the vault.",
        (),
        (SYLLABUS,),
    ),
    (
        "not_in_vault",
        "In which room is the Calculus II final?",
        "Not in the vault; only the date and time are given.",
        (),
        (STUDY, CALENDAR),
    ),
    (
        "not_in_vault",
        "How much did the team spend on domain registration for next year?",
        "Not in the vault; the budget only lists this year's domain name cost.",
        (),
        (BUDGET,),
    ),
    (
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
            distractors=distractors,
        )
        for i, (category, question, answer, sources, distractors) in enumerate(_RAW, start=1)
    ]
