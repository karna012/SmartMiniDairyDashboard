from collections import defaultdict
from datetime import datetime, timedelta, date, timezone
from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    func,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = "sqlite:///./data.db"
LOCAL_TIMEZONE = datetime.now().astimezone().tzinfo

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def normalize_station_timestamp(timestamp, now=None):
    if timestamp is None:
        return None
    now = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is not None:
        return timestamp.astimezone(timezone.utc)
    utc_timestamp = timestamp.replace(tzinfo=timezone.utc)
    if utc_timestamp > now + timedelta(minutes=1):
        return timestamp.replace(tzinfo=LOCAL_TIMEZONE).astimezone(timezone.utc)
    return utc_timestamp


def format_elapsed_label(timestamp, now=None):
    timestamp = normalize_station_timestamp(timestamp, now)
    if timestamp is None:
        return "Unknown"
    now = now or datetime.now(timezone.utc)
    elapsed = max(now - timestamp, timedelta())
    age_minutes = int(elapsed.total_seconds() // 60)
    if age_minutes < 60:
        return f"{age_minutes} min ago"
    return f"{age_minutes // 60} hr ago"


class Station(Base):
    __tablename__ = "stations"
    StationID = Column(Integer, primary_key=True)
    Label = Column(String(24), nullable=False)
    logs = relationship("DailyMilkingVitalsLog", back_populates="station")


class Breed(Base):
    __tablename__ = "breeds"
    Name = Column(String(32), primary_key=True)


class FeedType(Base):
    __tablename__ = "feed_types"
    Name = Column(String(32), primary_key=True)


class MilkQualityType(Base):
    __tablename__ = "milk_quality_types"
    Name = Column(String(32), primary_key=True)


class MilkInventory(Base):
    __tablename__ = "milk_inventory"
    Date = Column(Date, primary_key=True)
    MilkQualityType = Column(String(32), ForeignKey("milk_quality_types.Name"), primary_key=True)
    TotalStoredKg = Column(Float, nullable=False)
    ConsumedKg = Column(Float, nullable=False)
    RemainingKg = Column(Float, nullable=False)
    Note = Column(String(128), nullable=True)


class FeedInventory(Base):
    __tablename__ = "feed_inventory"
    FeedType = Column(String(32), ForeignKey("feed_types.Name"), primary_key=True)
    AvailableKg = Column(Float, nullable=False, default=0.0)
    LastUpdated = Column(Date, nullable=False, default=date.today)
    Note = Column(String(128), nullable=True)


class FeedAudit(Base):
    __tablename__ = "feed_audit"
    LogID = Column(Integer, primary_key=True, autoincrement=True)
    Timestamp = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    FeedType = Column(String(32), ForeignKey("feed_types.Name"), nullable=False)
    Activity = Column(String(24), nullable=False)
    QuantityKg = Column(Float, nullable=False)
    Source = Column(String(64), nullable=True)
    Note = Column(String(128), nullable=True)


class OperationalParameter(Base):
    __tablename__ = "operational_parameters"
    Key = Column(String(64), primary_key=True)
    Label = Column(String(96), nullable=False)
    Value = Column(Float, nullable=False)
    Unit = Column(String(24), nullable=True)
    Description = Column(String(160), nullable=True)


class Cow(Base):
    __tablename__ = "cows"
    __table_args__ = (
        CheckConstraint("length(EarTagRFID) = 15", name="eartag_length"),
        CheckConstraint("Status IN ('Active', 'Treatment', 'Dry')", name="cow_status_values"),
    )
    EarTagRFID = Column(String(15), primary_key=True, index=True)
    VisualID = Column(String(12), nullable=False)
    CowName = Column(String(64), nullable=True)
    Breed = Column(String(32), nullable=False)
    AgeYears = Column(Float, nullable=True)
    WeightKg = Column(Float, nullable=True)
    Status = Column(String(16), nullable=False, default="Active")
    PhotoURL = Column(String(256), nullable=True)
    logs = relationship("DailyMilkingVitalsLog", back_populates="cow")


class DailyMilkingVitalsLog(Base):
    __tablename__ = "daily_milking_vitals"
    LogID = Column(Integer, primary_key=True, autoincrement=True)
    EarTagRFID = Column(String(15), ForeignKey("cows.EarTagRFID"), nullable=False)
    StationID = Column(Integer, ForeignKey("stations.StationID"), nullable=False)
    Timestamp = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    YieldKg = Column(Float, nullable=False)
    WeightSensorKg = Column(Float, nullable=True)
    CowName = Column(String(64), nullable=True)
    BodyTemperatureCelsius = Column(Float, nullable=False)
    cow = relationship("Cow", back_populates="logs")
    station = relationship("Station", back_populates="logs")


class LiveStationState(Base):
    __tablename__ = "live_station_state"
    StationID = Column(Integer, primary_key=True)
    Timestamp = Column(DateTime(timezone=True), nullable=False)
    Status = Column(String(24), nullable=False, default="Idle")
    VisualID = Column(String(12), nullable=True)
    CowName = Column(String(64), nullable=True)
    TemperatureCelsius = Column(Float, nullable=True)
    WeightRaw = Column(String(32), nullable=True)
    MilkYieldKg = Column(Float, nullable=True)
    FeedDispensedKg = Column(Float, nullable=False, default=0.0)
    UpdatedAt = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class FeedCenter(Base):
    __tablename__ = "feed_center"
    __table_args__ = (
        CheckConstraint("length(RFIDTag) = 15", name="feed_rfid_length"),
    )
    LogID = Column(Integer, primary_key=True, autoincrement=True)
    RFIDTag = Column(String(15), ForeignKey("cows.EarTagRFID"), nullable=False)
    Timestamp = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    DispatchDate = Column(Date, nullable=False, default=date.today)
    FeedType = Column(String(32), nullable=False)
    AllocatedAmountKg = Column(Float, nullable=False)
    Source = Column(String(32), nullable=False, default="Manual Upload")
    Note = Column(String(128), nullable=True)


class StagingFeedUpload(Base):
    __tablename__ = "Staging_Feed_Upload"
    RowID = Column(Integer, primary_key=True, autoincrement=True)
    UploadID = Column(String(36), nullable=False)
    RowNumber = Column(Integer, nullable=False)
    EarTagRFID = Column(String(15), nullable=True)
    Timestamp = Column(DateTime(timezone=True), nullable=True)
    GreenFodderKg = Column(Float, nullable=True)
    ConcentrateFeedKg = Column(Float, nullable=True)
    Status = Column(String(16), nullable=False, default="Pending")
    ErrorMessage = Column(String(256), nullable=True)
    RawData = Column(String(512), nullable=True)
    CreatedAt = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class WaterIntakeLog(Base):
    __tablename__ = "water_intake"
    LogID = Column(Integer, primary_key=True, autoincrement=True)
    EarTagRFID = Column(String(15), ForeignKey("cows.EarTagRFID"), nullable=False)
    IntakeDate = Column(Date, nullable=False, default=date.today)
    WaterLiters = Column(Float, nullable=False)
    Source = Column(String(64), nullable=True)
    Note = Column(String(128), nullable=True)


def remove_duplicate_rows(engine):
    duplicate_specs = [
        (
            "daily_milking_vitals",
            "LogID",
            [
                "EarTagRFID",
                "StationID",
                "Timestamp",
                "YieldKg",
                "WeightSensorKg",
                "CowName",
                "BodyTemperatureCelsius",
            ],
        ),
        (
            "feed_center",
            "LogID",
            ["RFIDTag", "Timestamp", "DispatchDate", "FeedType", "AllocatedAmountKg"],
        ),
        (
            "feed_audit",
            "LogID",
            ["Timestamp", "FeedType", "Activity", "QuantityKg", "Source", "Note"],
        ),
        (
            "water_intake",
            "LogID",
            ["EarTagRFID", "IntakeDate", "WaterLiters", "Source", "Note"],
        ),
    ]
    with engine.begin() as conn:
        for table_name, primary_key, columns in duplicate_specs:
            table_exists = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
                {"table_name": table_name},
            ).first()
            if not table_exists:
                continue
            group_by = ", ".join(f'"{column}"' for column in columns)
            conn.execute(
                text(
                    f"""
                    DELETE FROM "{table_name}"
                    WHERE "{primary_key}" NOT IN (
                        SELECT MIN("{primary_key}")
                        FROM "{table_name}"
                        GROUP BY {group_by}
                    )
                    """
                )
            )


