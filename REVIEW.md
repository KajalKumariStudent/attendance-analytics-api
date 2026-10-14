# REVIEW.md

## Defects Found in the Starter Application

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Returns `200 OK` without checking whether MongoDB is reachable. | Stop MongoDB and call `GET /health`; it still reports success. | Added a MongoDB connectivity check and return `503` when the database is unavailable. |
| 2 | `create_employee()` | Checks for duplicate employee codes before insertion, which is not safe under concurrent requests. | Send simultaneous requests with the same `emp_code`; both can pass the initial check. | Added a unique index on `emp_code` and handled duplicate-key errors as `409 Conflict`. |
| 3 | `create_employee()` | Stores `created_at` using naive local time rather than a consistent UTC instant. | Run the application on systems with different local time zones and compare stored timestamps. | Standardized timestamp handling and conversion at the API/database boundary. |
| 4 | `list_employees()` | Calculates the offset as `page * page_size`, incorrectly skipping the first page. | Request page 1 with a page size of 20; the first 20 employees are skipped. | Changed the offset to `(page - 1) * page_size`. |
| 5 | `list_employees()` | Calculates `total` without applying the department filter. | Filter by a department containing fewer employees than the whole collection; `total` is too large. | Count documents using the same filter as the listing query. |
| 6 | `list_employees()` | Does not validate pagination bounds or enforce the maximum page size. | Send page 0, a negative page, or `page_size` above 100. | Added pagination validation, with page starting at 1 and page size limited to 100. |
| 7 | `punch_in()` | Does not handle an unknown employee before accessing the employee's shift fields. | Punch in using an unknown `emp_code`; the code can raise an internal error instead of returning `404`. | Check employee existence first and return `404 Not Found` when missing. |
| 8 | `punch_in()` | Uses local-time conversion and truthiness to interpret `punched_at`; this can produce the wrong date and silently replace a supplied zero with the current time. | Use timestamps near midnight UTC or submit `punched_at: 0`. | Added strict epoch-millisecond validation and explicit UTC-to-IST conversion, including second-level truncation. |
| 9 | `punch_in()` | Does not validate the attendance status against the allowed values. | Submit an unsupported status such as `HOLIDAY`. | Restricted status to `PRESENT`, `WFH`, and `ON_DUTY` for punch-in requests. |
| 10 | `punch_in()` | The duplicate check followed by insertion is a race condition. | Send simultaneous punch-ins for the same employee and attendance date. | Added a unique compound index on `(emp_code, date)` and handled duplicate-key errors so only one request succeeds. |
| 11 | `punch_in()` | Determines the attendance date directly from the converted timestamp and does not account for the previous-day start of an overnight shift. | Punch in at 02:00 for a 22:00–06:00 shift; the record should belong to the shift that started the previous day. | Added IST attendance-date calculation that handles overnight shifts. |
| 12 | `punch_in()` | Returns an `id` derived from MongoDB's internal `_id`, although the API contract identifies attendance records by `(emp_code, date)` and does not expose database IDs. | Inspect the punch-in response for an unexpected `id` field. | Removed internal MongoDB identifiers from API responses. |
| 13 | `list_attendance()` | Fetches all matching attendance documents and sorts them in Python before pagination. This does not scale to the required dataset size. | Run the endpoint against a large collection and inspect memory use and query latency. | Moved sorting, skipping, and limiting into MongoDB and added appropriate indexes. |
| 14 | `compute_late_minutes()` | Uses the floored whole-minute value to decide whether lateness exceeds the grace period. This misses a punch-in just over 10 minutes late. | For a 09:30 shift, test 09:40:01; the expected `late_minutes` is 10, not 0. | Compare the actual time difference against the strict 10-minute threshold before calculating floored minutes from shift start. |
| 15 | `compute_work_hours()` | Python's built-in `round()` does not guarantee the required decimal half-up rounding. | Test work durations whose hour values fall on a rounding tie. | Used explicit half-up rounding to two decimal places. |
| 16 | `compute_overtime()` | Always constructs the shift-end time on the supplied date, so it calculates the wrong overtime boundary for overnight shifts. | Test a 22:00–06:00 shift with punch-out after 06:00 the following day. | Calculated the correct shift-end date for both normal and overnight shifts and applied the 30-minute overtime threshold. |
| 17 | `list_attendance()` | Does not apply the same filtered query to its count and does not use database pagination efficiently. | Combine employee, date, and status filters on a large dataset and compare the count and returned page. | Kept the count query aligned with all active filters and applied sorting and pagination in MongoDB. |

## Items Examined and Considered Not Defects

- **Environment variable precedence:** `load_dotenv()` defaults to not overriding existing environment variables, so real `MONGO_URI` and `MONGO_DB` values take precedence over values in `.env`. This behavior is correct, provided no later code overrides those variables.
- **Employee identity:** Using `emp_code` as the business identifier is correct. MongoDB's `_id` should remain an internal implementation detail.
- **Attendance uniqueness:** A record is identified by `(emp_code, date)`. This is the correct business key; it must be enforced with a unique compound index.
- **Pagination convention:** Starting pages at 1 is consistent with the assignment contract. The defect was the incorrect offset calculation, not the convention itself.

