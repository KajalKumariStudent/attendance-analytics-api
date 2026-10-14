# DECISIONS.md

### 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employees, and a compound index on `attendance_logs.emp_code` and `date` to guarantee one attendance record per employee per day. I also added indexes to support department filtering, employee joining dates, attendance date sorting, and status-based filtering. I rejected an index on `department` and `date` in attendance logs because department belongs to employees, not attendance records.

### 2. Punch-in race

When two identical punch-in requests arrive simultaneously, both may pass the initial existence check. The unique compound index on `emp_code` and `date` allows only one insert. The successful request returns `201`; the duplicate-key error is caught and converted to `409 Conflict`.

### 3. Ties

The leaderboard uses MongoDB ranking so employees with equal late minutes share a rank, and the next rank is skipped. Tied employees are ordered by `emp_code`. The limit is applied after ranking, so tied employees at the cutoff are handled according to the ranking and limit stages in the pipeline.

### 4. Headcount

The department summary starts from employees rather than attendance logs. It includes employees who joined on or before the period's final date and preserves employees with no matching attendance records, allowing them to contribute to headcount.

### 5. Scaling further

For 100 times more data, I would benchmark query plans and index usage, optimize compound indexes for real query patterns, and consider archiving older attendance records. I would also add load testing and monitoring before introducing caching or other infrastructure changes.