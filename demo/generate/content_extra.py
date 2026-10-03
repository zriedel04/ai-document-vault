"""More documents for the fictional student's other courses and life. They add variety and
distractors for retrieval: similar kinds of document about different things. All content is
invented."""

from __future__ import annotations

from model import Document, h1, h2, li, p

MATH_SYLLABUS = Document(
    path="courses/math241/syllabus.pdf",
    kind="pdf_text",
    blocks=(
        (
            h1("MATH 241: Calculus II - Course Syllabus"),
            p("Lakeshore Institute of Technology, Department of Mathematics. Fall 2026."),
            h2("Course information"),
            p(
                "The course meets on Mondays, Wednesdays and Fridays from 9:00 AM to 9:50 AM. Instructor: Prof. Daniel Okafor, daniel.okafor@lit.example. His office hours are on Thursdays from 3:00 PM to 5:00 PM."
            ),
            h2("Topics"),
            li("Techniques of integration"),
            li("Improper integrals and numerical integration"),
            li("Sequences and series"),
            li("Parametric curves and polar coordinates"),
        ),
        (
            h2("Grading"),
            li("Homework: 20 percent"),
            li("Three unit exams: 15 percent each"),
            li("Final exam: 35 percent"),
            p(
                "Homework is submitted on the course portal every Monday. The lowest homework score is dropped."
            ),
            h2("Final exam"),
            p("The final exam is on Tuesday, December 8, 2026 at 9:00 AM."),
            p("A scientific calculator is allowed. Graphing calculators are not."),
            h2("Required textbook"),
            p(
                "Calculus: Early Transcendentals, 9th edition, by L. Brandt. The bookstore lists it at 112.50 dollars."
            ),
        ),
    ),
)

OS_SYLLABUS = Document(
    path="courses/cs350/syllabus.pdf",
    kind="pdf_text",
    blocks=(
        (
            h1("CS 350: Operating Systems - Course Syllabus"),
            p("Lakeshore Institute of Technology, Department of Computer Science. Fall 2026."),
            h2("Course information"),
            p(
                "The course meets on Mondays and Wednesdays from 11:00 AM to 12:15 PM. Instructor: Prof. Mei Tanaka, mei.tanaka@lit.example. Her office hours are on Tuesdays from 1:00 PM to 3:00 PM."
            ),
            h2("Topics"),
            li("Processes and threads"),
            li("CPU scheduling"),
            li("Memory management and virtual memory"),
            li("File systems"),
        ),
        (
            h2("Grading"),
            li("Programming projects: 40 percent"),
            li("Two in-class quizzes: 10 percent each"),
            li("Final exam: 40 percent"),
            h2("Final exam"),
            p("The final exam is on Wednesday, December 9, 2026 at 1:00 PM."),
            p("One sheet of handwritten notes is allowed."),
            h2("Projects"),
            p(
                "There are four programming projects. Each is done individually and submitted through the course portal."
            ),
        ),
    ),
)

LECTURE_5 = Document(
    path="courses/cse485/lecture-05-slide-notes.pdf",
    kind="pdf_text",
    blocks=(
        (
            h1("Lecture 5 Slide Notes: Design and Reviews"),
            h2("Slide 1: From requirements to design"),
            p("Every design decision should trace back to at least one requirement."),
            h2("Slide 2: Layers"),
            li("Presentation: what the user sees"),
            li("Logic: the rules of the application"),
            li("Data: where information is stored"),
        ),
        (
            h2("Slide 3: Interfaces first"),
            p(
                "Agree on the interface between two components before either one is built. Write down inputs, outputs and errors."
            ),
            h2("Slide 4: Running a design review"),
            p(
                "A design review is a meeting where the team explains its design and a reviewer asks questions. Bring a diagram, a traceability table and a list of open risks."
            ),
            p("The design reviews in this course are on October 30."),
        ),
    ),
)

