import os
import csv
import shutil
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from itertools import zip_longest
from typing import List
from urllib.parse import urlencode
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from models import (
    Breed as BreedModel,
    Cow,
    DailyMilkingVitalsLog,
    FeedCenter,
    FeedInventory,
    LiveStationState,
    StagingFeedUpload,
    FeedType as FeedTypeModel,
    FeedAudit,
    OperationalParameter,
    Station,
    WaterIntakeLog,
    engine,
    SessionLocal,
    initialize_database,
    get_feed_summary,
    get_data_verification_queue,
    get_feed_reconciliation_report,
    get_inventory_history,
    get_milk_summary,
    get_milk_yield_report,
    get_water_summary,
    get_herd_status_with_yield,
    get_cow_profile,
    get_operational_parameters,
    get_parameter_rows,
    get_station_cards,
    record_station_session,
    apply_health_alert,
    get_existing_milking_cycle_log,
    get_milking_cycle_bounds,
    update_cow_from_station_data,
    upsert_live_station_state,
    update_operational_parameters,
    clear_database,
)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
VALID_COW_STATUSES = {"Active", "Dry", "Treatment"}
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo
BASE_PATH = os.getenv("DAIRY_BASE_PATH", "")


def normalize_cow_status(status):
    raw_status = str(status or "").strip()
    for valid_status in VALID_COW_STATUSES:
        if raw_status.lower() == valid_status.lower():
            return valid_status
    return None


def normalize_rfid(value):
    if value is None:
        return ""
    value = str(value or "").strip()
    if not value:
        return ""
    value = value.replace(" ", "")
    if value.isdigit():
        return value
    try:
        decimal_value = Decimal(value)
    except InvalidOperation:
        return value
    if decimal_value == decimal_value.to_integral_value():
        return format(decimal_value.quantize(Decimal("1")), "f")
    return value


def is_valid_rfid(value):
    value = normalize_rfid(value)
    return len(value) == 15 and value.isdigit()


