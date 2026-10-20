import pytest
from datetime import datetime, timedelta, timezone
from models import (
    calculate_concentrate_release,
    check_milking_limit,
    apply_health_alert,
    record_station_session,
    get_station_cards,
    get_milking_cycle_bounds,
    initialize_database,
    SessionLocal,
    engine,
    Cow,
    DailyMilkingVitalsLog,
    FeedCenter,
    LiveStationState,
    Station,
)
from app import upsert_live_station_rows


def setup_module(module):
    initialize_database(engine)


def test_calculate_concentrate_release():
    assert calculate_concentrate_release(0) == 2.0
    assert calculate_concentrate_release(4.2) == 4.1
    assert calculate_concentrate_release(10.0) == 7.0


def test_milking_limit_rejection():
    db = SessionLocal()
    try:
        cow = db.query(Cow).first()
        assert cow is not None
        first_log = DailyMilkingVitalsLog(
            EarTagRFID=cow.EarTagRFID,
            StationID=1,
            Timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
            YieldKg=15.0,
            BodyTemperatureCelsius=38.0,
        )
        db.add(first_log)
        db.commit()
        assert check_milking_limit(db, cow.EarTagRFID) is True
    finally:
        db.close()


def test_health_alert_does_not_change_cow_status():
    db = SessionLocal()
    try:
        cow = db.query(Cow).filter(Cow.Status != "Treatment").first()
        assert cow is not None
        original_status = cow.Status
        result = apply_health_alert(db, cow.EarTagRFID, 39.6)
        db.refresh(cow)
        assert result is True
        assert cow.Status == original_status
    finally:
        db.close()


def test_record_station_session_does_not_log_feed_entry():
    db = SessionLocal()
    try:
        cow = db.query(Cow).filter(Cow.EarTagRFID == "100000000000998").first()
        if cow is None:
            cow = Cow(EarTagRFID="100000000000998", VisualID="COW-98", Breed="Jersey", Status="Active")
            db.add(cow)
            db.commit()
        db.query(DailyMilkingVitalsLog).filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID).delete()
        db.query(FeedCenter).filter(FeedCenter.RFIDTag == cow.EarTagRFID).delete()
        db.commit()
        payload = {
            "EarTagRFID": cow.EarTagRFID,
            "StationID": 2,
            "YieldKg": 8.0,
            "BodyTemperatureCelsius": 38.7,
        }
        result = record_station_session(db, payload)
        assert result["rejected"] is False
        assert result["release_amount"] == 6.0
        assert db.query(FeedCenter).filter(FeedCenter.RFIDTag == cow.EarTagRFID).count() == 0
    finally:
        db.close()


def test_record_station_session_rejects_same_milking_cycle():
    db = SessionLocal()
    try:
        cow = db.query(Cow).filter(Cow.EarTagRFID == "100000000000997").first()
        if cow is None:
            cow = Cow(EarTagRFID="100000000000997", VisualID="COW-97", Breed="Jersey", Status="Active")
            db.add(cow)
            db.commit()
        db.query(DailyMilkingVitalsLog).filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID).delete()
        db.commit()
        first_log = DailyMilkingVitalsLog(
            EarTagRFID=cow.EarTagRFID,
            StationID=1,
            Timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
            YieldKg=12.0,
            BodyTemperatureCelsius=38.2,
        )
        db.add(first_log)
        db.commit()
        payload = {
            "EarTagRFID": cow.EarTagRFID,
            "StationID": 1,
            "YieldKg": 9.0,
            "BodyTemperatureCelsius": 38.5,
        }
        result = record_station_session(db, payload)
        assert result["rejected"] is True
        assert "already milked" in result["reason"]
    finally:
        db.close()


def test_record_station_session_rejects_simultaneous_station_conflict():
    db = SessionLocal()
    try:
        cow = db.query(Cow).filter(Cow.EarTagRFID == "100000000000999").first()
        if cow is None:
            cow = Cow(EarTagRFID="100000000000999", VisualID="COW-99", Breed="Jersey", Status="Active")
            db.add(cow)
            db.commit()
        first_log = DailyMilkingVitalsLog(
            EarTagRFID=cow.EarTagRFID,
            StationID=1,
            Timestamp=datetime.now(timezone.utc) - timedelta(minutes=5),
            YieldKg=12.0,
            BodyTemperatureCelsius=38.2,
        )
        db.add(first_log)
        db.commit()
        payload = {
            "EarTagRFID": cow.EarTagRFID,
            "StationID": 2,
            "YieldKg": 9.0,
            "BodyTemperatureCelsius": 38.5,
        }
        result = record_station_session(db, payload)
        assert result["rejected"] is True
        assert "another station" in result["reason"]
    finally:
        db.close()


def test_live_station_json_rejects_second_log_in_same_cycle():
    db = SessionLocal()
    try:
        cow = db.query(Cow).filter(Cow.EarTagRFID == "100000000000996").first()
        if cow is None:
            cow = Cow(EarTagRFID="100000000000996", VisualID="COW-96", Breed="Jersey", Status="Active")
            db.add(cow)
            db.commit()
        cow.Status = "Active"
        db.add(cow)
        db.query(DailyMilkingVitalsLog).filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID).delete()
        db.commit()

        _, cycle_start, _ = get_milking_cycle_bounds(datetime.now(timezone.utc))
        first_timestamp = cycle_start + timedelta(minutes=30)
        second_timestamp = cycle_start + timedelta(hours=1)
        db.add(
            DailyMilkingVitalsLog(
                EarTagRFID=cow.EarTagRFID,
                StationID=1,
                Timestamp=first_timestamp,
                YieldKg=8.0,
                BodyTemperatureCelsius=38.4,
            )
        )
        db.commit()

        result = upsert_live_station_rows(
            db,
            [
                {
                    "StationID": 2,
                    "EarTagRFID": cow.EarTagRFID,
                    "VisualID": cow.VisualID,
                    "Timestamp": second_timestamp.isoformat(),
                    "WeightKg": 7.5,
                    "TemperatureCelsius": 38.6,
                }
            ],
        )

        assert result["finalized"] == 0
        assert "already milked" in result["errors"][0]["error"]
        assert db.query(DailyMilkingVitalsLog).filter(DailyMilkingVitalsLog.EarTagRFID == cow.EarTagRFID).count() == 1
    finally:
        db.close()


def test_station_cards_ignore_milk_logs_without_live_json():
    db = SessionLocal()
    try:
        station_id = 99
        cow = db.query(Cow).filter(Cow.EarTagRFID == "100000000000995").first()
        if cow is None:
            cow = Cow(EarTagRFID="100000000000995", VisualID="COW-95", Breed="Jersey", Status="Active")
            db.add(cow)
        station = db.query(Station).filter(Station.StationID == station_id).first()
        if station is None:
            station = Station(StationID=station_id, Label="Test Station")
            db.add(station)
        db.query(LiveStationState).filter(LiveStationState.StationID == station_id).delete()
        db.query(DailyMilkingVitalsLog).filter(DailyMilkingVitalsLog.StationID == station_id).delete()
        db.commit()
        db.add(
            DailyMilkingVitalsLog(
                EarTagRFID=cow.EarTagRFID,
                StationID=station_id,
                Timestamp=datetime.now(timezone.utc),
                YieldKg=10.0,
                BodyTemperatureCelsius=38.2,
            )
        )
        db.commit()

        card = next(item for item in get_station_cards(db) if item["station"] == station_id)

        assert card["station_status"] == "Waiting for JSON"
        assert card["yield"] == 0.0
        assert card["visual_id"] == "No cow"
    finally:
        db.close()
