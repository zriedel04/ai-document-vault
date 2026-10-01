"""Screenshots and receipts, drawn as pictures. The lines are the text drawn into each picture."""

from __future__ import annotations

from model import Document

BUILD_ERROR = Document(
    path="screenshots/build-error.png",
    kind="png",
    layout="terminal",
    lines=(
        (
            "alex@laptop:~/studybuddy$ npm run build",
            "> studybuddy@0.1.0 build",
            "> webpack --mode production",
            "ERROR in ./src/app.js",
            "Module not found: Error: Can't resolve 'express-session'",
            "Build failed with 1 error",
            "alex@laptop:~/studybuddy$ npm install express-session",
            "added 1 package in 2s",
        ),
    ),
)

KANBAN = Document(
    path="screenshots/kanban-board.png",
    kind="png",
    layout="kanban",
    lines=(
        (
            "Team Northwind - Sprint 3 Board",
            "TO DO",
            "Write search tests",
            "Export calendar file",
            "IN PROGRESS",
            "Group page layout (Sam)",
            "Join request endpoint (Jordan)",
            "DONE",
            "Account signup (Alex)",
            "Create group form (Sam)",
        ),
    ),
)

CALENDAR = Document(
    path="screenshots/calendar-finals-week.png",
    kind="png",
    layout="calendar",
    lines=(
        (
            "December 2026 - Finals Week",
            "Mon Dec 7: Team retro at 4:30 PM",
            "Tue Dec 8: Calculus II final at 9:00 AM",
            "Wed Dec 9: Operating Systems final at 1:00 PM",
            "Thu Dec 10: Capstone celebration at 5:00 PM",
            "Fri Dec 11: Last day of term",
        ),
    ),
)

ADVISOR_CHAT = Document(
    path="screenshots/chat-with-advisor.png",
    kind="png",
    layout="chat",
    lines=(
        (
            "Friday, November 20, 2026",
            "Dr. Raman: Great demo today. Please fix the search speed before the final report.",
            "Alex: Will do. Jordan is adding an index.",
            "Dr. Raman: Perfect. The final report is due December 4.",
        ),
    ),
)

RECEIPT_BOOKS = Document(
    path="receipts/bookstore-receipt.jpg",
    kind="jpg",
    layout="receipt",
    seed=21,
    lines=(
        (
            "HARBOR BOOKS",
            "14 Dock Street",
            "Date: 2026-08-28",
            "Software Engineering in Practice 4th Ed\t$89.95",
            "Tax (8%)\t$7.20",
            "Total\t$97.15",
            "Paid by card",
            "Thank you for shopping at Harbor Books",
        ),
    ),
)

RECEIPT_COFFEE = Document(
    path="receipts/coffee-receipt.jpg",
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
)

RECEIPT_REPAIR = Document(
    path="receipts/laptop-repair-receipt.jpg",
    kind="jpg",
    layout="receipt",
    seed=23,
    lines=(
        (
            "QUICK FIX COMPUTERS",
            "Date: 2026-10-05",
            "Customer: Sam Patel",
            "Screen replacement\t$129.00",
            "Labor\t$45.00",
            "Subtotal\t$174.00",
            "Tax (8%)\t$13.92",
            "Total\t$187.92",
            "Repair completed in 5 business days",
        ),
    ),
)

RECEIPT_PRINTING = Document(
    path="receipts/printing-receipt.jpg",
    kind="jpg",
    layout="receipt",
    seed=24,
    lines=(
        (
            "COPY CORNER",
            "Date: 2026-12-03",
            "Poster prints 1 x $7.50",
            "Total\t$7.50",
            "Paid by card",
        ),
    ),
)

DOCS = [
    BUILD_ERROR,
    KANBAN,
    CALENDAR,
    ADVISOR_CHAT,
    RECEIPT_BOOKS,
    RECEIPT_COFFEE,
    RECEIPT_REPAIR,
    RECEIPT_PRINTING,
]