def parse_optional_nonnegative_float(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def parse_required_float(value):
    if value is None or str(value).strip() == "":
        raise ValueError("missing number")
    return float(value)


def parse_feed_upload_timestamp(value):
    if value is None or str(value).strip() == "":
        raise ValueError("missing timestamp")
    raw_value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        parsed_date = date.fromisoformat(raw_value)
        parsed = datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_live_station_timestamp(value):
    if value is None or str(value).strip() == "":
        return datetime.now(timezone.utc)
    raw_value = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(raw_value, fmt)
            return parsed.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
        except ValueError:
            pass
    parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def parse_optional_float_value(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_feed_upload_row(row, row_number):
    errors = []
    ear_tag = normalize_rfid(row.get("EarTagRFID", ""))
    timestamp = None
    green_fodder = None
    concentrate = None
    if not is_valid_rfid(ear_tag):
        errors.append("Invalid EarTagRFID")
    try:
        timestamp = parse_feed_upload_timestamp(row.get("Timestamp"))
    except (TypeError, ValueError):
        errors.append("Invalid Timestamp")
    try:
        green_fodder = parse_required_float(row.get("GreenFodderKg"))
    except (TypeError, ValueError):
        errors.append("Invalid GreenFodderKg")
    try:
        concentrate = parse_required_float(row.get("ConcentrateFeedKg"))
    except (TypeError, ValueError):
        errors.append("Invalid ConcentrateFeedKg")
    if concentrate is not None and not (1.0 < concentrate < 10.0):
        errors.append("Data Entry Limit Exceeded: ConcentrateFeedKg must be strictly between 1.0 and 10.0 kg per session.")
    if green_fodder is not None and not (5.0 < green_fodder < 20.0):
        errors.append("Data Entry Limit Exceeded: GreenFodderKg must be strictly between 5.0 and 20.0 kg per session.")
    if green_fodder is not None and concentrate is not None and green_fodder < concentrate:
        errors.append("Warning: Fodder amount is less than concentrate. Possible data inversion error detected.")
    return {
        "row_number": row_number,
        "ear_tag": ear_tag,
        "timestamp": timestamp,
        "green_fodder_kg": green_fodder,
        "concentrate_feed_kg": concentrate,
        "errors": errors,
        "raw": row,
    }


def wants_json(request):
    accept = request.headers.get("accept", "")
    return "application/json" in accept.lower()


def flatten_live_station_payload(payload):
    if isinstance(payload, dict):
        return [payload]
    if not isinstance(payload, list):
        raise ValueError("Live station payload must be a JSON object or array.")
    rows = []
    for item in payload:
        if isinstance(item, list):
            rows.extend(flatten_live_station_payload(item))
        elif isinstance(item, dict):
            rows.append(item)
    return rows


def extract_live_station_payload(raw_text):
    raw_text = raw_text.strip()
    try:
        return flatten_live_station_payload(json.loads(raw_text))
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates = []
    for index, char in enumerate(raw_text):
        if char not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(raw_text[index:])
        except json.JSONDecodeError:
            continue
        try:
            rows = flatten_live_station_payload(parsed)
        except ValueError:
            continue
        if rows:
            candidates.append(rows)
    if not candidates:
        raise ValueError("No valid live station JSON payload found.")
    return candidates[-1]


def upsert_live_station_rows(db, rows):
    updated = 0
    finalized = 0
    errors = []
    for index, row in enumerate(rows, start=1):
        try:
            station_id = int(row.get("StationID"))
            timestamp = parse_live_station_timestamp(row.get("Timestamp"))
        except (TypeError, ValueError) as exc:
            errors.append({"row": index, "error": f"Invalid station row: {exc}"})
            continue
        if station_id <= 0:
            errors.append({"row": index, "error": "StationID must be positive."})
            continue
        if not db.query(Station).filter(Station.StationID == station_id).first():
            db.add(Station(StationID=station_id, Label=f"Station {station_id}"))
        ear_tag = normalize_rfid(row.get("EarTagRFID") or row.get("RFIDTag") or row.get("RFID") or "")
        visual_id = str(row.get("VisualID") or "").strip() or None
        cow_name = str(row.get("CowName") or "").strip() or None
        status = str(row.get("Status") or "Idle").strip() or "Idle"
        temperature = parse_optional_float_value(row.get("TemperatureCelsius"))
        weight_raw = "" if row.get("WeightKg") is None else str(row.get("WeightKg")).strip()
        milk_yield = parse_optional_float_value(row.get("WeightKg"))
        cow = None
        if ear_tag:
            cow = db.query(Cow).filter(Cow.EarTagRFID == ear_tag).first()
        if cow is None and visual_id:
            cow = db.query(Cow).filter(Cow.VisualID == visual_id).first()
        if visual_id is None and cow is not None:
            visual_id = cow.VisualID
        if cow is None:
            identifier = ear_tag or visual_id or cow_name or f"station {station_id}"
            errors.append({"row": index, "error": f"Cow {identifier} was not found in cow table; row ignored."})
            upsert_live_station_state(
                db,
                station_id,
                timestamp,
                "Idle",
                None,
                None,
                None,
                "",
                None,
                0.0,
            )
            continue
        updated += 1
        update_cow_from_station_data(db, cow, cow_name=cow_name, visual_id=visual_id)
        if milk_yield is not None and milk_yield > 0 and visual_id:
            if cow.Status != "Active":
                errors.append({"row": index, "error": f"Cow {cow.VisualID} status is {cow.Status}; milking row ignored."})
                upsert_live_station_state(
                    db,
                    station_id,
                    timestamp,
                    "Idle",
                    visual_id,
                    cow_name or cow.CowName,
                    temperature,
                    weight_raw,
                    None,
                    0.0,
                )
                continue
            existing_log = (
                db.query(DailyMilkingVitalsLog)
                .filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID)
                .filter(DailyMilkingVitalsLog.StationID == station_id)
                .filter(DailyMilkingVitalsLog.Timestamp == timestamp)
                .first()
            )
            cycle_log = get_existing_milking_cycle_log(
                db,
                cow.EarTagRFID,
                timestamp,
                exclude_log_id=existing_log.LogID if existing_log else None,
            )
            if cycle_log is not None:
                cycle_name, _, _ = get_milking_cycle_bounds(timestamp)
                errors.append(
                    {
                        "row": index,
                        "error": f"Cow {cow.VisualID} already milked in the {cycle_name} cycle; row ignored.",
                    }
                )
                upsert_live_station_state(
                    db,
                    station_id,
                    timestamp,
                    "Idle",
                    visual_id,
                    cow_name or cow.CowName,
                    temperature,
                    weight_raw,
                    None,
                    0.0,
                )
                continue
            if existing_log is None:
                existing_log = DailyMilkingVitalsLog(
                    EarTagRFID=cow.EarTagRFID,
                    StationID=station_id,
                    Timestamp=timestamp,
                )
            existing_log.YieldKg = milk_yield
            existing_log.WeightSensorKg = milk_yield
            existing_log.CowName = cow_name or cow.CowName or cow.VisualID
            existing_log.BodyTemperatureCelsius = temperature or 0.0
            db.add(existing_log)
            apply_health_alert(db, cow.EarTagRFID, existing_log.BodyTemperatureCelsius, commit=False)
            status = "Complete"
            finalized += 1
        upsert_live_station_state(
            db,
            station_id,
            timestamp,
            status,
            visual_id,
            cow_name or (cow.CowName if cow else None),
            temperature,
            weight_raw,
            milk_yield,
            0.0,
        )
    db.commit()
    return {"updated": updated, "finalized": finalized, "errors": errors}


try:
    import openpyxl
except ImportError:
    openpyxl = None

app = FastAPI(title="Smart Mini-Dairy Dashboard", root_path=BASE_PATH)

os.makedirs("static/uploads", exist_ok=True)
os.makedirs("static/samples", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")
templates.env.globals["base_path"] = BASE_PATH

initialize_database(engine)


@app.middleware("http")
async def db_session_middleware(request: Request, call_next):
    request.state.db = SessionLocal()
    try:
        response = await call_next(request)
        return response
    finally:
        request.state.db.close()


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    db: Session = request.state.db
    station_cards = get_station_cards(db)
    herd_rows = get_herd_status_with_yield(db)
    feed_summary = get_feed_summary(db)
    milk_summary = get_milk_summary(db)
    milk_report_summary = get_milk_yield_report(db, period="today", group="milked")
    water_summary = get_water_summary(db)
    data_verification_queue = get_data_verification_queue(db)
    feed_reconciliation = get_feed_reconciliation_report(db)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "station_cards": station_cards,
            "herd_rows": herd_rows,
            "feed_summary": feed_summary,
            "milk_summary": milk_summary,
            "milk_report_summary": milk_report_summary,
            "water_summary": water_summary,
            "data_verification_queue": data_verification_queue,
            "feed_reconciliation": feed_reconciliation,
            "message": request.query_params.get("message", ""),
        },
    )


@app.get("/api/station-cards")
def station_cards_api(request: Request):
    db: Session = request.state.db
    return get_station_cards(db)


@app.post("/api/live-station-json")
async def live_station_json_api(request: Request):
    db: Session = request.state.db
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Missing live station JSON payload")
    try:
        rows = extract_live_station_payload(body.decode("utf-8-sig"))
        result = upsert_live_station_rows(db, rows)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@app.post("/admin/live-station-json")
def admin_live_station_json_upload(request: Request, upload_file: UploadFile = File(...)):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    raw_text = upload_file.file.read().decode("utf-8-sig")
    try:
        rows = extract_live_station_payload(raw_text)
        result = upsert_live_station_rows(db, rows)
    except ValueError as exc:
        return admin_redirect(f"Live station JSON rejected: {exc}")
    message = f"Live station JSON updated {result['updated']} station row(s)"
    if result["finalized"]:
        message += f" and finalized {result['finalized']} milk log(s)"
    if result["errors"]:
        message += f"; {len(result['errors'])} warning(s)"
    return admin_redirect(message)


@app.get("/cows/{ear_tag}", response_class=HTMLResponse)
def cow_detail(request: Request, ear_tag: str):
    db: Session = request.state.db
    profile = get_cow_profile(db, ear_tag)
    if profile is None:
        raise HTTPException(status_code=404, detail="Cow not found")
    return templates.TemplateResponse(
        "cow_detail.html",
        {
            "request": request,
            "profile": profile,
        },
    )