def ensure_unique_indexes(engine):
    unique_indexes = [
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_milking_vitals_row
        ON daily_milking_vitals (
            EarTagRFID,
            StationID,
            Timestamp,
            YieldKg,
            ifnull(WeightSensorKg, -1),
            ifnull(CowName, ''),
            BodyTemperatureCelsius
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_feed_center_row
        ON feed_center (
            RFIDTag,
            Timestamp,
            DispatchDate,
            FeedType,
            AllocatedAmountKg
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_feed_audit_row
        ON feed_audit (
            Timestamp,
            FeedType,
            Activity,
            QuantityKg,
            ifnull(Source, ''),
            ifnull(Note, '')
        )
        """,
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_water_intake_row
        ON water_intake (
            EarTagRFID,
            IntakeDate,
            WaterLiters,
            ifnull(Source, ''),
            ifnull(Note, '')
        )
        """,
    ]
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_feed_center_row"))
        for index_sql in unique_indexes:
            conn.execute(text(index_sql))


def initialize_database(engine):
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        try:
            result = conn.execute(text("PRAGMA table_info(daily_milking_vitals)"))
            existing_cols = {row[1] for row in result}
            if "WeightSensorKg" not in existing_cols:
                conn.execute(text("ALTER TABLE daily_milking_vitals ADD COLUMN WeightSensorKg FLOAT"))
            if "CowName" not in existing_cols:
                conn.execute(text("ALTER TABLE daily_milking_vitals ADD COLUMN CowName VARCHAR(64)"))
        except Exception:
            pass
        try:
            result = conn.execute(text("PRAGMA table_info(cows)"))
            existing_cols = {row[1] for row in result}
            if "CowName" not in existing_cols:
                conn.execute(text("ALTER TABLE cows ADD COLUMN CowName VARCHAR(64)"))
            if "AgeYears" not in existing_cols:
                conn.execute(text("ALTER TABLE cows ADD COLUMN AgeYears FLOAT"))
            if "WeightKg" not in existing_cols:
                conn.execute(text("ALTER TABLE cows ADD COLUMN WeightKg FLOAT"))
        except Exception:
            pass
        try:
            result = conn.execute(text("PRAGMA table_info(feed_center)"))
            existing_cols = {row[1] for row in result}
            if "Timestamp" not in existing_cols:
                conn.execute(text("ALTER TABLE feed_center ADD COLUMN Timestamp DATETIME"))
            if "Source" not in existing_cols:
                conn.execute(text("ALTER TABLE feed_center ADD COLUMN Source VARCHAR(32) NOT NULL DEFAULT 'Manual Upload'"))
            if "Note" not in existing_cols:
                conn.execute(text("ALTER TABLE feed_center ADD COLUMN Note VARCHAR(128)"))
            conn.execute(text("UPDATE feed_center SET Timestamp = DispatchDate WHERE Timestamp IS NULL"))
        except Exception:
            pass
    remove_duplicate_rows(engine)
    ensure_unique_indexes(engine)
    session = SessionLocal()
    try:
        default_parameters = {
            "milk_daily_target_kg": {
                "label": "Daily Milk Target",
                "value": 850.0,
                "unit": "kg",
                "description": "Expected total milk collection for the herd per day.",
            },
            "green_fodder_baseline_kg": {
                "label": "Green Fodder Baseline",
                "value": 700.0,
                "unit": "kg",
                "description": "Daily green fodder planning baseline for the herd.",
            },
            "concentrate_base_kg": {
                "label": "Concentrate Base Release",
                "value": 2.0,
                "unit": "kg",
                "description": "Base concentrate released for each completed station session.",
            },
            "concentrate_per_yield_kg": {
                "label": "Concentrate Per Milk Kg",
                "value": 0.5,
                "unit": "kg/kg",
                "description": "Extra concentrate released per kg of milk yield.",
            },
            "heat_alert_temperature_c": {
                "label": "Heat Alert Temperature",
                "value": 39.5,
                "unit": "C",
                "description": "Body temperature above this value marks the cow for treatment.",
            },
            "milking_interval_hours": {
                "label": "Milking Interval",
                "value": 8.0,
                "unit": "hours",
                "description": "Minimum time between accepted milking sessions for a cow.",
            },
            "station_conflict_window_minutes": {
                "label": "Station Conflict Window",
                "value": 20.0,
                "unit": "minutes",
                "description": "Recent-session window used to reject simultaneous station conflicts.",
            },
            "water_daily_baseline_liters": {
                "label": "Daily Water Baseline",
                "value": 1200.0,
                "unit": "liters",
                "description": "Expected daily drinking water availability for the herd.",
            },
        }
        for key, item in default_parameters.items():
            parameter = session.query(OperationalParameter).filter(OperationalParameter.Key == key).first()
            if parameter is None:
                session.add(
                    OperationalParameter(
                        Key=key,
                        Label=item["label"],
                        Value=item["value"],
                        Unit=item["unit"],
                        Description=item["description"],
                    )
                )
        session.commit()
    finally:
        session.close()


def clear_database(db):
    for table in reversed(Base.metadata.sorted_tables):
        db.execute(table.delete())
    try:
        db.execute(text("DELETE FROM sqlite_sequence"))
    except Exception:
        pass
    db.commit()


def seed_demo_data(force=False):
    session = SessionLocal()
    try:
        if not force and (
            session.query(DailyMilkingVitalsLog).count() > 0
            or session.query(FeedCenter).count() > 0
            or session.query(WaterIntakeLog).count() > 0
        ):
            return False
        if force:
            session.query(FeedAudit).delete()
            session.query(WaterIntakeLog).delete()
            session.query(FeedCenter).delete()
            session.query(DailyMilkingVitalsLog).delete()
            session.commit()

        cows = session.query(Cow).order_by(Cow.VisualID).limit(12).all()
        stations = session.query(Station).order_by(Station.StationID).all()
        if not cows or not stations:
            return False
        for cow_index, cow in enumerate(session.query(Cow).order_by(Cow.VisualID).all()):
            cow.CowName = cow.CowName or f"Cow {cow_index + 1:02d}"
            cow.AgeYears = cow.AgeYears if cow.AgeYears is not None else round(2.5 + (cow_index % 7) * 0.6, 1)
            cow.WeightKg = cow.WeightKg if cow.WeightKg is not None else round(330 + (cow_index % 9) * 18, 1)
            session.add(cow)

        today = date.today()
        milk_times = [(6, 15), (17, 45)]
        feed_plan = [
            ("Green Fodder", 18.0),
            ("Dry Grass", 4.5),
            ("Concentrate", 3.0),
            ("Hydroponic Feed", 2.5),
        ]
        water_sources = ["Trough 1", "Trough 2", "Auto Drinker A", "Auto Drinker B"]

        for day_offset in range(4):
            log_date = today - timedelta(days=day_offset)
            for cow_index, cow in enumerate(cows):
                for session_index, (hour, minute) in enumerate(milk_times):
                    yield_kg = round(6.8 + (cow_index % 5) * 0.75 + session_index * 0.55 - day_offset * 0.1, 2)
                    temperature = round(38.2 + (cow_index % 4) * 0.18 + (0.9 if cow_index == 5 and day_offset == 0 else 0.0), 1)
                    timestamp = datetime(
                        log_date.year,
                        log_date.month,
                        log_date.day,
                        hour,
                        minute,
                        tzinfo=timezone.utc,
                    )
                    station = stations[(cow_index + session_index) % len(stations)]
                    session.add(
                        DailyMilkingVitalsLog(
                            EarTagRFID=cow.EarTagRFID,
                            StationID=station.StationID,
                            Timestamp=timestamp,
                            YieldKg=yield_kg,
                            WeightSensorKg=yield_kg,
                            CowName=f"{cow.VisualID} Demo",
                            BodyTemperatureCelsius=temperature,
                        )
                    )
                    concentrate_kg = calculate_concentrate_release(yield_kg)
                    session.add(
                        FeedCenter(
                            RFIDTag=cow.EarTagRFID,
                            DispatchDate=log_date,
                            FeedType="Concentrate",
                            AllocatedAmountKg=concentrate_kg,
                        )
                    )
                    session.add(
                        FeedAudit(
                            Timestamp=timestamp,
                            FeedType="Concentrate",
                            Activity="Allocated",
                            QuantityKg=concentrate_kg,
                            Source=f"Demo Station {station.StationID}",
                            Note="Demo milking concentrate release",
                        )
                    )

                feed_type, base_amount = feed_plan[cow_index % len(feed_plan)]
                feed_amount = round(base_amount + (cow_index % 3) * 0.6, 2)
                session.add(
                    FeedCenter(
                        RFIDTag=cow.EarTagRFID,
                        DispatchDate=log_date,
                        FeedType=feed_type,
                        AllocatedAmountKg=feed_amount,
                    )
                )
                session.add(
                    FeedAudit(
                        Timestamp=datetime(log_date.year, log_date.month, log_date.day, 8, 30, tzinfo=timezone.utc),
                        FeedType=feed_type,
                        Activity="Allocated",
                        QuantityKg=feed_amount,
                        Source="Demo feed center",
                        Note="Demo daily feed output",
                    )
                )
                session.add(
                    WaterIntakeLog(
                        EarTagRFID=cow.EarTagRFID,
                        IntakeDate=log_date,
                        WaterLiters=round(32.0 + (cow_index % 6) * 4.5 - day_offset * 0.8, 2),
                        Source=water_sources[cow_index % len(water_sources)],
                        Note="Demo drinking water entry",
                    )
                )

        for feed_type, amount in {
            "Green Fodder": 920.0,
            "Hydroponic Feed": 420.0,
            "Dry Grass": 560.0,
            "Concentrate": 360.0,
        }.items():
            inventory = session.query(FeedInventory).filter(FeedInventory.FeedType == feed_type).first()
            if inventory:
                inventory.AvailableKg = amount
                inventory.LastUpdated = today
                inventory.Note = "Demo stock balance"
                session.add(inventory)

        treatment_cow = cows[5] if len(cows) > 5 else cows[-1]
        treatment_cow.Status = "Treatment"
        session.add(treatment_cow)
        session.commit()
        return True
    finally:
        session.close()


def get_last_log(db, ear_tag):
    return (
        db.query(DailyMilkingVitalsLog)
        .filter(DailyMilkingVitalsLog.EarTagRFID == ear_tag)
        .order_by(DailyMilkingVitalsLog.Timestamp.desc())
        .first()
    )


def get_milking_cycle_bounds(timestamp):
    timestamp = normalize_station_timestamp(timestamp) or datetime.now(timezone.utc)
    local_timestamp = timestamp.astimezone(LOCAL_TIMEZONE)
    cycle_date = local_timestamp.date()
    if local_timestamp.hour < 12:
        cycle_name = "morning"
        start_local = datetime(cycle_date.year, cycle_date.month, cycle_date.day, 0, 0, tzinfo=LOCAL_TIMEZONE)
        end_local = datetime(cycle_date.year, cycle_date.month, cycle_date.day, 12, 0, tzinfo=LOCAL_TIMEZONE)
    else:
        cycle_name = "evening"
        start_local = datetime(cycle_date.year, cycle_date.month, cycle_date.day, 12, 0, tzinfo=LOCAL_TIMEZONE)
        end_local = start_local + timedelta(hours=12)
    return cycle_name, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def get_existing_milking_cycle_log(db, ear_tag, timestamp, exclude_log_id=None):
    _, cycle_start, cycle_end = get_milking_cycle_bounds(timestamp)
    query = (
        db.query(DailyMilkingVitalsLog)
        .filter(DailyMilkingVitalsLog.EarTagRFID == ear_tag)
        .filter(DailyMilkingVitalsLog.Timestamp >= cycle_start)
        .filter(DailyMilkingVitalsLog.Timestamp < cycle_end)
    )
    if exclude_log_id is not None:
        query = query.filter(DailyMilkingVitalsLog.LogID != exclude_log_id)
    return query.order_by(DailyMilkingVitalsLog.Timestamp.desc()).first()


def check_milking_cycle_limit(db, ear_tag, timestamp):
    return get_existing_milking_cycle_log(db, ear_tag, timestamp) is not None


def get_operational_parameters(db):
    defaults = {
        "milk_daily_target_kg": 850.0,
        "green_fodder_baseline_kg": 700.0,
        "concentrate_base_kg": 2.0,
        "concentrate_per_yield_kg": 0.5,
        "heat_alert_temperature_c": 39.5,
        "milking_interval_hours": 8.0,
        "station_conflict_window_minutes": 20.0,
        "water_daily_baseline_liters": 1200.0,
    }
    rows = db.query(OperationalParameter).all()
    values = defaults.copy()
    values.update({row.Key: row.Value for row in rows})
    return values


def get_parameter_rows(db):
    return db.query(OperationalParameter).order_by(OperationalParameter.Label).all()


def update_operational_parameters(db, values):
    parameters = {row.Key: row for row in db.query(OperationalParameter).all()}
    for key, raw_value in values.items():
        if key not in parameters or raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if value < 0:
            continue
        parameters[key].Value = value
        db.add(parameters[key])
    db.commit()


def check_milking_limit(db, ear_tag, timestamp=None):
    timestamp = timestamp or datetime.now(timezone.utc)
    last_log = get_last_log(db, ear_tag)
    if not last_log:
        return False
    last_timestamp = last_log.Timestamp
    if last_timestamp.tzinfo is None:
        last_timestamp = last_timestamp.replace(tzinfo=timezone.utc)
    parameters = get_operational_parameters(db)
    return (timestamp - last_timestamp) < timedelta(hours=parameters["milking_interval_hours"])


def calculate_concentrate_release(yield_kg, parameters=None):
    parameters = parameters or {
        "concentrate_base_kg": 2.0,
        "concentrate_per_yield_kg": 0.5,
    }
    return round(parameters["concentrate_base_kg"] + (yield_kg * parameters["concentrate_per_yield_kg"]), 2)


def check_active_station_conflict(db, ear_tag, station_id, timestamp=None):
    timestamp = timestamp or datetime.now(timezone.utc)
    last_log = get_last_log(db, ear_tag)
    if not last_log:
        return False
    if last_log.StationID == int(station_id):
        return False
    last_timestamp = last_log.Timestamp
    if last_timestamp.tzinfo is None:
        last_timestamp = last_timestamp.replace(tzinfo=timezone.utc)
    parameters = get_operational_parameters(db)
    active_window = timedelta(minutes=parameters["station_conflict_window_minutes"])
    return (timestamp - last_timestamp) < active_window


def apply_health_alert(db, ear_tag, temperature, commit=True):
    parameters = get_operational_parameters(db)
    return temperature > parameters["heat_alert_temperature_c"]


def update_cow_from_station_data(db, cow, cow_name=None, visual_id=None, sensor_weight=None):
    if cow is None:
        return
    changed = False
    cow_name = str(cow_name or "").strip()
    visual_id = str(visual_id or "").strip()
    if cow_name and cow.CowName != cow_name:
        cow.CowName = cow_name
        changed = True
    if visual_id and cow.VisualID != visual_id:
        cow.VisualID = visual_id
        changed = True
    if sensor_weight is not None and cow.WeightKg is None:
        cow.WeightKg = float(sensor_weight)
        changed = True
    if changed:
        db.add(cow)


def ensure_feed_type_and_inventory(db, feed_type):
    if not db.query(FeedType).filter(FeedType.Name == feed_type).first():
        db.add(FeedType(Name=feed_type))
    inventory = db.query(FeedInventory).filter(FeedInventory.FeedType == feed_type).first()
    if inventory is None:
        inventory = FeedInventory(FeedType=feed_type, AvailableKg=0.0, LastUpdated=date.today())
        db.add(inventory)
    return inventory


def upsert_station_feed_release(db, ear_tag, timestamp, station_id, amount, feed_type="Concentrate"):
    existing = (
        db.query(FeedCenter)
        .filter(FeedCenter.RFIDTag == ear_tag)
        .filter(FeedCenter.Timestamp == timestamp)
        .filter(FeedCenter.FeedType == feed_type)
        .first()
    )
    inventory = ensure_feed_type_and_inventory(db, feed_type)
    if existing is None:
        activity = "Allocated"
        note = "Station concentrate release"
        existing = FeedCenter(
            RFIDTag=ear_tag,
            Timestamp=timestamp,
            DispatchDate=timestamp.date(),
            FeedType=feed_type,
            AllocatedAmountKg=amount,
            Source="Station JSON",
            Note=note,
        )
        inventory.AvailableKg = max(inventory.AvailableKg - amount, 0.0)
    else:
        old_amount = existing.AllocatedAmountKg
        activity = "Corrected"
        note = f"Station release updated from {old_amount:.2f} kg"
        existing.DispatchDate = timestamp.date()
        existing.AllocatedAmountKg = amount
        existing.Source = "Station JSON"
        existing.Note = note
        inventory.AvailableKg = max(inventory.AvailableKg + old_amount - amount, 0.0)
    inventory.LastUpdated = timestamp.date()
    db.add(existing)
    db.add(inventory)
    db.add(
        FeedAudit(
            Timestamp=timestamp,
            FeedType=feed_type,
            Activity=activity,
            QuantityKg=amount,
            Source=f"Station {station_id}",
            Note=note,
        )
    )
    return existing


def upsert_live_station_state(
    db,
    station_id,
    timestamp,
    status,
    visual_id,
    cow_name,
    temperature,
    weight_raw,
    milk_yield,
    feed_amount,
):
    live_state = db.query(LiveStationState).filter(LiveStationState.StationID == station_id).first()
    if live_state is None:
        live_state = LiveStationState(StationID=station_id)
    live_state.Timestamp = timestamp
    live_state.Status = status
    live_state.VisualID = visual_id
    live_state.CowName = cow_name
    live_state.TemperatureCelsius = temperature
    live_state.WeightRaw = weight_raw
    live_state.MilkYieldKg = milk_yield
    live_state.FeedDispensedKg = feed_amount
    live_state.UpdatedAt = datetime.now(timezone.utc)
    db.add(live_state)
    return live_state


def record_station_session(db, payload):
    ear_tag = payload["EarTagRFID"]
    timestamp = datetime.now(timezone.utc)
    cow = db.query(Cow).filter(Cow.EarTagRFID == ear_tag).first()
    if cow is None:
        return {"rejected": True, "reason": "Cow RFID was not found"}
    if cow.Status != "Active":
        return {"rejected": True, "reason": f"Cow status is {cow.Status}; only Active cows can be milked"}
    if check_active_station_conflict(db, ear_tag, payload["StationID"], timestamp):
        return {"rejected": True, "reason": "Cow already recorded at another station within the current milking window"}
    if check_milking_cycle_limit(db, ear_tag, timestamp):
        cycle_name, _, _ = get_milking_cycle_bounds(timestamp)
        return {"rejected": True, "reason": f"Cow already milked in the {cycle_name} cycle"}
    weight_sensor_value = payload.get("WeightSensorKg")
    if weight_sensor_value is not None and str(weight_sensor_value).strip() != "":
        weight_sensor_value = float(weight_sensor_value)
        yield_value = weight_sensor_value
    else:
        yield_value = float(payload["YieldKg"])
        weight_sensor_value = None
    cow_name_value = str(payload.get("CowName", "")).strip() or None
    if cow_name_value is None and cow is not None:
        cow_name_value = cow.CowName or cow.VisualID
    visual_id = str(payload.get("VisualID", "")).strip() or (cow.VisualID if cow else None)
    update_cow_from_station_data(db, cow, cow_name=cow_name_value, visual_id=visual_id)
    log = DailyMilkingVitalsLog(
        EarTagRFID=ear_tag,
        StationID=int(payload["StationID"]),
        Timestamp=timestamp,
        YieldKg=yield_value,
        WeightSensorKg=weight_sensor_value,
        CowName=cow_name_value,
        BodyTemperatureCelsius=float(payload["BodyTemperatureCelsius"]),
    )
    db.add(log)
    parameters = get_operational_parameters(db)
    status_alert = apply_health_alert(db, ear_tag, log.BodyTemperatureCelsius, commit=False)
    release_amount = calculate_concentrate_release(yield_value, parameters)
    upsert_live_station_state(
        db,
        int(payload["StationID"]),
        timestamp,
        "Complete",
        visual_id,
        cow_name_value,
        log.BodyTemperatureCelsius,
        str(weight_sensor_value if weight_sensor_value is not None else yield_value),
        yield_value,
        0.0,
    )
    db.commit()
    db.refresh(log)
    return {
        "rejected": False,
        "LogID": log.LogID,
        "release_amount": release_amount,
        "status_alert": status_alert,
    }


def get_station_cards(db):
    cards = []
    station_ids = {
        row[0]
        for row in db.query(Station.StationID).all()
    }
    station_ids.update(row[0] for row in db.query(LiveStationState.StationID).all())
    now = datetime.now(timezone.utc)
    parameters = get_operational_parameters(db)
    live_station_timeout = timedelta(minutes=2)
    for station_id in sorted(station_ids):
        station = db.query(Station).filter(Station.StationID == station_id).first()
        live_state = db.query(LiveStationState).filter(LiveStationState.StationID == station_id).first()
        if live_state:
            updated_at = live_state.UpdatedAt
            if updated_at is not None and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            last_ts = normalize_station_timestamp(live_state.Timestamp, now)
            is_stale = updated_at is None or (now - updated_at) > live_station_timeout
            if is_stale:
                cards.append(
                    {
                        "station": station_id,
                        "label": station.Label if station else f"Station {station_id}",
                        "visual_id": "No cow",
                        "cow_name": "",
                        "cow_status": "Idle",
                        "breed": "Unknown",
                        "yield": 0.0,
                        "sensor_weight": None,
                        "temperature": 0.0,
                        "feed_served": 0.0,
                        "feed_type": "",
                        "concentrate_release": 0.0,
                        "station_status": "Waiting for JSON",
                        "last_milked": "Waiting for JSON",
                        "overheat": False,
                        "timestamp": last_ts.isoformat() if last_ts else None,
                    }
                )
                continue
            cow = None
            if live_state.VisualID:
                cow = db.query(Cow).filter(Cow.VisualID == live_state.VisualID).first()
            latest_feed = None
            if cow:
                latest_feed = get_latest_manual_feed_for_cow(db, cow.EarTagRFID)
            last_milked = format_elapsed_label(live_state.Timestamp, now)
            feed_amount = latest_feed.AllocatedAmountKg if latest_feed else 0.0
            feed_type = latest_feed.FeedType if latest_feed else ""
            temperature = live_state.TemperatureCelsius or 0.0
            cards.append(
                {
                    "station": station_id,
                    "label": station.Label if station else f"Station {station_id}",
                    "visual_id": live_state.VisualID or "No cow",
                    "cow_name": live_state.CowName or (cow.CowName if cow else live_state.VisualID or ""),
                    "cow_status": live_state.Status,
                    "breed": cow.Breed if cow else "Unknown",
                    "yield": live_state.MilkYieldKg or 0.0,
                    "sensor_weight": live_state.MilkYieldKg,
                    "temperature": temperature,
                    "feed_served": feed_amount,
                    "feed_type": feed_type,
                    "concentrate_release": feed_amount,
                    "station_status": live_state.Status,
                    "last_milked": last_milked,
                    "overheat": temperature > parameters["heat_alert_temperature_c"],
                    "timestamp": last_ts.isoformat() if last_ts else None,
                }
            )
            continue
        cards.append(
            {
                "station": station_id,
                "label": station.Label if station else f"Station {station_id}",
                "visual_id": "No cow",
                "cow_name": "",
                "cow_status": "Idle",
                "breed": "Unknown",
                "yield": 0.0,
                "sensor_weight": None,
                "temperature": 0.0,
                "feed_served": 0.0,
                "feed_type": "",
                "concentrate_release": 0.0,
                "station_status": "Waiting for JSON",
                "last_milked": "Waiting for JSON",
                "overheat": False,
                "timestamp": None,
            }
        )
        continue
        latest = (
            db.query(DailyMilkingVitalsLog)
            .filter(DailyMilkingVitalsLog.StationID == station_id)
            .order_by(DailyMilkingVitalsLog.Timestamp.desc())
            .first()
        )
        if latest:
            cow = db.query(Cow).filter(Cow.EarTagRFID == latest.EarTagRFID).first()
            latest_feed = get_latest_manual_feed_for_cow(db, latest.EarTagRFID)
            last_ts = normalize_station_timestamp(latest.Timestamp, now)
            elapsed = now - last_ts if last_ts else None
            is_milking = elapsed is not None and elapsed <= timedelta(minutes=15)
            last_milked = format_elapsed_label(latest.Timestamp, now)
            cards.append(
                {
                    "station": station_id,
                    "label": station.Label if station else f"Station {station_id}",
                    "visual_id": latest.CowName or (cow.VisualID if cow else "Unknown"),
                    "cow_name": latest.CowName or (cow.VisualID if cow else "Unknown"),
                    "cow_status": cow.Status if cow else "Unknown",
                    "breed": cow.Breed if cow else "Unknown",
                    "yield": latest.YieldKg,
                    "sensor_weight": latest.WeightSensorKg,
                    "temperature": latest.BodyTemperatureCelsius,
                    "feed_served": latest_feed.AllocatedAmountKg if latest_feed else 0.0,
                    "feed_type": latest_feed.FeedType if latest_feed else "",
                    "concentrate_release": latest_feed.AllocatedAmountKg if latest_feed else 0.0,
                    "station_status": "Milking" if is_milking else "Idle",
                    "last_milked": last_milked,
                    "overheat": latest.BodyTemperatureCelsius > parameters["heat_alert_temperature_c"],
                    "timestamp": last_ts.isoformat() if last_ts else None,
                }
            )
        else:
            cards.append(
                {
                    "station": station_id,
                    "label": station.Label if station else f"Station {station_id}",
                    "visual_id": "No cow",
                    "cow_name": "No active cow",
                    "cow_status": "Idle",
                    "breed": "—",
                    "yield": 0.0,
                    "sensor_weight": None,
                    "temperature": 0.0,
                    "feed_served": 0.0,
                    "feed_type": "",
                    "concentrate_release": 0.0,
                    "station_status": "Idle",
                    "last_milked": "Never",
                    "overheat": False,
                    "timestamp": None,
                }
            )
    return cards


def get_latest_manual_feed_for_cow(db, ear_tag):
    return (
        db.query(FeedCenter)
        .filter(FeedCenter.RFIDTag == ear_tag)
        .filter(FeedCenter.DispatchDate == date.today())
        .filter(FeedCenter.Source != "Station JSON")
        .order_by(FeedCenter.Timestamp.desc(), FeedCenter.LogID.desc())
        .first()
    )


def get_herd_status_with_yield(db):
    seven_days = datetime.now(timezone.utc) - timedelta(days=7)
    today = date.today()
    rows = []
    cows = db.query(Cow).order_by(Cow.VisualID).all()
    for cow in cows:
        avg_yield = (
            db.query(func.avg(DailyMilkingVitalsLog.YieldKg))
            .filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID)
            .filter(DailyMilkingVitalsLog.Timestamp >= seven_days)
            .scalar()
        )
        water_today = (
            db.query(func.sum(WaterIntakeLog.WaterLiters))
            .filter(WaterIntakeLog.EarTagRFID == cow.EarTagRFID)
            .filter(WaterIntakeLog.IntakeDate == today)
            .scalar()
            or 0.0
        )
        rows.append(
            {
                "EarTagRFID": cow.EarTagRFID,
                "VisualID": cow.VisualID,
                "CowName": cow.CowName or cow.VisualID,
                "Breed": cow.Breed,
                "Status": cow.Status,
                "AgeYears": cow.AgeYears,
                "WeightKg": cow.WeightKg,
                "avg_yield": round(avg_yield or 0.0, 2),
                "water_today": round(water_today, 2),
                "photo_url": cow.PhotoURL,
            }
        )
    return rows


def get_cow_profile(db, ear_tag):
    cow = db.query(Cow).filter(Cow.EarTagRFID == ear_tag).first()
    if cow is None:
        return None
    seven_days = datetime.now(timezone.utc) - timedelta(days=7)
    today = date.today()
    avg_milk = (
        db.query(func.avg(DailyMilkingVitalsLog.YieldKg))
        .filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID)
        .filter(DailyMilkingVitalsLog.Timestamp >= seven_days)
        .scalar()
        or 0.0
    )
    total_milk_today = (
        db.query(func.sum(DailyMilkingVitalsLog.YieldKg))
        .filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID)
        .filter(func.date(DailyMilkingVitalsLog.Timestamp) == today)
        .scalar()
        or 0.0
    )
    sessions_today = (
        db.query(DailyMilkingVitalsLog)
        .filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID)
        .filter(func.date(DailyMilkingVitalsLog.Timestamp) == today)
        .count()
    )
    water_today = (
        db.query(func.sum(WaterIntakeLog.WaterLiters))
        .filter(WaterIntakeLog.EarTagRFID == cow.EarTagRFID)
        .filter(WaterIntakeLog.IntakeDate == today)
        .scalar()
        or 0.0
    )
    logs = (
        db.query(DailyMilkingVitalsLog)
        .filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID)
        .order_by(DailyMilkingVitalsLog.Timestamp.desc())
        .limit(100)
        .all()
    )
    transactions = []
    for log in logs:
        timestamp = log.Timestamp
        if timestamp is not None and timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        transactions.append(
            {
                "date": timestamp.date().isoformat() if timestamp else "",
                "time": timestamp.strftime("%H:%M") if timestamp else "",
                "station_id": log.StationID,
                "yield": round(log.YieldKg, 2),
                "temperature": round(log.BodyTemperatureCelsius, 1),
                "sensor_weight": round(log.WeightSensorKg, 2) if log.WeightSensorKg is not None else None,
                "cow_name": log.CowName,
            }
        )
    feed_logs = (
        db.query(FeedCenter)
        .filter(FeedCenter.RFIDTag == cow.EarTagRFID)
        .filter(FeedCenter.Source != "Station JSON")
        .order_by(FeedCenter.DispatchDate.desc(), FeedCenter.Timestamp.desc(), FeedCenter.LogID.desc())
        .limit(100)
        .all()
    )
    feed_transactions = [
        {
            "date": log.DispatchDate.isoformat() if log.DispatchDate else "",
            "time": log.Timestamp.strftime("%H:%M") if log.Timestamp else "",
            "feed_type": log.FeedType,
            "amount": round(log.AllocatedAmountKg, 2),
            "source": log.Source or "",
            "note": log.Note or "",
        }
        for log in feed_logs
    ]
    water_logs = (
        db.query(WaterIntakeLog)
        .filter(WaterIntakeLog.EarTagRFID == cow.EarTagRFID)
        .order_by(WaterIntakeLog.IntakeDate.desc(), WaterIntakeLog.LogID.desc())
        .limit(100)
        .all()
    )
    water_transactions = [
        {
            "date": log.IntakeDate.isoformat() if log.IntakeDate else "",
            "liters": round(log.WaterLiters, 2),
            "source": log.Source or "",
            "note": log.Note or "",
        }
        for log in water_logs
    ]
    latest_name = next((item["cow_name"] for item in transactions if item["cow_name"]), None)
    return {
        "ear_tag": cow.EarTagRFID,
        "visual_id": cow.VisualID,
        "cow_name": cow.CowName or latest_name or cow.VisualID,
        "breed": cow.Breed,
        "age_years": cow.AgeYears,
        "weight_kg": cow.WeightKg,
        "status": cow.Status,
        "photo_url": cow.PhotoURL,
        "avg_milk_7_day": round(avg_milk, 2),
        "total_milk_today": round(total_milk_today, 2),
        "sessions_today": sessions_today,
        "sessions_expected": 2,
        "sessions_remaining": max(2 - sessions_today, 0),
        "water_today": round(water_today, 2),
        "transactions": transactions,
        "feed_transactions": feed_transactions,
        "water_transactions": water_transactions,
    }


