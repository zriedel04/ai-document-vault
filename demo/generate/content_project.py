"""Project documents for the fictional team and student. All names and facts are invented."""

from __future__ import annotations

from model import Document, h1, h2, li, p

_HEADER_V1 = "Version 1.0 (draft), September 28, 2026. Team Northwind: Alex Rivera, Jordan Lee and Sam Patel."
_HEADER_V2 = "Version 2.0 (submitted), October 11, 2026. Team Northwind: Alex Rivera, Jordan Lee and Sam Patel."
_PURPOSE = "StudyBuddy helps university students find and join study groups for the courses they are taking."

REQUIREMENTS_V1 = Document(
    path="project/requirements-v1.docx",
    kind="docx",
    blocks=(
        (
            h1("StudyBuddy Requirements Specification"),
            p(_HEADER_V1),
            h2("1. Purpose"),
            p(_PURPOSE),
            h2("2. Functional requirements"),
            li(
                "FR-1: The system shall let a student create an account using a university email address."
            ),
            li("FR-2: The system shall let a student create a study group for a course."),
            li("FR-3: The system shall let a student search for study groups by course code."),
            li("FR-4: The system shall let a student request to join a study group."),
            li("FR-5: The system shall let the group owner approve or decline join requests."),
            li("FR-6: The system shall provide an in-app chat for the members of a group."),
            li(
                "FR-7: The system shall let a group owner schedule a meeting with a date, time and place."
            ),
            li(
                "FR-8: The system shall send an email reminder 24 hours before a scheduled meeting."
            ),
            h2("3. Non-functional requirements"),
            li("NFR-1: Search results shall load within 3 seconds."),
            li("NFR-2: The system shall support 200 concurrent users."),
            li("NFR-3: A study group shall have a maximum of 8 members."),
            li("NFR-4: The system shall be available 99 percent of the time during the semester."),
            li("NFR-5: Passwords shall be stored only as salted hashes."),
            h2("4. Out of scope"),
            p("Payments, video calls and a native mobile application are out of scope."),
        ),
    ),
)

REQUIREMENTS_V2 = Document(
    path="project/requirements-v2.docx",
    kind="docx",
    blocks=(
        (
            h1("StudyBuddy Requirements Specification"),
            p(_HEADER_V2),
            p(
                "The deadline for this assignment was extended to October 12, 2026 by the instructor."
            ),
            h2("1. Purpose"),
            p(_PURPOSE),
            h2("2. Functional requirements"),
            li(
                "FR-1: The system shall let a student create an account using a university email address."
            ),
            li("FR-2: The system shall let a student create a study group for a course."),
            li("FR-3: The system shall let a student search for study groups by course code."),
            li("FR-4: The system shall let a student request to join a study group."),
            li("FR-5: The system shall let the group owner approve or decline join requests."),
            li(
                "FR-6: The system shall let a group owner schedule a meeting with a date, time and place."
            ),
            li(
                "FR-7: The system shall send an email reminder 24 hours before a scheduled meeting."
            ),
            li(
                "FR-8: The system shall let a student export a scheduled meeting as an iCalendar file."
            ),
            li("FR-9: The system shall let a student leave a study group."),
            li("FR-10: The system shall let a group owner remove a member from the group."),
            li(
                "FR-11: The system shall let a student report inappropriate content to an administrator."
            ),
            li(
                "FR-12: The system shall let an administrator import the course list from a CSV file."
            ),
            h2("3. Non-functional requirements"),
            li("NFR-1: Search results shall load within 2 seconds."),
            li("NFR-2: The system shall support 500 concurrent users."),
            li("NFR-3: A study group shall have a maximum of 10 members."),
            li("NFR-4: The system shall be available 99 percent of the time during the semester."),
            li("NFR-5: Passwords shall be stored only as salted hashes."),
            li("NFR-6: All pages shall meet the WCAG 2.1 level AA contrast requirements."),
            h2("4. Out of scope"),
            p(
                "Payments, video calls, in-app chat and a native mobile application are out of scope."
            ),
        ),
    ),
    near_duplicate_of="project/requirements-v1.docx",
    note="Second version of the requirements: several numbers and requirements differ from v1",
)

REQUIREMENTS_V2_COPY = Document(
    path="project/requirements-v2-copy.docx",
    kind="docx",
    blocks=REQUIREMENTS_V2.blocks,
    exact_duplicate_of="project/requirements-v2.docx",
    note="Byte-for-byte copy of requirements-v2.docx",
)

DESIGN_REVIEW = Document(
    path="project/design-review-feedback.docx",
    kind="docx",
    blocks=(
        (
            h1("Design Review Feedback"),
            p(
                "Team Northwind, project StudyBuddy. Design review held on Friday, October 30, 2026. Reviewer: Dr. Priya Raman."
            ),
            h2("Strengths"),
            li("The traceability table links every functional requirement to a user need."),
            li("The screen mockups are consistent and easy to follow."),
            h2("Concerns"),
            li("The group search query needs an index on the course code column before the demo."),
            li("The prototype demo on November 20 must support at least 3 simultaneous sessions."),
            li("The team has not yet described how it will test the email reminders."),
            h2("Score"),
            p("Design review score: 92 out of 100."),
        ),
    ),
)