@app.get("/milk-yield", response_class=HTMLResponse)
def milk_yield_report(
    request: Request,
    period: str = "today",
    group: str = "milked",
    start: str = "",
    end: str = "",
):
    db: Session = request.state.db
    start_date = None
    end_date = None
    if period == "range" and start and end:
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except ValueError:
            period = "today"
            start_date = None
            end_date = None
    report = get_milk_yield_report(db, period=period, start_date=start_date, end_date=end_date, group=group)
    return templates.TemplateResponse(
        "milk_yield.html",
        {
            "request": request,
            "report": report,
        },
    )


@app.get("/inventory-history", response_class=HTMLResponse)
def inventory_history(
    request: Request,
    period: str = "today",
    start: str = "",
    end: str = "",
):
    db: Session = request.state.db
    start_date = None
    end_date = None
    if period == "range" and start and end:
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end)
        except ValueError:
            period = "today"
            start_date = None
            end_date = None
    report = get_inventory_history(db, period=period, start_date=start_date, end_date=end_date)
    return templates.TemplateResponse(
        "inventory_history.html",
        {
            "request": request,
            "report": report,
        },
    )


def is_admin_authenticated(request: Request):
    return request.cookies.get("admin_authenticated") == "true"


def admin_redirect(message=""):
    if not message:
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    return RedirectResponse(url=f"{BASE_PATH}/admin?{urlencode({'message': message})}", status_code=303)


def dashboard_redirect(message=""):
    if not message:
        return RedirectResponse(url=f"{BASE_PATH}/", status_code=303)
    return RedirectResponse(url=f"{BASE_PATH}/\?{urlencode({'message': message})}", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if password != ADMIN_PASSWORD:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid password"})
    response = RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    response.set_cookie("admin_authenticated", "true", httponly=True)
    return response


@app.get("/logout")
def logout(request: Request):
    response = RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    response.delete_cookie("admin_authenticated")
    return response


@app.post("/admin/clear-database")
def admin_clear_database(request: Request, confirmation: str = Form("")):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    if confirmation.strip() != "CLEAR":
        return admin_redirect("Database clear skipped. Type CLEAR to confirm.")
    db: Session = request.state.db
    clear_database(db)
    initialize_database(engine)
    return admin_redirect("Database cleared for testing. Default operating parameters were restored.")


@app.get("/admin", response_class=HTMLResponse)
def admin(
    request: Request,
    cow: str = "",
    visual_id: str = "",
    action: str = "",
):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    cow_count = db.query(Cow).count()
    station_count = db.query(Station).count()
    breed_count = db.query(BreedModel).count()
    feed_type_count = db.query(FeedTypeModel).count()
    feed_inventory_count = db.query(FeedInventory).count()
    water_log_count = db.query(WaterIntakeLog).count()
    parameter_rows = get_parameter_rows(db)
    statuses = ["Active", "Dry", "Treatment"]
    cow_options = [
        {
            "ear_tag": cow.EarTagRFID,
            "visual_id": cow.VisualID,
            "cow_name": cow.CowName or cow.VisualID,
            "breed": cow.Breed,
            "status": cow.Status,
            "age_years": cow.AgeYears,
            "weight_kg": cow.WeightKg,
            "photo_url": cow.PhotoURL or "",
        }
        for cow in db.query(Cow).order_by(Cow.VisualID).all()
    ]
    cow_rows = [
        {
            "ear_tag": row["ear_tag"],
            "visual_id": row["visual_id"],
            "cow_name": row["cow_name"],
        }
        for row in cow_options
    ]
    breeds = [breed.Name for breed in db.query(BreedModel).order_by(BreedModel.Name).all()]
    feed_types = [feed.Name for feed in db.query(FeedTypeModel).order_by(FeedTypeModel.Name).all()]
    stations = [station.StationID for station in db.query(Station).order_by(Station.StationID).all()]
    selected_cow = None
    selected_ear_tag = normalize_rfid(cow)
    if selected_ear_tag:
        cow_record = db.query(Cow).filter(Cow.EarTagRFID == selected_ear_tag).first()
        if cow_record:
            selected_cow = {
                "ear_tag": cow_record.EarTagRFID,
                "visual_id": cow_record.VisualID,
                "cow_name": cow_record.CowName or "",
                "breed": cow_record.Breed,
                "status": cow_record.Status,
                "age_years": cow_record.AgeYears,
                "weight_kg": cow_record.WeightKg,
                "photo_url": cow_record.PhotoURL or "",
            }
    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "cow_count": cow_count,
            "station_count": station_count,
            "breed_count": breed_count,
            "feed_type_count": feed_type_count,
            "feed_inventory_count": feed_inventory_count,
            "water_log_count": water_log_count,
            "parameter_rows": parameter_rows,
            "statuses": statuses,
            "cow_options": cow_options,
            "cow_rows": cow_rows,
            "breeds": breeds,
            "feed_types": feed_types,
            "stations": stations,
            "selected_cow": selected_cow,
            "prefill_cow_ear_tag": cow,
            "prefill_cow_visual_id": visual_id,
            "prefill_action": action,
            "message": request.query_params.get("message", ""),
        },
    )


