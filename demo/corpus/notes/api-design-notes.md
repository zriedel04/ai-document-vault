# StudyBuddy API Design Notes

## Endpoints

- GET /groups: list study groups, filtered by course code
- POST /groups: create a study group
- POST /groups/{id}/requests: ask to join a group
- GET /meetings/{id}/calendar: download an iCalendar file

## Pagination

List endpoints return 20 items by default. A client may ask for at most 100 items per page with the limit parameter.

## Errors

Errors use standard HTTP status codes. A request for a group that does not exist returns 404.
