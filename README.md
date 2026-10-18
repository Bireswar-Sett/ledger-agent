# Ledger Agent (Render deployment)

## Important limitation
The exam's private API schema and pagination contract must be discovered from the full `questionData` URL. This project includes a best-effort discovery/parser scaffold, but it is **not guaranteed to pass grading without adapting `main.py` to the actual API response**. Do not assume that guessed paths or field names are correct.

## Deploy to Render
1. Push this folder to a GitHub repository.
2. In Render, choose **New > Blueprint** and connect the repository, or create a Web Service manually.
3. Configure the environment variable `LEDGER_ROOT_URL` with the complete assignment URL, including the full `quizSign` and `path=/`.
4. Build command: `pip install -r requirements.txt`
5. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. Deploy. The public endpoint is the service HTTPS URL at `POST /`.

## API contract
Request:
```json
{"question":"What was the total revenue in USD from the North region in March 2026?"}
```
Response:
```json
{"answer": 12345.67}
```
The response number above is illustrative only.

## Before submission
- Adapt `_candidate_urls`, `_flatten_records`, `_query`, and pagination to match the live API's actual schema.
- Ensure revenue/refund definitions are correct and avoid double-counting nested records.
- Test all question categories and the grader's alternate wording.
- Confirm responses complete within 15 seconds.
- Do not commit the signed assignment URL to a public GitHub repository. Set it privately as a Render environment variable.
