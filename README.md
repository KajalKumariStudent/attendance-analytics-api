# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** for employee management, attendance punch-in/punch-out, attendance regularization, and analytics.

## Tech Stack

- Python 3.11+
- FastAPI
- PyMongo
- MongoDB 7
- Pytest

## Features

- Employee creation and paginated employee listing
- Attendance punch-in and punch-out
- Attendance record filtering and pagination
- Attendance regularization with correction history
- Monthly employee attendance analytics
- Department-wise attendance summary
- Late-comer leaderboard with ranking
- Department attendance trends with a seven-day moving average
- MongoDB query execution statistics through the explain endpoint

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer
- Git

## Setup and Run

Clone the repository and navigate to the project root.

Create and activate a virtual environment on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Configure the following environment variables before starting the application:

- `MONGO_URI` — MongoDB connection URI
- `MONGO_DB` — MongoDB database name

For local development, you may use a `.env` file in the project root. Do not commit `.env` or any credentials.

Start the API:

```powershell
uvicorn app.main:app --port 8000
```

The API will be available at `http://localhost:8000`.

## API Documentation

Interactive Swagger documentation:

`http://localhost:8000/docs`

Health endpoint:

`http://localhost:8000/health`

## Run Tests

Run the automated test suite from the project root:

```powershell
python -m pytest -v
```

The current test suite contains 32 tests, all of which pass in the local development environment.

## Notes

- MongoDB indexes are created by the application at startup.
- Attendance timestamps use epoch milliseconds at the API boundary.
- Attendance dates and shift times follow Indian Standard Time (IST).
- Analytics are implemented using MongoDB aggregation pipelines.