def normalize_feed_name(name):
    if not name:
        return ""
    name = name.strip().lower()
    for token in ("station", "feed", "center", "centre", "inventory", "stock"):
        name = name.replace(token, "")
    return " ".join(name.split())


def get_feed_inventory(db):
    today = date.today()
    raw_dispensed = db.query(FeedCenter.FeedType, func.sum(FeedCenter.AllocatedAmountKg).label("total"))
    raw_dispensed = (
        raw_dispensed.filter(FeedCenter.DispatchDate == today)
        .filter(FeedCenter.Source != "Station JSON")
        .group_by(FeedCenter.FeedType)
        .all()
    )
    dispensed_today = {row.FeedType: row.total for row in raw_dispensed}

    inventory_items = db.query(FeedInventory).order_by(FeedInventory.FeedType).all()
    inventory = []
    inventory_type_map = {item.FeedType: item for item in inventory_items}
    normalized_inventory_map = {
        normalize_feed_name(item.FeedType): item.FeedType for item in inventory_items
    }

    dispatch_map = defaultdict(float)
    untracked_dispatch = []
    for feed_type, total in dispensed_today.items():
        if feed_type in inventory_type_map:
            dispatch_map[feed_type] += total
            continue
        normalized = normalize_feed_name(feed_type)
        mapped = normalized_inventory_map.get(normalized)
        if mapped:
            dispatch_map[mapped] += total
        else:
            untracked_dispatch.append({"feed_type": feed_type, "total": round(total or 0.0, 2)})

    for item in inventory_items:
        used_today = dispatch_map.get(item.FeedType, 0.0) or 0.0
        remaining = round(max(item.AvailableKg, 0.0), 2)
        inventory.append(
            {
                "feed_type": item.FeedType,
                "available": round(item.AvailableKg, 2),
                "used_today": round(used_today, 2),
                "remaining": remaining,
                "note": item.Note or "",
            }
        )
    return inventory, untracked_dispatch