SIGNOFF_SCAN = Document(
    path="scans/advisor-signoff-form-scan.pdf",
    kind="pdf_scan",
    lines=(
        (
            "LAKESHORE INSTITUTE OF TECHNOLOGY",
            "CSE 485 PROJECT APPROVAL FORM",
            "",
            "Team name: Team Northwind",
            "Project title: StudyBuddy",
            "Team members: Alex Rivera, Jordan Lee, Sam Patel",
            "",
            "Decision: APPROVED",
            "Approved by Dr. Priya Raman on September 22, 2026",
            "",
            "Conditions:",
            "- A team of three students is approved",
            "- The scope is limited to a web application",
            "- Any change of scope needs written approval",
        ),
    ),
    seed=11,
    note="Scanned-style PDF: an image only, no text layer",
)

MEETING_NOTES_SCAN = Document(
    path="scans/team-meeting-notes-scan.pdf",
    kind="pdf_scan",
    lines=(
        (
            "TEAM NORTHWIND MEETING NOTES",
            "Date: Friday, October 2, 2026",
            "Present: Alex, Jordan, Sam",
            "",
            "Decisions",
            "- Jordan owns the API and the database",
            "- Sam owns the screen mockups and testing",
            "- Alex owns the documentation and the requirements spec",
            "- The team chat stays on Discord",
            "",
            "Action items",
            "- Alex: send requirements version 2 to the team by October 8",
            "- Jordan: write the join request endpoint",
            "- Sam: finish the group page mockup",
        ),
        (
            "Risks",
            "- Search may be slow when there are many groups",
            "- Jordan will test search with 1000 groups",
            "",
            "Open question",
            "- Should the first release include in-app chat?",
            "",
            "Next meeting: Thursday, October 8 at 4:30 PM",
            "Place: library study room 3",
        ),
    ),
    seed=12,
    note="Scanned-style PDF: an image only, two pages",
)

EXTENSION_REQUEST = Document(
    path="emails/advisor-extension-request.txt",
    kind="txt",
    text="""From: Alex Rivera <alex.rivera@lit.example>
To: Dr. Priya Raman <priya.raman@lit.example>
Date: Sat, Oct 3, 2026 at 6:12 PM
Subject: Extension request for Assignment 2

Dear Dr. Raman,

Our teammate Sam's laptop screen broke today and the repair shop says the repair will take five business days. Sam holds the latest draft of our requirements specification.

May our team have until Monday, October 12 at 11:59 PM to submit Assignment 2? The syllabus asks for extension requests at least 48 hours before the deadline, and this request is sent six days before it.

Thank you,
Alex Rivera
""",
)

EXTENSION_REPLY = Document(
    path="emails/advisor-reply.txt",
    kind="txt",
    text="""From: Dr. Priya Raman <priya.raman@lit.example>
To: Alex Rivera <alex.rivera@lit.example>
Date: Sun, Oct 4, 2026 at 9:40 AM
Subject: Re: Extension request for Assignment 2

Alex,

The extension is approved. Your team may submit Assignment 2 by Monday, October 12 at 11:59 PM with no late penalty. This applies to Assignment 2 only.

Please add a short note on the first page of the specification that says the deadline was extended.

Best,
Priya Raman
""",
)

STANDUP_0908 = Document(
    path="notes/standup-2026-09-08.txt",
    kind="txt",
    text="""Team Northwind standup, Tuesday, September 8, 2026
Attendees: Alex, Jordan, Sam

Alex: drafting the outline of the project proposal.
Jordan: comparing web frameworks. Leaning toward Flask.
Sam: sketching the first screens.
Blockers: none.
""",
)

STANDUP_0915 = Document(
    path="notes/standup-2026-09-15.txt",
    kind="txt",
    text="""Team Northwind standup, Tuesday, September 15, 2026
Attendees: Alex, Jordan, Sam

Alex: proposal draft is done and under review by the team.
Jordan: chose SQLite for the prototype.
Sam: finished the sketch of the search screen.
Blockers: Jordan needs access to the course portal repository.
""",
)

STANDUP_0915_EDITED = Document(
    path="notes/standup-2026-09-15-edited.txt",
    kind="txt",
    text="""Team Northwind standup, Tuesday, September 15, 2026
Attendees: Alex, Jordan, Sam

Alex: proposal draft is done and has been reviewed by the team.
Jordan: chose SQLite for the prototype.
Sam: finished the sketch of the search screen.
Blockers: Jordan needs access to the course repository.
""",
    near_duplicate_of="notes/standup-2026-09-15.txt",
    note="Edited copy of the September 15 standup with two small wording changes",
)

