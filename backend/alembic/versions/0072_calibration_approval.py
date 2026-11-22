"""Nullable VEH proof, one two-number approval request, B3-owned process originals."""

from sqlalchemy import text

from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = depends_on = None

OLD_TYPES = ("TYPE", "VEH", "CP", "ORG", "CPREP", "ORGREP", "DOC-KIND", "DOCUMENT")
OLD_FILE = "extension IN ('pdf','png','jpg','gif','webp','docx','xlsx','pptx','bin') AND stored_name<>'' AND original_upload_name<>'' AND storage_ref LIKE 'disk:/%'"


def history_types(types):
    op.create_check_constraint(
        "ck_arm2_history_object_type",
        "arm2_history_events",
        "object_type IN (" + ",".join("'" + t + "'" for t in types) + ")",
    )
    op.create_check_constraint(
        "ck_arm2_history_object_id",
        "arm2_history_events",
        " OR ".join(f"(object_type='{t}' AND object_id ~ '^{t}-[0-9]{{6,}}$')" for t in types),
    )


def upgrade():
    op.execute("""
    ALTER TABLE arm2_vehicles
      ADD calibration_m3 double precision,
      ADD calibration_confirmed_at timestamptz,
      ADD calibration_confirmed_by integer REFERENCES users(id) ON DELETE RESTRICT,
      ADD calibration_document_id varchar CONSTRAINT fk_arm2_calibration_document REFERENCES arm2_documents(id) ON DELETE RESTRICT,
      ADD calibration_revision_id varchar CONSTRAINT fk_arm2_calibration_revision REFERENCES arm2_document_revisions(id) ON DELETE RESTRICT,
      ADD calibration_plate_number varchar,
      ADD CONSTRAINT fk_arm2_calibration_revision_document FOREIGN KEY(calibration_revision_id,calibration_document_id)
        REFERENCES arm2_document_revisions(id,document_id) ON DELETE RESTRICT,
      ADD CONSTRAINT ck_arm2_vehicle_calibration CHECK (
        (calibration_m3 IS NULL AND calibration_confirmed_at IS NULL AND calibration_confirmed_by IS NULL AND calibration_document_id IS NULL AND calibration_revision_id IS NULL AND calibration_plate_number IS NULL)
        OR (calibration_m3 IS NOT NULL AND calibration_m3>0 AND calibration_m3<'Infinity'::float8 AND calibration_confirmed_at IS NOT NULL AND calibration_confirmed_by IS NOT NULL AND calibration_document_id IS NOT NULL AND calibration_revision_id IS NOT NULL AND calibration_plate_number IS NOT NULL));
    ALTER TABLE arm2_document_files ADD process_id varchar,
      ADD generated_bundle boolean NOT NULL DEFAULT false,
      ALTER COLUMN document_id DROP NOT NULL,
      ADD CONSTRAINT uq_arm2_file_process UNIQUE(id,process_id);
    CREATE SEQUENCE arm2_approval_id_seq;
    CREATE SEQUENCE arm2_approval_item_id_seq;
    CREATE TABLE arm2_approval_requests (
      id varchar PRIMARY KEY, name varchar NOT NULL,
      contract_number_1 varchar NOT NULL, contract_number_2 varchar NOT NULL,
      vehicles_snapshot jsonb NOT NULL, letter_file_id varchar NOT NULL, package_file_id varchar NOT NULL,
      response_file_ids jsonb NOT NULL, last_response_at timestamptz, created_at timestamptz NOT NULL,
      author_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT, version integer NOT NULL,
      CONSTRAINT ck_arm2_approval_request CHECK(id ~ '^APPROVAL-[0-9]{6,}$' AND version>0 AND btrim(contract_number_1)<>'' AND btrim(contract_number_2)<>''),
      CONSTRAINT ck_arm2_approval_snapshot CHECK(jsonb_typeof(vehicles_snapshot)='array' AND jsonb_array_length(vehicles_snapshot) BETWEEN 1 AND 100 AND jsonb_typeof(response_file_ids)='array'),
      CONSTRAINT fk_arm2_approval_letter FOREIGN KEY(letter_file_id) REFERENCES arm2_document_files(id) DEFERRABLE INITIALLY DEFERRED,
      CONSTRAINT fk_arm2_approval_package FOREIGN KEY(package_file_id) REFERENCES arm2_document_files(id) DEFERRABLE INITIALLY DEFERRED,
      CONSTRAINT fk_arm2_approval_letter_owner FOREIGN KEY(letter_file_id,id) REFERENCES arm2_document_files(id,process_id) DEFERRABLE INITIALLY DEFERRED,
      CONSTRAINT fk_arm2_approval_package_owner FOREIGN KEY(package_file_id,id) REFERENCES arm2_document_files(id,process_id) DEFERRABLE INITIALLY DEFERRED);
    ALTER TABLE arm2_document_files
      ADD CONSTRAINT fk_arm2_file_process FOREIGN KEY(process_id) REFERENCES arm2_approval_requests(id) ON DELETE RESTRICT,
      ADD CONSTRAINT ck_arm2_file_owner CHECK ((document_id IS NOT NULL AND process_id IS NULL AND NOT generated_bundle) OR (document_id IS NULL AND process_id IS NOT NULL));
    CREATE TABLE arm2_approval_decisions (
      id varchar PRIMARY KEY, request_id varchar NOT NULL REFERENCES arm2_approval_requests(id) ON DELETE RESTRICT,
      vehicle_id varchar NOT NULL REFERENCES arm2_vehicles(id) ON DELETE RESTRICT,
      plate_number varchar NOT NULL, volume_m3 double precision NOT NULL, state varchar NOT NULL,
      response_file_id varchar REFERENCES arm2_document_files(id) ON DELETE RESTRICT,
      decided_at timestamptz, decided_by integer REFERENCES users(id) ON DELETE RESTRICT, version integer NOT NULL,
      CONSTRAINT uq_arm2_approval_request_vehicle UNIQUE(request_id,vehicle_id),
      CONSTRAINT ck_arm2_approval_decision CHECK(id ~ '^APPROVAL-ITEM-[0-9]{6,}$' AND version>0 AND volume_m3>0 AND volume_m3<'Infinity'::float8),
      CONSTRAINT ck_arm2_approval_evidence CHECK((state='PENDING' AND response_file_id IS NULL AND decided_at IS NULL AND decided_by IS NULL) OR (state IN ('APPROVED','REJECTED') AND response_file_id IS NOT NULL AND decided_at IS NOT NULL AND decided_by IS NOT NULL)),
      CONSTRAINT fk_arm2_approval_response_owner FOREIGN KEY(response_file_id,request_id) REFERENCES arm2_document_files(id,process_id) ON DELETE RESTRICT);
    CREATE TABLE arm2_approval_attempts (
      id varchar PRIMARY KEY, action varchar NOT NULL, target_id varchar NOT NULL,
      principal_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      fingerprint varchar NOT NULL, plan jsonb NOT NULL, result jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_arm2_approval_attempt CHECK(id ~ '^[0-9a-f-]{36}$' AND action IN ('calibration','letter','response-stage','response') AND fingerprint ~ '^[0-9a-f]{64}$'));
    """)
    op.create_index("ix_arm2_approval_created", "arm2_approval_requests", ["created_at"])
    op.create_index(
        "ix_arm2_approval_characteristics",
        "arm2_approval_decisions",
        ["vehicle_id", "plate_number", "volume_m3", "decided_at"],
    )
    op.create_index(
        "ix_arm2_approval_attempt_principal", "arm2_approval_attempts", ["principal_id", "created_at"]
    )
    op.drop_constraint("ck_arm2_file_metadata", "arm2_document_files", type_="check")
    op.create_check_constraint(
        "ck_arm2_file_metadata",
        "arm2_document_files",
        "((extension IN ('pdf','png','jpg','gif','webp','docx','xlsx','pptx','bin') AND NOT generated_bundle) OR (extension='zip' AND generated_bundle AND process_id IS NOT NULL)) AND stored_name<>'' AND original_upload_name<>'' AND storage_ref LIKE 'disk:/%'",
    )
    for name in ("ck_arm2_history_object_type", "ck_arm2_history_object_id"):
        op.drop_constraint(name, "arm2_history_events", type_="check")
    history_types((*OLD_TYPES, "APPROVAL", "APPROVAL-ITEM"))