def get_milk_summary(db):
    today = date.today()
    parameters = get_operational_parameters(db)
    total_collected_today = (
        db.query(func.sum(DailyMilkingVitalsLog.YieldKg))
        .filter(func.date(DailyMilkingVitalsLog.Timestamp) == today)
        .scalar()
        or 0.0
    )
    return {
        "total_collected_today": round(total_collected_today, 2),
        "required_baseline": parameters["milk_daily_target_kg"],
        "remaining_to_target": round(max(parameters["milk_daily_target_kg"] - total_collected_today, 0.0), 2),
        "target_percent": round((total_collected_today / parameters["milk_daily_target_kg"]) * 100, 1)
        if parameters["milk_daily_target_kg"] > 0
        else 0.0,
        "formula": f"Milk target = {parameters['milk_daily_target_kg']:.0f} kg daily herd output",
    }


def get_milk_yield_report(db, period="today", start_date=None, end_date=None, group="milked"):
    today = date.today()
    period_days = {
        "today": 1,
        "48h": 2,
        "72h": 3,
        "1w": 7,
        "15d": 15,
        "1m": 30,
    }
    if period == "range" and start_date and end_date:
        range_start = start_date
        range_end = end_date
    else:
        days = period_days.get(period, 1)
        range_end = today
        range_start = today - timedelta(days=days - 1)

    cows = db.query(Cow).order_by(Cow.VisualID).all()
    total_cows = len(cows)
    cow_map = {cow.EarTagRFID: cow for cow in cows}
    logs = (
        db.query(
            DailyMilkingVitalsLog.EarTagRFID,
            func.sum(DailyMilkingVitalsLog.YieldKg).label("total_yield"),
            func.count(DailyMilkingVitalsLog.LogID).label("session_count"),
        )
        .filter(func.date(DailyMilkingVitalsLog.Timestamp) >= range_start)
        .filter(func.date(DailyMilkingVitalsLog.Timestamp) <= range_end)
        .group_by(DailyMilkingVitalsLog.EarTagRFID)
        .all()
    )
    yield_map = {row.EarTagRFID: row for row in logs}
    milked_rows = []
    not_milked_rows = []
    status_counts = defaultdict(int)
    status_rows = defaultdict(list)

    for cow in cows:
        yield_row = yield_map.get(cow.EarTagRFID)
        row = {
            "ear_tag": cow.EarTagRFID,
            "visual_id": cow.VisualID,
            "cow_name": cow.CowName or cow.VisualID,
            "breed": cow.Breed,
            "age_years": cow.AgeYears,
            "weight_kg": cow.WeightKg,
            "status": cow.Status,
            "photo_url": cow.PhotoURL,
            "total_yield": round((yield_row.total_yield if yield_row else 0.0) or 0.0, 2),
            "session_count": int((yield_row.session_count if yield_row else 0) or 0),
        }
        status_counts[cow.Status] += 1
        status_rows[cow.Status].append(row)
        if row["total_yield"] > 0:
            milked_rows.append(row)
        else:
            not_milked_rows.append(row)

    status_key = group.replace("status:", "", 1) if group.startswith("status:") else ""
    if group == "not_milked":
        selected_rows = not_milked_rows
        selected_label = "Cows Not Milked"
    elif group == "all":
        selected_rows = milked_rows + not_milked_rows
        selected_label = "All Cows"
    elif status_key:
        selected_rows = status_rows.get(status_key, [])
        selected_label = f"{status_key} Cows"
    else:
        selected_rows = milked_rows
        selected_label = "Cows That Gave Milk"

    total_yield = round(sum(row["total_yield"] for row in milked_rows), 2)
    return {
        "period": period,
        "start_date": range_start,
        "end_date": range_end,
        "group": group,
        "selected_label": selected_label,
        "total_cows": total_cows,
        "milked_count": len(milked_rows),
        "not_milked_count": len(not_milked_rows),
        "status_counts": dict(status_counts),
        "total_yield": total_yield,
        "rows": selected_rows,
        "period_options": [
            ("today", "Today"),
            ("48h", "48 hrs"),
            ("72h", "72 hrs"),
            ("1w", "1 week"),
            ("15d", "15 days"),
            ("1m", "1 month"),
        ],
    }