STANDUP_0922 = Document(
    path="notes/standup-2026-09-22.txt",
    kind="txt",
    text="""Team Northwind standup, Tuesday, September 22, 2026
Attendees: Alex, Jordan, Sam

Alex: the proposal was submitted on September 17. Dr. Raman approved our team today.
Jordan: the API skeleton is running with two endpoints.
Sam: set up the kanban board.
Blockers: we need to decide whether the first release has in-app chat.
""",
)

READING_LIST = Document(
    path="notes/reading-list.txt",
    kind="txt",
    text="""CSE 485 reading list

Software Engineering in Practice, chapters 1 and 2: read by September 8
Software Engineering in Practice, chapters 3 and 4: read by September 15
Software Engineering in Practice, chapter 5 on requirements: read by September 24
Software Engineering in Practice, chapter 6 on design: read by October 8
Article: "Writing testable requirements": read by October 1
""",
)

TEAM_CONTRACT = Document(
    path="notes/team-contract.md",
    kind="md",
    text="""# Team Northwind Contract

## Roles

- Alex Rivera: project lead, documentation
- Jordan Lee: backend and database
- Sam Patel: design and testing

## Meetings

The team meets every Thursday at 4:30 PM in the library. Short standups happen on Tuesdays.

## Communication

The team chat is on Discord. A message that needs an answer is replied to within 24 hours.

## Conflict resolution

If two members disagree, they first try to settle it between themselves. If that does not work, the whole team votes. If the vote is tied, the team asks Dr. Raman.
""",
)

PROJECT_JOURNAL = Document(
    path="notes/project-journal.md",
    kind="md",
    text="""# StudyBuddy Project Journal

## September 3

Formed the team. We chose the idea of a study group matcher and named it StudyBuddy.

## September 17

Submitted the project proposal. We listed three core features: create a group, search for groups and schedule meetings.

## September 28

Finished the first draft of the requirements specification. Jordan thinks 200 concurrent users is plenty for a prototype.

## October 3

Sam's laptop screen broke. The repair shop quoted five business days. I emailed Dr. Raman to ask for an extension on Assignment 2.

## October 11

Submitted version 2 of the requirements. We removed in-app chat from the first release because it would take too long to build.

## October 30

Design review went well. Dr. Raman gave us a 92 and asked us to add a database index for search.
""",
)

STUDY_PLAN = Document(
    path="notes/study-plan.md",
    kind="md",
    text="""# Study Plan for Finals Week

## Dates

- Tuesday, December 8 at 9:00 AM: Calculus II final
- Wednesday, December 9 at 1:00 PM: Operating Systems final
- Thursday, December 10 at 5:00 PM: capstone celebration

## Plan

Review Calculus II on December 5 and 6 using the practice exams. Spend December 7 on Operating Systems scheduling and memory chapters. Sleep at least seven hours before each exam.
""",
)

API_NOTES = Document(
    path="notes/api-design-notes.md",
    kind="md",
    text="""# StudyBuddy API Design Notes

## Endpoints

- GET /groups: list study groups, filtered by course code
- POST /groups: create a study group
- POST /groups/{id}/requests: ask to join a group
- GET /meetings/{id}/calendar: download an iCalendar file

## Pagination

List endpoints return 20 items by default. A client may ask for at most 100 items per page with the limit parameter.

## Errors

Errors use standard HTTP status codes. A request for a group that does not exist returns 404.
""",
)

GRADES_CSV = Document(
    path="data/grades.csv",
    kind="csv",
    text="""assessment,weight_percent,score,out_of
Project proposal,10,88,100
Requirements specification,15,90,100
Design review,15,92,100
Prototype demo,20,95,100
Peer evaluation,5,96,100
""",
)

BUDGET_CSV = Document(
    path="data/project-budget.csv",
    kind="csv",
    text="""item,cost_usd
Domain name,12.00
Hosting for three months,45.00
Poster printing,7.50
Demo day snacks,18.40
Total,82.90
""",
)

HOURS_CSV = Document(
    path="data/team-hours.csv",
    kind="csv",
    text="""week,alex_hours,jordan_hours,sam_hours
1,4,5,3
2,5,6,4
3,6,6,5
4,7,8,6
5,6,9,2
6,8,7,7
total,36,41,27
""",
)

DOCS = [
    REQUIREMENTS_V1,
    REQUIREMENTS_V2,
    REQUIREMENTS_V2_COPY,
    DESIGN_REVIEW,
    SIGNOFF_SCAN,
    MEETING_NOTES_SCAN,
    EXTENSION_REQUEST,
    EXTENSION_REPLY,
    STANDUP_0908,
    STANDUP_0915,
    STANDUP_0915_EDITED,
    STANDUP_0922,
    READING_LIST,
    TEAM_CONTRACT,
    PROJECT_JOURNAL,
    STUDY_PLAN,
    API_NOTES,
    GRADES_CSV,
    BUDGET_CSV,
    HOURS_CSV,
]