def downgrade():
    conn = op.get_bind()
    if any(
        conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table})"))
        for table in ("arm2_approval_requests", "arm2_approval_decisions", "arm2_approval_attempts")
    ) or conn.scalar(text("SELECT EXISTS(SELECT 1 FROM arm2_vehicles WHERE calibration_m3 IS NOT NULL)")):
        raise RuntimeError(
            "0072 downgrade refused: confirmation, approval or retry evidence exists; no records/files discarded"
        )
    op.execute("""
    ALTER TABLE arm2_document_files DROP CONSTRAINT fk_arm2_file_process;
    DROP TABLE arm2_approval_decisions;
    DROP TABLE arm2_approval_requests;
    DROP TABLE arm2_approval_attempts;
    ALTER TABLE arm2_document_files DROP CONSTRAINT ck_arm2_file_owner,
      DROP CONSTRAINT ck_arm2_file_metadata, DROP CONSTRAINT uq_arm2_file_process,
      DROP process_id, DROP generated_bundle, ALTER COLUMN document_id SET NOT NULL;
    ALTER TABLE arm2_vehicles DROP CONSTRAINT fk_arm2_calibration_revision_document,
      DROP CONSTRAINT ck_arm2_vehicle_calibration,
      DROP calibration_m3, DROP calibration_confirmed_at, DROP calibration_confirmed_by,
      DROP calibration_document_id, DROP calibration_revision_id, DROP calibration_plate_number;
    DROP SEQUENCE arm2_approval_id_seq;
    DROP SEQUENCE arm2_approval_item_id_seq;
    """)
    op.create_check_constraint("ck_arm2_file_metadata", "arm2_document_files", OLD_FILE)
    for name in ("ck_arm2_history_object_type", "ck_arm2_history_object_id"):
        op.drop_constraint(name, "arm2_history_events", type_="check")
    history_types(OLD_TYPES)