def get_feed_summary(db):
    today = date.today()
    parameters = get_operational_parameters(db)
    total_dispensed = (
        db.query(func.sum(FeedCenter.AllocatedAmountKg))
        .filter(FeedCenter.DispatchDate == today)
        .filter(FeedCenter.Source != "Station JSON")
        .scalar()
        or 0.0
    )
    inventory_rows, untracked_dispatch_rows = get_feed_inventory(db)
    total_available = round(sum(item["available"] for item in inventory_rows), 2)
    total_tracked_used = round(sum(item["used_today"] for item in inventory_rows), 2)
    total_remaining = round(sum(item["remaining"] for item in inventory_rows), 2)
    dispatch_rows = [
        {"feed_type": row.FeedType, "total": round(row.total or 0.0, 2)}
        for row in db.query(FeedCenter.FeedType, func.sum(FeedCenter.AllocatedAmountKg).label("total"))
        .filter(FeedCenter.DispatchDate == today)
        .filter(FeedCenter.Source != "Station JSON")
        .group_by(FeedCenter.FeedType)
        .all()
    ]
    untracked_dispatched = round(sum(row["total"] for row in untracked_dispatch_rows), 2)
    baseline = parameters["green_fodder_baseline_kg"]
    suggestions = []
    if total_remaining < baseline * 0.5:
        suggestions.append(
            "Total remaining fodder is below half the herd baseline. Plan a restock soon."
        )
    if untracked_dispatched > 0:
        suggestions.append(
            f"{untracked_dispatched:.2f} kg dispatched from feed types not currently in inventory tracking."
        )
        suggestions.append(
            "Add or map these feed types in inventory to keep dispatch and stock totals aligned."
        )
    for item in inventory_rows:
        if item["available"] <= 0:
            suggestions.append(f"{item['feed_type']} is out of stock. Reorder immediately.")
        elif item["remaining"] <= 0:
            suggestions.append(f"{item['feed_type']} has no remaining usable stock. Switch to an alternate feed type.")
        elif item["remaining"] <= max(10.0, item["available"] * 0.15):
            suggestions.append(
                f"{item['feed_type']} is low ({item['remaining']:.2f} kg left). Top up or rotate feed supply."
            )
        elif item["used_today"] > 0:
            days_left = item["remaining"] / item["used_today"]
            if days_left < 3:
                suggestions.append(
                    f"{item['feed_type']} will last about {days_left:.1f} days at current usage."
                )
    if not suggestions:
        suggestions.append("Fodder levels are healthy across all tracked feed types.")
    return {
        "total_dispensed": round(total_dispensed, 2),
        "dispatch_rows": dispatch_rows,
        "untracked_dispatch_rows": untracked_dispatch_rows,
        "untracked_dispatched": untracked_dispatched,
        "total_available": total_available,
        "total_tracked_used": total_tracked_used,
        "total_remaining": total_remaining,
        "baseline_green_fodder": baseline,
        "formula": f"Total green fodder needed = {baseline:.0f} kg baseline for herd",
        "inventory_rows": inventory_rows,
        "suggestions": suggestions,
    }