def parse_spreadsheet(upload_file: UploadFile):
    file_bytes = upload_file.file.read()
    name = upload_file.filename or ""
    aliases = {
        "eartagrfid": "EarTagRFID",
        "ear tag rfid": "EarTagRFID",
        "rfid": "EarTagRFID",
        "rfidtag": "EarTagRFID",
        "visualid": "VisualID",
        "visual id": "VisualID",
        "cowname": "CowName",
        "cow name": "CowName",
        "breed": "Breed",
        "ageyears": "AgeYears",
        "age years": "AgeYears",
        "weightkg": "WeightKg",
        "weight kg": "WeightKg",
        "status": "Status",
        "photourl": "PhotoURL",
        "photo url": "PhotoURL",
        "delete": "Delete",
        "stationid": "StationID",
        "station id": "StationID",
        "label": "Label",
        "name": "Name",
        "feedtype": "FeedType",
        "feed type": "FeedType",
        "availablekg": "AvailableKg",
        "available kg": "AvailableKg",
        "lastupdated": "LastUpdated",
        "last updated": "LastUpdated",
        "note": "Note",
        "timestamp": "Timestamp",
        "greenfodderkg": "GreenFodderKg",
        "green fodder kg": "GreenFodderKg",
        "concentratefeedkg": "ConcentrateFeedKg",
        "concentrate feed kg": "ConcentrateFeedKg",
    }

    def normalize_header(key):
        header = str(key).strip().lstrip("\ufeff")
        lookup = " ".join(header.replace("_", " ").split()).lower()
        compact_lookup = lookup.replace(" ", "")
        return aliases.get(lookup) or aliases.get(compact_lookup) or header

    def clean_record(record):
        return {
            normalize_header(key): value
            for key, value in record.items()
            if key is not None and str(key).strip() != ""
        }

    def clean_record_from_cells(headers, cells):
        record = {}
        unnamed_values = []
        for index, header in enumerate(headers):
            value = cells[index] if index < len(cells) else None
            normalized = normalize_header(header)
            if normalized:
                record[normalized] = value
            elif value is not None and str(value).strip() != "":
                unnamed_values.append(value)
        cleaned = clean_record(record)
        if not is_valid_rfid(cleaned.get("EarTagRFID", "")):
            for value in unnamed_values:
                recovered_rfid = normalize_rfid(value)
                if is_valid_rfid(recovered_rfid):
                    cleaned["EarTagRFID"] = recovered_rfid
                    break
        return cleaned

    if name.lower().endswith(".xlsx") or upload_file.content_type in (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ):
        if openpyxl is None:
            raise HTTPException(status_code=500, detail="Openpyxl is required for XLSX uploads")
        workbook = openpyxl.load_workbook(filename=BytesIO(file_bytes), data_only=True)
        sheet = workbook.active
        rows = list(sheet.values)
        if not rows:
            return []
        header = [str(cell).strip() for cell in rows[0]]
        records = []
        for row in rows[1:]:
            if not any(cell is not None and str(cell).strip() != "" for cell in row):
                continue
            records.append(clean_record_from_cells(header, row))
        return records
    decode_error = None
    text = None
    for encoding in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin-1"):
        try:
            decoded = file_bytes.decode(encoding)
            if decoded and decoded.count("\x00") / len(decoded) > 0.05:
                continue
            text = decoded
            break
        except UnicodeDecodeError as exc:
            decode_error = exc
    if text is None:
        raise ValueError(f"Could not read CSV encoding: {decode_error}")
    text = text.replace("\x00", "").lstrip("\ufeff")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"Upload file is empty ({len(file_bytes)} bytes).")
    delimiter_hint = None
    if lines[0].strip().lower().startswith("sep="):
        delimiter_hint = lines[0].strip()[4:5] or None
        lines = lines[1:]
    if not lines:
        raise ValueError("Upload file is missing a header row after the Excel separator line.")
    text = "\n".join(lines)
    sample = text[:4096]
    if delimiter_hint:
        dialect = csv.excel
        dialect.delimiter = delimiter_hint
    else:
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
    reader = csv.reader(StringIO(text), dialect=dialect)
    rows = list(reader)
    if not rows:
        raise ValueError(f"Upload file is missing a header row ({len(file_bytes)} bytes received).")
    headers = rows[0]
    return [
        clean_record_from_cells(headers, row)
        for row in rows[1:]
        if any(value is not None and str(value).strip() != "" for value in row)
    ]


def discard_duplicate_upload_rows(rows):
    unique_rows = []
    seen = set()
    duplicate_count = 0
    for row in rows:
        signature = tuple(
            (str(key).strip(), "" if value is None else str(value).strip())
            for key, value in sorted(row.items(), key=lambda item: str(item[0]))
        )
        if signature in seen:
            duplicate_count += 1
            continue
        seen.add(signature)
        unique_rows.append(row)
    return unique_rows, duplicate_count


def ensure_feed_type(db, name):
    if not db.query(FeedTypeModel).filter(FeedTypeModel.Name == name).first():
        db.add(FeedTypeModel(Name=name))


def upsert_feed_upload_item(db, ear_tag, timestamp, dispatch_date, feed_type, amount):
    existing = (
        db.query(FeedCenter)
        .filter(FeedCenter.RFIDTag == ear_tag)
        .filter(FeedCenter.Timestamp == timestamp)
        .filter(FeedCenter.FeedType == feed_type)
        .first()
    )
    if existing:
        old_amount = existing.AllocatedAmountKg
        existing.DispatchDate = dispatch_date
        existing.AllocatedAmountKg = amount
        existing.Source = "Manual Upload"
        db.add(existing)
        inventory = db.query(FeedInventory).filter(FeedInventory.FeedType == feed_type).first()
        if inventory:
            inventory.AvailableKg = max(inventory.AvailableKg + old_amount - amount, 0.0)
            inventory.LastUpdated = dispatch_date
            db.add(inventory)
        return "Updated"
    db.add(
        FeedCenter(
            RFIDTag=ear_tag,
            Timestamp=timestamp,
            DispatchDate=dispatch_date,
            FeedType=feed_type,
            AllocatedAmountKg=amount,
            Source="Manual Upload",
        )
    )
    inventory = db.query(FeedInventory).filter(FeedInventory.FeedType == feed_type).first()
    if inventory:
        inventory.AvailableKg = max(inventory.AvailableKg - amount, 0.0)
        inventory.LastUpdated = dispatch_date
        db.add(inventory)
    return "Inserted"


def parse_feed_output_items(feed_types, amounts):
    items = []
    errors = []
    totals_by_type = {}
    for index, (feed_type_value, amount_value) in enumerate(zip_longest(feed_types, amounts, fillvalue=""), start=1):
        feed_type = str(feed_type_value or "").strip()
        raw_amount = str(amount_value or "").strip()
        if not feed_type and not raw_amount:
            continue
        if feed_type and not raw_amount:
            continue
        if not feed_type:
            errors.append(f"Feed row {index}: feed type is required.")
            continue
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            errors.append(f"Feed row {index}: enter a valid amount.")
            continue
        if amount <= 0:
            errors.append(f"Feed row {index}: amount must be greater than 0.")
            continue
        totals_by_type[feed_type] = totals_by_type.get(feed_type, 0.0) + amount
    for feed_type, amount in totals_by_type.items():
        items.append({"feed_type": feed_type, "amount": amount})
    return items, errors