PROPOSAL = Document(
    path="project/project-proposal.docx",
    kind="docx",
    blocks=(
        (
            h1("StudyBuddy Project Proposal"),
            p(
                "Team Northwind: Alex Rivera, Jordan Lee and Sam Patel. Submitted September 17, 2026."
            ),
            h2("Problem statement"),
            p(
                "Students who want to study together often cannot find classmates who take the same courses. Messages in large group chats get lost."
            ),
            h2("Target users"),
            p(
                "University students who take courses with large enrollment and prefer to study in small groups."
            ),
            h2("Core features"),
            li("Create a study group for a course"),
            li("Search for study groups by course code"),
            li("Schedule meetings for a group"),
            h2("Risks"),
            li("The team has not built a web application before."),
            li("Search may become slow as the number of groups grows."),
            li("Three members have different class schedules, so meetings are hard to plan."),
        ),
    ),
)

STANDUP_0929 = Document(
    path="notes/standup-2026-09-29.txt",
    kind="txt",
    text="""Team Northwind standup, Tuesday, September 29, 2026
Attendees: Alex, Jordan, Sam

Alex: the first draft of the requirements specification is finished and shared.
Jordan: started the join request endpoint.
Sam: working on the mockup of the group page.
Blockers: none.
""",
)

STANDUP_1006 = Document(
    path="notes/standup-2026-10-06.txt",
    kind="txt",
    text="""Team Northwind standup, Tuesday, October 6, 2026
Attendees: Alex, Jordan, Sam

Alex: collecting feedback on the requirements from the peer review in class.
Jordan: still working on the join request endpoint.
Sam: borrowing a library computer until the laptop repair is done.
Blockers: Sam cannot run the prototype locally.
""",
)

DEMO_DAY = Document(
    path="emails/class-announcement-demo-day.txt",
    kind="txt",
    text="""From: Dr. Priya Raman <priya.raman@lit.example>
To: CSE 485 class list <cse485-all@lit.example>
Date: Mon, Nov 9, 2026 at 8:05 AM
Subject: Prototype demo logistics

Hello everyone,

Each team has 12 minutes for its prototype demo on Friday, November 20: 8 minutes to present and 4 minutes for questions. Sign up for a time slot on the course portal by Friday, November 13.

Please bring a laptop that can connect to the projector, and test it before the session.

Dr. Raman
""",
)

SCHEDULE_CSV = Document(
    path="data/class-schedule.csv",
    kind="csv",
    text="""course,days,start,end
CSE 485 Capstone Project,Tue Thu,14:00,15:15
CS 350 Operating Systems,Mon Wed,11:00,12:15
MATH 241 Calculus II,Mon Wed Fri,09:00,09:50
""",
)

LIBRARY_NOTICE = Document(
    path="scans/library-overdue-notice-scan.pdf",
    kind="pdf_scan",
    lines=(
        (
            "LAKESHORE INSTITUTE LIBRARY",
            "OVERDUE NOTICE",
            "",
            "Borrower: Alex Rivera",
            "Item: Practical Databases, 2nd edition",
            "Due date: October 14, 2026",
            "Returned: October 19, 2026",
            "Late fee: 1.25 dollars",
            "",
            "The fee was added to the student account.",
        ),
    ),
    seed=13,
    note="Scanned-style PDF: an image only, no text layer",
)

COFFEE_COPY = Document(
    path="receipts/coffee-receipt-copy.jpg",
    kind="jpg",
    layout="receipt",
    seed=22,
    lines=(
        (
            "BEAN THERE CAFE",
            "Date: 2026-09-15",
            "Large latte\t$5.20",
            "Blueberry muffin\t$4.20",
            "Total\t$9.40",
            "Paid by cash",
        ),
    ),
    exact_duplicate_of="receipts/coffee-receipt.jpg",
    note="Byte-for-byte copy of coffee-receipt.jpg",
)

REPORT_OUTLINE = Document(
    path="notes/final-report-outline.md",
    kind="md",
    text="""# Final Report Outline

Due Friday, December 4. Target length: 20 pages.

## Sections

- Introduction and goals
- Requirements, with the changes between version 1 and version 2
- Design and architecture
- Testing and results
- Lessons learned

## To do

- Collect screenshots of the finished screens
- Ask Sam for the test results table
- Add the search timing numbers after the index is built
""",
)

DOCS = [
    MATH_SYLLABUS,
    OS_SYLLABUS,
    LECTURE_5,
    PROPOSAL,
    STANDUP_0929,
    STANDUP_1006,
    DEMO_DAY,
    SCHEDULE_CSV,
    LIBRARY_NOTICE,
    COFFEE_COPY,
    REPORT_OUTLINE,
]
