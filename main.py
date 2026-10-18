import os
import re
import json
import time
import threading
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Ledger Agent", version="1.0.0")
SESSION = requests.Session()
CACHE_LOCK = threading.Lock()
CACHE = {"loaded_at": 0.0, "payloads": []}
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "300"))
ROOT_URL = os.getenv("LEDGER_ROOT_URL", "").strip()
TIMEOUT = float(os.getenv("LEDGER_TIMEOUT_SECONDS", "8"))


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


def _get_json(url: str, params: dict | None = None) -> Any:
    r = SESSION.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {"_raw_text": r.text}


def _root_url() -> str:
    if not ROOT_URL:
        raise RuntimeError("LEDGER_ROOT_URL environment variable is not configured")
    return ROOT_URL


def _candidate_urls(root: str, root_data: Any) -> list[str]:
    # Discover URL-like resources from the root response, if the API advertises them.
    found = []
    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str) and (v.startswith("/") or v.startswith("http")):
                    if any(word in (k + " " + v).lower() for word in
                           ("order", "refund", "product", "customer", "ledger", "transaction")):
                        found.append(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(root_data)
    base = root.split("?", 1)[0]
    # The exam API commonly uses a path query parameter; try resource paths on that same entry point.
    for p in ("/orders", "/refunds", "/products", "/customers", "/ledger", "/transactions"):
        found.append(base + "?" + "path=" + requests.utils.quote(p, safe=""))
    result = []
    for item in found:
        if item.startswith("http"):
            url = item
        else:
            from urllib.parse import urljoin
            url = urljoin(base, item)
        if url not in result:
            result.append(url)
    return result[:30]


def _fetch_payloads(force: bool = False) -> list[Any]:
    with CACHE_LOCK:
        if not force and CACHE["payloads"] and time.time() - CACHE["loaded_at"] < CACHE_TTL:
            return CACHE["payloads"]
        root = _root_url()
        root_data = _get_json(root)
        payloads = [root_data]
        for url in _candidate_urls(root, root_data):
            try:
                data = _get_json(url)
                if data != root_data:
                    payloads.append(data)
            except Exception:
                continue
        CACHE["payloads"] = payloads
        CACHE["loaded_at"] = time.time()
        return payloads


def _flatten_records(value: Any):
    """Yield dictionaries that look like ledger records from nested JSON."""
    if isinstance(value, dict):
        # Prefer leaf-ish record objects, while still exploring nested collections.
        if any(k.lower() in {"order_id", "orderid", "refund_id", "customer_id", "product_id",
                             "transaction_id", "invoice_id"} for k in value):
            yield value
        for v in value.values():
            yield from _flatten_records(v)
    elif isinstance(value, list):
        for item in value:
            yield from _flatten_records(item)


def _norm(s: str) -> str:
    return " ".join(s.lower().strip().split())


def _as_date(v: Any):
    if not isinstance(v, (str, int, float)):
        return None
    try:
        if isinstance(v, (int, float)):
            # Support unix seconds or milliseconds.
            n = float(v)
            if n > 10_000_000_000:
                n /= 1000
            return datetime.fromtimestamp(n).date()
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _money(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        try:
            return Decimal(str(v))
        except InvalidOperation:
            return None
    if isinstance(v, str):
        cleaned = re.sub(r"[$,\s]", "", v)
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _all_records(payloads):
    out = []
    seen = set()
    for p in payloads:
        for rec in _flatten_records(p):
            marker = json.dumps(rec, sort_keys=True, default=str)
            if marker not in seen:
                seen.add(marker)
                out.append(rec)
    return out


def _record_value(rec, names):
    for k, v in rec.items():
        if k.lower().replace("-", "_") in names:
            return v
    return None


def _query(question: str, records: list[dict]):
    q = _norm(question)
    if not records:
        raise ValueError("No ledger records were discovered. Check the API root and resource paths.")

    # Recognize a month/year date window.
    month_match = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})\b",
        q
    )
    start = end = None
    if month_match:
        months = {m.lower(): i for i, m in enumerate(
            ["January","February","March","April","May","June","July","August","September","October","November","December"], 1)}
        m = months[month_match.group(1)]
        y = int(month_match.group(2))
        start = date(y, m, 1)
        end = date(y + (m == 12), 1 if m == 12 else m + 1, 1)

    region_match = re.search(r"\b(north|south|east|west|central)\b", q)
    region = region_match.group(1) if region_match else None

    product_words = re.search(r"\bproduct\s+['\"]?([^'\"]+?)['\"]?(?:\s+in\s+|\s+for\s+|\s*$)", question, re.I)
    customer_words = re.search(r"\bcustomer\s+['\"]?([^'\"]+?)['\"]?(?:\s+in\s+|\s+for\s+|\s*$)", question, re.I)

    filtered = []
    for r in records:
        s = json.dumps(r, default=str).lower()
        if region and region not in s:
            continue
        if start:
            raw_date = _record_value(r, {"date", "order_date", "created_at", "timestamp", "refund_date"})
            d = _as_date(raw_date)
            if d is None or not (start <= d < end):
                continue
        if product_words and product_words.group(1).strip().lower() not in s:
            continue
        if customer_words and customer_words.group(1).strip().lower() not in s:
            continue
        filtered.append(r)

    if any(x in q for x in ("refund", "refunded")):
        metric_names = {"refund_amount", "refund_total", "amount_refunded", "refund_usd", "refund"}
        metric = "refunds"
    elif "revenue" in q or "sales" in q or "spent" in q or "spending" in q:
        metric_names = {"revenue", "revenue_usd", "total_revenue", "net_revenue", "sales", "sales_usd",
                        "total", "total_amount", "amount", "order_total", "total_usd", "subtotal"}
        metric = "revenue"
    elif any(x in q for x in ("how many", "number of", "count")):
        metric_names = set()
        metric = "count"
    else:
        raise ValueError("Question type not recognized. Extend the query parser for this wording/metric.")

    if metric == "count":
        answer = len(filtered)
    else:
        vals = []
        for r in filtered:
            for k, v in r.items():
                if k.lower().replace("-", "_") in metric_names:
                    amount = _money(v)
                    if amount is not None:
                        vals.append(amount)
                        break
        if not vals:
            raise ValueError(f"Could not identify a numeric {metric} field in the discovered records.")
        answer = float(sum(vals, Decimal("0.00")).quantize(Decimal("0.01")))

    return answer


@app.get("/")
def health():
    return {"status": "ok", "service": "ledger-agent", "endpoint": "POST /"}


@app.post("/")
def ask(request: QuestionRequest):
    try:
        payloads = _fetch_payloads()
        records = _all_records(payloads)
        answer = _query(request.question, records)
        return {"answer": answer}
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Ledger API request failed: {type(e).__name__}")
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Unexpected processing error")


@app.post("/refresh")
def refresh():
    try:
        _fetch_payloads(force=True)
        return {"status": "refreshed"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Refresh failed: {type(e).__name__}")