def save_manual_feed_output_batch(db, rfid_tag, dispatch_date, timestamp, feed_items, note, audit_activity, audit_source):
    feed_note = note[:128] if note else None
    for item in feed_items:
        feed_type = item["feed_type"]
        amount = item["amount"]
        ensure_feed_type(db, feed_type)
        db.add(
            FeedCenter(
                RFIDTag=rfid_tag,
                Timestamp=timestamp,
                DispatchDate=dispatch_date,
                FeedType=feed_type,
                AllocatedAmountKg=amount,
                Source="Manual Upload",
                Note=feed_note,
            )
        )
        inventory = db.query(FeedInventory).filter(FeedInventory.FeedType == feed_type).first()
        if inventory:
            inventory.AvailableKg = max(inventory.AvailableKg - amount, 0.0)
            inventory.LastUpdated = dispatch_date
            db.add(inventory)
        db.add(
            FeedAudit(
                Timestamp=timestamp,
                FeedType=feed_type,
                Activity=audit_activity,
                QuantityKg=amount,
                Source=audit_source,
                Note=note,
            )
        )


@app.post("/admin/feed-upload")
def admin_feed_upload(request: Request, upload_file: UploadFile = File(...)):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    upload_id = str(uuid.uuid4())
    rows = parse_spreadsheet(upload_file)
    if not rows:
        if wants_json(request):
            return JSONResponse(status_code=422, content={"upload_id": upload_id, "errors": [{"row_number": None, "errors": ["Upload file contains no rows."]}]})
        return admin_redirect("Feed upload rejected. Upload file contains no rows.")
    parsed_rows = [validate_feed_upload_row(row, index + 2) for index, row in enumerate(rows)]
    seen_keys = set()
    errors = []
    for parsed in parsed_rows:
        key = (
            parsed["ear_tag"],
            parsed["timestamp"].isoformat() if parsed["timestamp"] else "",
        )
        if key in seen_keys:
            parsed["errors"].append("Duplicate row in upload for composite key [EarTagRFID, Timestamp].")
        seen_keys.add(key)
        if not parsed["errors"] and not db.query(Cow).filter(Cow.EarTagRFID == parsed["ear_tag"]).first():
            parsed["errors"].append("Cow RFID was not found.")
        if parsed["errors"]:
            errors.append(
                {
                    "row_number": parsed["row_number"],
                    "EarTagRFID": parsed["ear_tag"],
                    "Timestamp": parsed["timestamp"].isoformat() if parsed["timestamp"] else None,
                    "GreenFodderKg": parsed["green_fodder_kg"],
                    "ConcentrateFeedKg": parsed["concentrate_feed_kg"],
                    "errors": parsed["errors"],
                }
            )
    if errors:
        db.rollback()
        for parsed in parsed_rows:
            if not parsed["errors"]:
                continue
            db.add(
                StagingFeedUpload(
                    UploadID=upload_id,
                    RowNumber=parsed["row_number"],
                    EarTagRFID=parsed["ear_tag"] or None,
                    Timestamp=parsed["timestamp"],
                    GreenFodderKg=parsed["green_fodder_kg"],
                    ConcentrateFeedKg=parsed["concentrate_feed_kg"],
                    Status="Rejected",
                    ErrorMessage="; ".join(parsed["errors"])[:256],
                    RawData=json.dumps(parsed["raw"], default=str)[:512],
                )
            )
        db.commit()
        if wants_json(request):
            return JSONResponse(status_code=422, content={"upload_id": upload_id, "errors": errors})
        return admin_redirect(f"Feed upload rejected. {len(errors)} row(s) moved to Data Verification.")
    try:
        with db.begin_nested():
            ensure_feed_type(db, "Green Fodder")
            ensure_feed_type(db, "Concentrate")
            inserted_count = 0
            updated_count = 0
            for parsed in parsed_rows:
                dispatch_date = parsed["timestamp"].date()
                green_action = upsert_feed_upload_item(
                    db,
                    parsed["ear_tag"],
                    parsed["timestamp"],
                    dispatch_date,
                    "Green Fodder",
                    parsed["green_fodder_kg"],
                )
                concentrate_action = upsert_feed_upload_item(
                    db,
                    parsed["ear_tag"],
                    parsed["timestamp"],
                    dispatch_date,
                    "Concentrate",
                    parsed["concentrate_feed_kg"],
                )
                inserted_count += int(green_action == "Inserted") + int(concentrate_action == "Inserted")
                updated_count += int(green_action == "Updated") + int(concentrate_action == "Updated")
                db.add(
                    StagingFeedUpload(
                        UploadID=upload_id,
                        RowNumber=parsed["row_number"],
                        EarTagRFID=parsed["ear_tag"],
                        Timestamp=parsed["timestamp"],
                        GreenFodderKg=parsed["green_fodder_kg"],
                        ConcentrateFeedKg=parsed["concentrate_feed_kg"],
                        Status="Accepted",
                        RawData=json.dumps(parsed["raw"], default=str)[:512],
                    )
                )
            db.add(
                FeedAudit(
                    FeedType="Green Fodder",
                    Activity="Upload",
                    QuantityKg=sum(row["green_fodder_kg"] for row in parsed_rows),
                    Source="Feed CSV upload",
                    Note=f"{len(parsed_rows)} sessions, {inserted_count} inserted, {updated_count} updated",
                )
            )
            db.add(
                FeedAudit(
                    FeedType="Concentrate",
                    Activity="Upload",
                    QuantityKg=sum(row["concentrate_feed_kg"] for row in parsed_rows),
                    Source="Feed CSV upload",
                    Note=f"UploadID {upload_id}",
                )
            )
        db.commit()
    except ValueError as exc:
        db.rollback()
        error = {"row_number": None, "errors": [str(exc)]}
        if wants_json(request):
            return JSONResponse(status_code=422, content={"upload_id": upload_id, "errors": [error]})
        return admin_redirect(str(exc))
    if wants_json(request):
        return {"upload_id": upload_id, "inserted": inserted_count, "updated": updated_count, "errors": []}
    return admin_redirect(f"Feed upload committed. {inserted_count} feed rows inserted, {updated_count} updated.")


