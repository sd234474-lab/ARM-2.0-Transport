from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.arm2.ids import is_canonical_id, next_id
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectDefinition, ObjectRegistry, ObjectRegistryError
from app.arm2.reference_paths import reference_id

ID_CHECKS = (
    ("TYPE", "arm2_vehicle_types", "ck_arm2_vehicle_types_id"),
    ("VEH", "arm2_vehicles", "ck_arm2_vehicles_id"),
    ("CP", "arm2_counterparties", "ck_arm2_counterparties_id"),
    ("ORG", "arm2_organizations", "ck_arm2_organizations_id"),
    ("CPREP", "nsi_counterparty_representatives", "ck_cprep_id"),
    ("ORGREP", "nsi_organization_representatives", "ck_orgrep_id"),
    ("DOC-KIND", "arm2_document_kinds", "ck_arm2_document_kinds_id"),
    ("DOCUMENT", "arm2_documents", "ck_arm2_documents_id"),
    ("APPROVAL", "arm2_approval_requests", "ck_arm2_approval_request"),
    ("APPROVAL-ITEM", "arm2_approval_decisions", "ck_arm2_approval_decision"),
    ("FILE", "arm2_document_files", "ck_arm2_file_identity"),
    ("DOCUMENT-REV", "arm2_document_revisions", "ck_arm2_revision_identity"),
)

ID_CASES = (
    ("000001", True), ("0000001", True), ("1" * 100, True), ("00001", False), ("000001 ", False),
    ("000001\n", False), ("٠٠٠٠٠١", False), ("۰۰۰۰۰۱", False), ("０００００１", False),
    ("⁰⁰⁰⁰⁰¹", False), ("00000١", False),
)


def id_cases(prefix):
    return [(f"{prefix}-{number}", valid) for number, valid in ID_CASES] + [
        ("WRONG-000001", False), (f" {prefix}-000001", False), (f"{prefix.lower()}-000001", False),
        (None, False), (1, False), (True, False), ([], False), ({}, False),
    ]


def id_catalog_check(conn, table, name):
    expression, not_null, max_length = conn.execute(text("""
        SELECT pg_get_expr(c.conbin, c.conrelid), a.attnotnull, a.atttypmod
        FROM pg_constraint AS c JOIN pg_attribute AS a ON a.attrelid=c.conrelid AND a.attname='id'
        WHERE c.contype='c' AND c.conrelid=to_regclass(:table) AND c.conname=:name
    """), {"table": table, "name": name}).one()
    assert not_null and max_length == -1
    return expression


def assert_id_parity(conn, expression, prefix, validate, *, object_type=None):
    statement = text(f"""
        SELECT ({expression}) IS TRUE FROM (SELECT CAST(:identifier AS varchar) AS id, :kind AS object_type,
            1 AS version, 'A' AS contract_number_1, 'B' AS contract_number_2, 1.0 AS volume_m3,
            1 AS number, 'arm2-api-manual' AS source, 1 AS size, repeat('0', 64) AS sha256) AS candidate
    """)
    for identifier, valid in id_cases(prefix):
        assert is_canonical_id(prefix, identifier) is valid
        if valid:
            validate(identifier)
        else:
            with pytest.raises(ValueError):
                validate(identifier)
        if isinstance(identifier, str) or identifier is None:
            assert conn.scalar(statement, {"identifier": identifier, "kind": object_type or prefix}) is valid


@pytest.mark.parametrize(("kind", "table", "constraint"), ID_CHECKS)
def test_canonical_id_matches_migrated_postgresql_checks(database_url, kind, table, constraint):
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0076"
            assert_id_parity(conn, id_catalog_check(conn, table, constraint), kind,
                             lambda value: reference_id(kind, value, "test"))
            if kind not in {"FILE", "DOCUMENT-REV"}:
                history_check = conn.scalar(text("""
                    SELECT pg_get_expr(conbin, conrelid) FROM pg_constraint
                    WHERE conrelid='arm2_history_events'::regclass AND conname='ck_arm2_history_object_id'
                """))
                assert history_check
                assert_id_parity(conn, history_check.replace("object_id", "id"), kind,
                                 OBJECT_REGISTRY.require(kind).validate_id)
    finally:
        engine.dispose()


def test_canonical_id_synthetic_registry_extension_uses_its_own_exact_prefix(database_url):
    registry = ObjectRegistry()
    registry.register(ObjectDefinition("SYNTHETIC", "SYNTH-EXT", {}, {}))
    definition = registry.require("SYNTHETIC")
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("""
                CREATE TEMP TABLE canonical_id_extension (
                    id varchar NOT NULL CONSTRAINT ck_extension_id CHECK (id ~ '^SYNTH-EXT-[0-9]{6,}$')
                )
            """)
            assert_id_parity(conn, id_catalog_check(conn, "canonical_id_extension", "ck_extension_id"),
                             "SYNTH-EXT", definition.validate_id, object_type="SYNTHETIC")
            with pytest.raises(ObjectRegistryError):
                definition.validate_id("SYNTHETIC-000001")
    finally:
        engine.dispose()


def test_vehicle_ids_are_persistent_non_reusable_and_concurrent(isolated_arm2_database_url):
    engine = create_engine(isolated_arm2_database_url, poolclass=NullPool)

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