def get_data_verification_queue(db, limit=25):
    rows = (
        db.query(StagingFeedUpload)
        .filter(StagingFeedUpload.Status == "Rejected")
        .order_by(StagingFeedUpload.CreatedAt.desc(), StagingFeedUpload.RowNumber.asc())
        .limit(limit)
        .all()
    )
    return [
        {
            "row_number": row.RowNumber,
            "ear_tag": row.EarTagRFID or "",
            "timestamp": row.Timestamp.isoformat() if row.Timestamp else "",
            "green_fodder_kg": row.GreenFodderKg,
            "concentrate_feed_kg": row.ConcentrateFeedKg,
            "error": row.ErrorMessage or "Validation failed",
            "created_at": row.CreatedAt,
        }
        for row in rows
    ]


def get_feed_reconciliation_report(db, report_date=None):
    report_date = report_date or date.today()
    rows = (
        db.query(FeedCenter.RFIDTag, FeedCenter.Timestamp)
        .filter(FeedCenter.DispatchDate == report_date)
        .filter(FeedCenter.Source != "Station JSON")
        .all()
    )
    session_keys = {
        (row.RFIDTag, row.Timestamp.isoformat() if row.Timestamp else "")
        for row in rows
    }
    successful_sessions = len(session_keys)
    expected_sessions = 48
    return {
        "date": report_date,
        "successful_sessions": successful_sessions,
        "expected_sessions": expected_sessions,
        "deviation": successful_sessions - expected_sessions,
        "has_alert": successful_sessions != expected_sessions,
    }