@app.post("/admin/upload")
def admin_upload(request: Request, entity: str = Form(...), upload_file: UploadFile = File(...)):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    try:
        rows = parse_spreadsheet(upload_file)
    except (UnicodeDecodeError, ValueError, csv.Error) as exc:
        return admin_redirect(f"Upload rejected: {exc}")
    if not rows:
        return admin_redirect("Upload rejected: no data rows found in the file.")
    rows, duplicate_count = discard_duplicate_upload_rows(rows)
    imported_count = 0
    skipped_count = duplicate_count
    skip_reasons = []
    existing_breeds = {breed.Name for breed in db.query(BreedModel).all()}
    existing_feed_types = {feed.Name for feed in db.query(FeedTypeModel).all()}

    def skip(row_number, reason):
        nonlocal skipped_count
        skipped_count += 1
        if len(skip_reasons) < 6:
            skip_reasons.append(f"row {row_number}: {reason}")

    if entity == "cows":
        for row_number, row in enumerate(rows, start=2):
            ear_tag = normalize_rfid(row.get("EarTagRFID", ""))
            if not is_valid_rfid(ear_tag):
                skip(row_number, "EarTagRFID must be exactly 15 digits")
                continue
            delete_flag = str(row.get("Delete", "")).strip().lower() in ("1", "true", "yes", "y")
            cow = db.query(Cow).filter(Cow.EarTagRFID == ear_tag).first()
            if delete_flag:
                if cow is not None:
                    db.query(DailyMilkingVitalsLog).filter(DailyMilkingVitalsLog.EarTagRFID == ear_tag).delete()
                    db.query(FeedCenter).filter(FeedCenter.RFIDTag == ear_tag).delete()
                    db.query(WaterIntakeLog).filter(WaterIntakeLog.EarTagRFID == ear_tag).delete()
                    db.delete(cow)
                    imported_count += 1
                else:
                    skip(row_number, "Delete requested but cow RFID was not found")
                continue
            visual = str(row.get("VisualID", "")).strip()
            cow_name = str(row.get("CowName", "")).strip() or None
            breed = str(row.get("Breed", "")).strip()
            age_years = parse_optional_nonnegative_float(row.get("AgeYears"))
            weight_kg = parse_optional_nonnegative_float(row.get("WeightKg"))
            status = normalize_cow_status(row.get("Status", ""))
            photo = row.get("PhotoURL")
            if not visual:
                skip(row_number, "VisualID is required")
                continue
            if not breed:
                skip(row_number, "Breed is required")
                continue
            if not status:
                skip(row_number, "Status must be Active, Dry, or Treatment")
                continue
            if cow is None:
                cow = Cow(EarTagRFID=ear_tag)
            cow.VisualID = visual
            cow.CowName = cow_name
            cow.Breed = breed
            cow.AgeYears = age_years
            cow.WeightKg = weight_kg
            cow.Status = status
            cow.PhotoURL = str(photo).strip() if photo else None
            if breed and breed not in existing_breeds:
                db.add(BreedModel(Name=breed))
                existing_breeds.add(breed)
            db.add(cow)
            imported_count += 1
    elif entity == "stations":
        for row_number, row in enumerate(rows, start=2):
            station_id = row.get("StationID")
            label = str(row.get("Label", "")).strip()
            if station_id is None or not label:
                skip(row_number, "StationID and Label are required")
                continue
            try:
                station_id = int(station_id)
            except (TypeError, ValueError):
                skip(row_number, "StationID must be a whole number")
                continue
            if station_id <= 0:
                skip(row_number, "StationID must be greater than 0")
                continue
            station = db.query(Station).filter(Station.StationID == station_id).first()
            if station is None:
                station = Station(StationID=station_id, Label=label)
            else:
                station.Label = label
            db.add(station)
            imported_count += 1
    elif entity == "feed_types":
        for row_number, row in enumerate(rows, start=2):
            name = str(row.get("Name", "")).strip()
            if not name:
                skip(row_number, "Name is required")
                continue
            if name not in existing_feed_types:
                db.add(FeedTypeModel(Name=name))
                existing_feed_types.add(name)
            imported_count += 1
    elif entity == "feed_inventory":
        for row_number, row in enumerate(rows, start=2):
            feed_type = str(row.get("FeedType", "")).strip()
            if not feed_type:
                skip(row_number, "FeedType is required")
                continue
            available = row.get("AvailableKg")
            try:
                available = float(available)
            except (TypeError, ValueError):
                skip(row_number, "AvailableKg must be a number")
                continue
            if available < 0:
                skip(row_number, "AvailableKg cannot be negative")
                continue
            last_updated_raw = row.get("LastUpdated")
            last_updated = date.today()
            if last_updated_raw:
                try:
                    last_updated = date.fromisoformat(str(last_updated_raw))
                except ValueError:
                    pass
            note = str(row.get("Note", "")).strip() or None
            if feed_type not in existing_feed_types:
                db.add(FeedTypeModel(Name=feed_type))
                existing_feed_types.add(feed_type)
            inventory = db.query(FeedInventory).filter(FeedInventory.FeedType == feed_type).first()
            if inventory is None:
                inventory = FeedInventory(FeedType=feed_type, AvailableKg=available, LastUpdated=last_updated, Note=note)
            else:
                inventory.AvailableKg = available
                inventory.LastUpdated = last_updated
                inventory.Note = note
            db.add(inventory)
            imported_count += 1
    else:
        raise HTTPException(status_code=400, detail="Unknown import entity")
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        detail = str(getattr(exc, "orig", exc))
        return admin_redirect(f"Upload failed while saving to database: {detail}")
    except SQLAlchemyError as exc:
        db.rollback()
        return admin_redirect(f"Upload failed while saving to database: {exc.__class__.__name__}")
    message = f"{entity.replace('_', ' ').title()} upload complete: {imported_count} imported, {skipped_count} skipped."
    if duplicate_count:
        message += f" {duplicate_count} duplicate row(s) skipped."
    if skip_reasons:
        message += " " + " | ".join(skip_reasons)
    if imported_count == 0:
        return admin_redirect(message)
    return dashboard_redirect(message)


