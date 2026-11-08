from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.ids import next_id


def test_vehicle_ids_are_persistent_non_reusable_and_concurrent(database_url):
    engine = create_engine(database_url, poolclass=NullPool)

    with Session(engine) as db:
        type_id = next_id(db, "TYPE")
        assert type_id == "TYPE-000001"
        first = next_id(db, "VEH")
        second = next_id(db, "VEH")
        db.commit()

    with Session(engine) as db:
        rolled_back = next_id(db, "VEH")
        db.rollback()

    with Session(engine) as db:
        after_rollback = next_id(db, "VEH")
        db.commit()

    assert [first, second, rolled_back, after_rollback] == [
        "VEH-000001",
        "VEH-000002",
        "VEH-000003",
        "VEH-000004",
    ]

    def issue_id():
        with Session(engine) as db:
            value = next_id(db, "VEH")
            db.commit()
            return value

    with ThreadPoolExecutor(max_workers=2) as executor:
        parallel = list(executor.map(lambda _: issue_id(), range(2)))

    assert set(parallel) == {"VEH-000005", "VEH-000006"}
    engine.dispose()