def get_inventory_history(db, period="today", start_date=None, end_date=None):
    today = date.today()
    period_days = {
        "today": 1,
        "yesterday": 1,
        "1w": 7,
        "15d": 15,
        "1m": 30,
    }
    if period == "range" and start_date and end_date:
        range_start = start_date
        range_end = end_date
    elif period == "yesterday":
        range_start = today - timedelta(days=1)
        range_end = today - timedelta(days=1)
    else:
        days = period_days.get(period, 1)
        range_end = today
        range_start = today - timedelta(days=days - 1)

    daily_rows = [
        {
            "date": row.DispatchDate,
            "total_used": round(row.total or 0.0, 2),
        }
        for row in (
            db.query(FeedCenter.DispatchDate, func.sum(FeedCenter.AllocatedAmountKg).label("total"))
            .filter(FeedCenter.DispatchDate >= range_start)
            .filter(FeedCenter.DispatchDate <= range_end)
            .filter(FeedCenter.Source != "Station JSON")
            .group_by(FeedCenter.DispatchDate)
            .order_by(FeedCenter.DispatchDate.desc())
            .all()
        )
    ]

    summary_rows = [
        {
            "feed_type": row.FeedType,
            "used": round(row.total or 0.0, 2),
        }
        for row in (
            db.query(FeedCenter.FeedType, func.sum(FeedCenter.AllocatedAmountKg).label("total"))
            .filter(FeedCenter.DispatchDate >= range_start)
            .filter(FeedCenter.DispatchDate <= range_end)
            .filter(FeedCenter.Source != "Station JSON")
            .group_by(FeedCenter.FeedType)
            .order_by(FeedCenter.FeedType)
            .all()
        )
    ]

    total_used = round(sum(row["used"] for row in summary_rows), 2)
    used_today = next((row["total_used"] for row in daily_rows if row["date"] == today), 0.0)
    used_yesterday = next((row["total_used"] for row in daily_rows if row["date"] == today - timedelta(days=1)), 0.0)

    inventory_rows, untracked = get_feed_inventory(db)
    total_remaining = round(sum(item["remaining"] for item in inventory_rows), 2)
    total_available = round(sum(item["available"] for item in inventory_rows), 2)

    return {
        "period": period,
        "start_date": range_start,
        "end_date": range_end,
        "period_options": [
            ("today", "Today"),
            ("yesterday", "Yesterday"),
            ("1w", "1 Week"),
            ("15d", "15 Days"),
            ("1m", "1 Month"),
        ],
        "total_used": total_used,
        "used_today": used_today,
        "used_yesterday": used_yesterday,
        "summary_rows": summary_rows,
        "daily_rows": daily_rows,
        "inventory_rows": inventory_rows,
        "total_remaining": total_remaining,
        "total_available": total_available,
        "untracked_dispatch_rows": untracked,
    }