@app.post("/admin/entry")
async def admin_entry(
    request: Request,
    entity: str = Form(...),
    EarTagRFID: str = Form(None),
    VisualID: str = Form(None),
    CowName: str = Form(None),
    Breed: str = Form(None),
    AgeYears: str = Form(None),
    WeightKg: str = Form(None),
    Status: str = Form(None),
    PhotoURL: str = Form(None),
    Delete: str = Form(None),
    StationID: str = Form(None),
    Label: str = Form(None),
    Name: str = Form(None),
    FeedType: str = Form(None),
    AvailableKg: str = Form(None),
    LastUpdated: str = Form(None),
    Note: str = Form(None),
    Date: str = Form(None),
    TotalStoredKg: str = Form(None),
    ConsumedKg: str = Form(None),
    RemainingKg: str = Form(None),
):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    if entity == "cows":
        ear_tag = normalize_rfid(EarTagRFID)
        if not is_valid_rfid(ear_tag):
            return admin_redirect("Cow was not saved. EarTagRFID must be exactly 15 digits.")
        delete_flag = str(Delete or "").strip().lower() in ("1", "true", "yes", "y")
        cow = db.query(Cow).filter(Cow.EarTagRFID == ear_tag).first()
        if delete_flag:
            if cow is not None:
                db.query(DailyMilkingVitalsLog).filter(DailyMilkingVitalsLog.EarTagRFID == ear_tag).delete()
                db.query(FeedCenter).filter(FeedCenter.RFIDTag == ear_tag).delete()
                db.query(WaterIntakeLog).filter(WaterIntakeLog.EarTagRFID == ear_tag).delete()
                db.delete(cow)
                db.commit()
                return admin_redirect("Cow deleted.")
            return admin_redirect("Cow delete skipped. RFID was not found.")
        visual = str(VisualID or "").strip() or (cow.VisualID if cow else "")
        cow_name = str(CowName or "").strip() or (cow.CowName if cow else None)
        breed = str(Breed or "").strip() or (cow.Breed if cow else "")
        age_years = parse_optional_nonnegative_float(AgeYears)
        if age_years is None and cow is not None and (AgeYears is None or str(AgeYears).strip() == ""):
            age_years = cow.AgeYears
        weight_kg = parse_optional_nonnegative_float(WeightKg)
        if weight_kg is None and cow is not None and (WeightKg is None or str(WeightKg).strip() == ""):
            weight_kg = cow.WeightKg
        status = normalize_cow_status(Status)
        if status is None and cow is not None and (Status is None or str(Status).strip() == ""):
            status = cow.Status
        photo = str(PhotoURL or "").strip() or None
        if photo is None and cow is not None and (PhotoURL is None or str(PhotoURL).strip() == ""):
            photo = cow.PhotoURL
        if not visual or not breed or not status:
            return admin_redirect("Cow was not saved. VisualID, Breed, and Status are required.")
        if cow is None:
            cow = Cow(EarTagRFID=ear_tag)
        cow.VisualID = visual
        cow.CowName = cow_name
        cow.Breed = breed
        cow.AgeYears = age_years
        cow.WeightKg = weight_kg
        cow.Status = status
        cow.PhotoURL = photo
        if breed and not db.query(BreedModel).filter(BreedModel.Name == breed).first():
            db.add(BreedModel(Name=breed))
        db.add(cow)
        db.commit()
        return admin_redirect(f"Cow {cow.VisualID} updated.")
    elif entity == "stations":
        station_id = str(StationID or "").strip()
        label = str(Label or "").strip()
        if not station_id or not label:
            return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
        try:
            station_id = int(station_id)
        except ValueError:
            return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
        if station_id <= 0:
            return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
        station = db.query(Station).filter(Station.StationID == station_id).first()
        if station is None:
            station = Station(StationID=station_id, Label=label)
        else:
            station.Label = label
        db.add(station)
    elif entity == "feed_types":
        name = str(Name or "").strip()
        if name and not db.query(FeedTypeModel).filter(FeedTypeModel.Name == name).first():
            db.add(FeedTypeModel(Name=name))
    elif entity == "feed_inventory":
        feed_type = str(FeedType or "").strip()
        if feed_type:
            try:
                available = float(AvailableKg or 0)
            except (TypeError, ValueError):
                available = None
            if available is not None:
                if available < 0:
                    return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
                last_updated = date.today()
                if LastUpdated:
                    try:
                        last_updated = date.fromisoformat(str(LastUpdated))
                    except ValueError:
                        pass
                note = str(Note or "").strip() or None
                if not db.query(FeedTypeModel).filter(FeedTypeModel.Name == feed_type).first():
                    db.add(FeedTypeModel(Name=feed_type))
                inventory = db.query(FeedInventory).filter(FeedInventory.FeedType == feed_type).first()
                if inventory is None:
                    inventory = FeedInventory(FeedType=feed_type, AvailableKg=available, LastUpdated=last_updated, Note=note)
                else:
                    inventory.AvailableKg = available
                    inventory.LastUpdated = last_updated
                    inventory.Note = note
                db.add(inventory)
    elif entity == "parameters":
        form = await request.form()
        parameter_values = {
            parameter.Key: form.get(parameter.Key)
            for parameter in db.query(OperationalParameter).all()
        }
        update_operational_parameters(db, parameter_values)
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    else:
        raise HTTPException(status_code=400, detail="Unknown entry entity")
    db.commit()
    return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)


@app.post("/admin/station-log")
def admin_station_log(
    request: Request,
    EarTagRFID: str = Form(...),
    StationID: str = Form(...),
    CowName: str = Form(None),
    YieldKg: str = Form(...),
    WeightSensorKg: str = Form(None),
    BodyTemperatureCelsius: str = Form(...),
):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    payload = {
        "EarTagRFID": normalize_rfid(EarTagRFID),
        "StationID": str(StationID or "").strip(),
        "CowName": str(CowName or "").strip(),
        "YieldKg": str(YieldKg or "").strip(),
        "WeightSensorKg": str(WeightSensorKg or "").strip(),
        "BodyTemperatureCelsius": str(BodyTemperatureCelsius or "").strip(),
    }
    if not is_valid_rfid(payload["EarTagRFID"]) or not payload["StationID"] or not payload["YieldKg"] or not payload["BodyTemperatureCelsius"]:
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    if not db.query(Cow).filter(Cow.EarTagRFID == payload["EarTagRFID"]).first():
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    try:
        station_id = int(payload["StationID"])
    except ValueError:
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    if station_id <= 0 or not db.query(Station).filter(Station.StationID == station_id).first():
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    try:
        yield_value = float(payload["YieldKg"])
        temperature_value = float(payload["BodyTemperatureCelsius"])
        if yield_value < 0 or temperature_value <= 0:
            return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
        if payload["WeightSensorKg"]:
            sensor_value = float(payload["WeightSensorKg"])
            if sensor_value < 0:
                return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
        result = record_station_session(db, payload)
    except (TypeError, ValueError):
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    if result.get("rejected"):
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)


