"""Course documents for the fictional CSE 485 capstone course. All names and facts are invented."""

from __future__ import annotations

from model import Document, h1, h2, li, p

SYLLABUS = Document(
    path="courses/cse485/syllabus.pdf",
    kind="pdf_text",
    blocks=(
        (
            h1("CSE 485: Capstone Project - Course Syllabus"),
            p("Lakeshore Institute of Technology, Department of Computer Science. Fall 2026."),
            h2("Course information"),
            p(
                "The course runs from September 1 to December 11, 2026. Class meets on Tuesdays and Thursdays from 2:00 PM to 3:15 PM in Room 214 of Harbor Hall."
            ),
            p(
                "Instructor: Dr. Priya Raman, priya.raman@lit.example. Office hours are on Wednesdays from 10:00 AM to 12:00 PM in Room 318 of Harbor Hall."
            ),
            p(
                "Teaching assistant: Marcus Bell. His office hours are on Fridays from 1:00 PM to 3:00 PM, online."
            ),
            h2("Description"),
            p(
                "In this course, teams of three or four students design, build and demonstrate a software product for a real or realistic user. The course emphasises requirements, design reviews and working in a team."
            ),
            h2("Learning outcomes"),
            li("Write testable requirements and trace them to design decisions."),
            li("Plan and track work in a small team using an agile board."),
            li("Present a working prototype and defend design choices."),
        ),
        (
            h2("Grading"),
            p("The final grade is made up of the following parts."),
            li("Project proposal: 10 percent"),
            li("Requirements specification: 15 percent"),
            li("Design review: 15 percent"),
            li("Prototype demo: 20 percent"),
            li("Final report: 25 percent"),
            li("Peer evaluation: 5 percent"),
            li("Participation: 10 percent"),
            h2("Late work"),
            p(
                "Late work loses 10 percent per day, up to five days. After five days the work is not accepted."
            ),
            p(
                "Extension requests must be sent to the instructor at least 48 hours before the deadline. An extension is granted at the instructor's discretion and does not carry a penalty."
            ),
            h2("Academic integrity"),
            p(
                "All submitted work must be the team's own. Using code or text from another source without citing it is a violation of the academic integrity policy."
            ),
        ),
        (
            h2("Schedule of deadlines"),
            li("Project proposal: due Friday, September 18, 2026 at 11:59 PM"),
            li("Requirements specification: due Friday, October 9, 2026 at 11:59 PM"),
            li("Design review: Friday, October 30, 2026"),
            li("Prototype demo: Friday, November 20, 2026"),
            li("Final report: due Friday, December 4, 2026 at 11:59 PM"),
            p("There is no final exam in this course."),
            h2("Required textbook"),
            p(
                "Software Engineering in Practice, 4th edition, by R. Whitfield. The bookstore lists it at 89.95 dollars."
            ),
            h2("Course tools"),
            p(
                "Teams use the course portal to submit work and an agile board of their choice to track tasks. Source code must be kept in a version-control repository that the instructor can access."
            ),
        ),
    ),
)

SYLLABUS_COPY = Document(
    path="courses/cse485/syllabus-copy.pdf",
    kind="pdf_text",
    blocks=SYLLABUS.blocks,
    exact_duplicate_of="courses/cse485/syllabus.pdf",
    note="Byte-for-byte copy of syllabus.pdf",
)

ASSIGNMENT_1 = Document(
    path="courses/cse485/assignment-1-project-proposal.pdf",
    kind="pdf_text",
    blocks=(
        (
            h1("Assignment 1: Project Proposal"),
            p("Due Friday, September 18, 2026 at 11:59 PM. Worth 10 percent of the final grade."),
            h2("Task"),
            p(
                "Each team submits a proposal of no more than two pages that describes the product it plans to build."
            ),
            p(
                "The proposal must contain a problem statement, a description of the target users, three core features and a list of risks."
            ),
            h2("Teams"),
            p(
                "Teams have three or four students. Each team must be approved by the instructor before it starts work."
            ),
        ),
        (
            h2("Submission"),
            p(
                "Submit the proposal as a single PDF file through the course portal. Only one team member needs to submit."
            ),
            h2("Grading rubric (100 points)"),
            li("Problem statement: 25 points"),
            li("Core features: 30 points"),
            li("Risks and mitigations: 20 points"),
            li("Writing quality: 25 points"),
            p("A proposal that exceeds two pages loses 5 points for each extra page."),
        ),
    ),
)

ASSIGNMENT_2 = Document(
    path="courses/cse485/assignment-2-requirements-spec.pdf",
    kind="pdf_text",
    blocks=(
        (
            h1("Assignment 2: Requirements Specification"),
            p("Due Friday, October 9, 2026 at 11:59 PM. Worth 15 percent of the final grade."),
            h2("Task"),
            p(
                "Each team writes a requirements specification for its product. The specification must contain at least 12 functional requirements and at least 5 non-functional requirements."
            ),
            p(
                "Every requirement is written as a numbered statement that uses the word shall, and each one must be testable."
            ),
        ),
        (
            h2("Traceability"),
            p(
                "Include a traceability table that links each functional requirement to the user need it satisfies."
            ),
            h2("Format"),
            p(
                "Submit the specification as a DOCX or PDF file through the course portal. Keep a version number and a date on the first page."
            ),
            p(
                "Teams may revise the specification after feedback. The version that is graded is the one submitted by the deadline."
            ),
        ),
    ),
)

LECTURE_4 = Document(
    path="courses/cse485/lecture-04-slide-notes.pdf",
    kind="pdf_text",
    blocks=(
        (
            h1("Lecture 4 Slide Notes: Requirements Engineering"),
            h2("Slide 1: Why requirements matter"),
            p(
                "Most project failures trace back to requirements that were unclear, missing or changed late."
            ),
            h2("Slide 2: Elicitation techniques"),
            li("Interviews with the people who will use the product"),
            li("Surveys for reaching many users cheaply"),
            li("Observation of how users work today"),
        ),
        (
            h2("Slide 3: Qualities of a good requirement"),
            p("A good requirement is unambiguous, testable, necessary and feasible."),
            p(
                "Words such as fast, easy and user-friendly are not testable until a number is attached to them."
            ),
            h2("Slide 4: Functional and non-functional requirements"),
            p(
                "A functional requirement says what the system does. A non-functional requirement says how well it does it, for example response time or capacity."
            ),
        ),
        (
            h2("Slide 5: Prioritising with MoSCoW"),
            li("Must have: the product fails without it"),
            li("Should have: important but not vital"),
            li("Could have: nice to have if time allows"),
            li("Won't have: agreed to leave out for now"),
            h2("Slide 6: Reminder"),
            p(
                "The requirements specification is due on October 9. Bring your draft to class on October 6 for peer feedback."
            ),
        ),
    ),
)

DOCS = [SYLLABUS, SYLLABUS_COPY, ASSIGNMENT_1, ASSIGNMENT_2, LECTURE_4]