def get_water_summary(db):
    today = date.today()
    parameters = get_operational_parameters(db)
    total_water_today = (
        db.query(func.sum(WaterIntakeLog.WaterLiters))
        .filter(WaterIntakeLog.IntakeDate == today)
        .scalar()
        or 0.0
    )
    water_rows = [
        {
            "visual_id": row.VisualID,
            "ear_tag": row.EarTagRFID,
            "liters": round(row.total or 0.0, 2),
        }
        for row in (
            db.query(Cow.VisualID, Cow.EarTagRFID, func.sum(WaterIntakeLog.WaterLiters).label("total"))
            .join(WaterIntakeLog, WaterIntakeLog.EarTagRFID == Cow.EarTagRFID)
            .filter(WaterIntakeLog.IntakeDate == today)
            .group_by(Cow.VisualID, Cow.EarTagRFID)
            .order_by(func.sum(WaterIntakeLog.WaterLiters).desc())
            .all()
        )
    ]
    baseline = parameters["water_daily_baseline_liters"]
    remaining = round(max(baseline - total_water_today, 0.0), 2)
    percent = round((total_water_today / baseline) * 100, 1) if baseline > 0 else 0.0
    suggestions = []
    if total_water_today <= 0:
        suggestions.append("No water intake has been recorded today. Check drinker availability and update intake logs.")
    elif percent < 50:
        suggestions.append("Water intake is below half of the daily herd baseline. Inspect troughs and refill points.")
    elif percent < 80:
        suggestions.append("Water intake is progressing but still below the daily baseline. Keep monitoring through the day.")
    else:
        suggestions.append("Water intake is healthy against the current daily baseline.")
    return {
        "total_water_today": round(total_water_today, 2),
        "baseline_liters": baseline,
        "remaining_liters": remaining,
        "target_percent": percent,
        "water_rows": water_rows,
        "suggestions": suggestions,
        "formula": f"Water baseline = {baseline:.0f} liters daily herd intake",
    }