@app.post("/admin/feed-output")
def admin_feed_output(
    request: Request,
    RFIDTag: str = Form(...),
    FeedType: List[str] = Form(...),
    AllocatedAmountKg: List[str] = Form(...),
    DispatchDate: str = Form(None),
    Note: str = Form(None),
    CorrectionReason: str = Form(None),
    CorrectionPassword: str = Form(None),
):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    rfid_tag = normalize_rfid(RFIDTag)
    note = str(Note or "").strip() or None
    correction_reason = str(CorrectionReason or "").strip()
    correction_password = str(CorrectionPassword or "")
    if not is_valid_rfid(rfid_tag):
        return admin_redirect("Feed output was not saved. Enter a valid cow RFID.")
    feed_items, feed_errors = parse_feed_output_items(FeedType, AllocatedAmountKg)
    if feed_errors:
        return admin_redirect("Feed output was not saved. " + " ".join(feed_errors[:3]))
    if not feed_items:
        return admin_redirect("Feed output was not saved. Enter at least one feed type and amount.")
    dispatch_date = date.today()
    if DispatchDate:
        try:
            dispatch_date = date.fromisoformat(str(DispatchDate))
        except ValueError:
            pass
    cow = db.query(Cow).filter(Cow.EarTagRFID == rfid_tag).first()
    if cow is None:
        return admin_redirect("Feed output was not saved. Cow RFID was not found.")
    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(minutes=30)
    recent_feeds = (
        db.query(FeedCenter)
        .filter(FeedCenter.RFIDTag == rfid_tag)
        .filter(FeedCenter.Timestamp >= recent_cutoff)
        .order_by(FeedCenter.Timestamp.desc(), FeedCenter.LogID.desc())
        .all()
    )
    if recent_feeds:
        if not correction_reason or correction_password != ADMIN_PASSWORD:
            return admin_redirect(
                "Feed was already recorded for this cow within 30 minutes. "
                "To correct it, enter a reason and the admin password."
            )
        old_summary = []
        for recent_feed in recent_feeds:
            old_summary.append(f"{recent_feed.AllocatedAmountKg:.2f} kg {recent_feed.FeedType}")
            old_inventory = db.query(FeedInventory).filter(FeedInventory.FeedType == recent_feed.FeedType).first()
            if old_inventory:
                old_inventory.AvailableKg += recent_feed.AllocatedAmountKg
                old_inventory.LastUpdated = dispatch_date
                db.add(old_inventory)
            db.delete(recent_feed)
        correction_note = f"{correction_reason} | Previous: {', '.join(old_summary)}"[:128]
        save_manual_feed_output_batch(
            db,
            rfid_tag,
            dispatch_date,
            now,
            feed_items,
            correction_note,
            "Corrected",
            "Admin feed correction",
        )
        db.commit()
        return admin_redirect("Feed correction saved with reason and admin password.")
    save_manual_feed_output_batch(db, rfid_tag, dispatch_date, now, feed_items, note, "Allocated", "Admin feed output")
    db.commit()
    return admin_redirect(f"Feed output saved for {len(feed_items)} feed type(s).")


@app.post("/admin/water-intake")
def admin_water_intake(
    request: Request,
    EarTagRFID: str = Form(...),
    WaterLiters: str = Form(...),
    IntakeDate: str = Form(None),
    Source: str = Form(None),
    Note: str = Form(None),
):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    ear_tag = normalize_rfid(EarTagRFID)
    source = str(Source or "").strip() or "Admin water entry"
    note = str(Note or "").strip() or None
    if not is_valid_rfid(ear_tag):
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    if not db.query(Cow).filter(Cow.EarTagRFID == ear_tag).first():
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    try:
        water_liters = float(WaterLiters)
    except (TypeError, ValueError):
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    if water_liters <= 0:
        return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)
    intake_date = date.today()
    if IntakeDate:
        try:
            intake_date = date.fromisoformat(str(IntakeDate))
        except ValueError:
            pass
    db.add(
        WaterIntakeLog(
            EarTagRFID=ear_tag,
            IntakeDate=intake_date,
            WaterLiters=water_liters,
            Source=source,
            Note=note,
        )
    )
    db.commit()
    return RedirectResponse(url=f"{BASE_PATH}/admin", status_code=303)


@app.post("/admin/upload-photo")
def upload_photo(request: Request, EarTagRFID: str = Form(...), upload_file: UploadFile = File(...)):
    if not is_admin_authenticated(request):
        return RedirectResponse(url=f"{BASE_PATH}/login", status_code=303)
    db: Session = request.state.db
    ear_tag = normalize_rfid(EarTagRFID)
    if not is_valid_rfid(ear_tag):
        raise HTTPException(status_code=400, detail="Missing EarTagRFID")
    if not upload_file.filename:
        raise HTTPException(status_code=400, detail="Missing photo file")
    if not upload_file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Photo file must be an image")
    filename = f"{ear_tag}_{os.path.basename(upload_file.filename)}"
    filename = filename.replace(" ", "_")
    upload_path = os.path.join("static", "uploads", filename)
    with open(upload_path, "wb") as out_file:
        shutil.copyfileobj(upload_file.file, out_file)
    cow = db.query(Cow).filter(Cow.EarTagRFID == ear_tag).first()
    if cow is None:
        raise HTTPException(status_code=404, detail="Cow not found")
    cow.PhotoURL = f"/static/uploads/{filename}"
    db.add(cow)
    db.commit()
    return RedirectResponse(url=f"{BASE_PATH}/", status_code=303)


@app.post("/api/station-log")
def station_log(payload: dict, request: Request):
    db: Session = request.state.db
    required = {"EarTagRFID", "StationID", "YieldKg", "BodyTemperatureCelsius"}
    if not required.issubset(payload):
        raise HTTPException(status_code=400, detail="Missing station log fields")
    result = record_station_session(db, payload)
    if result.get("rejected"):
        raise HTTPException(status_code=429, detail=f"Milking session rejected: {result['reason']}")
    return result




