"""Immutable file bundles, retry ledger, and audited naming presets."""

import sqlalchemy as sa

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade():
    # Legacy types remain unconfigured NULL. No backfill, version bump or hidden business change.
    op.add_column("arm2_document_kinds", sa.Column("file_naming_template", sa.String(), nullable=True))
    op.create_check_constraint(
        "ck_arm2_kind_file_naming",
        "arm2_document_kinds",
        "file_naming_template IS NULL OR file_naming_template IN ('DOCUMENT_NAME','PARENT_AND_KIND')",
    )
    for sequence in ["arm2_document_revision_id_seq", "arm2_document_file_id_seq"]:
        op.execute(sa.schema.CreateSequence(sa.Sequence(sequence)))
    op.execute("""CREATE TABLE arm2_document_revisions (
      id varchar PRIMARY KEY, document_id varchar NOT NULL REFERENCES arm2_documents(id) ON DELETE RESTRICT,
      number integer NOT NULL, file_ids jsonb NOT NULL, author_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
      occurred_at timestamptz NOT NULL DEFAULT now(), source varchar NOT NULL,
      CONSTRAINT uq_arm2_revision_document UNIQUE(id,document_id), CONSTRAINT uq_arm2_revision_number UNIQUE(document_id,number),
      CONSTRAINT ck_arm2_revision_identity CHECK(id ~ '^DOCUMENT-REV-[0-9]{6,}$' AND number>0 AND source='arm2-api-manual'),
      CONSTRAINT ck_arm2_revision_manifest CHECK(jsonb_typeof(file_ids)='array' AND jsonb_array_length(file_ids)<=32))""")
    op.add_column("arm2_documents", sa.Column("current_revision_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_arm2_document_current_revision",
        "arm2_documents",
        "arm2_document_revisions",
        ["current_revision_id", "id"],
        ["id", "document_id"],
        ondelete="RESTRICT",
    )
    op.execute("""CREATE TABLE arm2_document_files (
      id varchar PRIMARY KEY, document_id varchar NOT NULL REFERENCES arm2_documents(id) ON DELETE RESTRICT,
      original_upload_name varchar NOT NULL, stored_name varchar NOT NULL, storage_ref varchar NOT NULL UNIQUE,
      size bigint NOT NULL, sha256 varchar NOT NULL, media_type varchar NOT NULL, extension varchar NOT NULL,
      CONSTRAINT uq_arm2_file_document UNIQUE(id,document_id), CONSTRAINT uq_arm2_document_file_sha UNIQUE(document_id,sha256),
      CONSTRAINT ck_arm2_file_identity CHECK(id ~ '^FILE-[0-9]{6,}$' AND size>0 AND sha256 ~ '^[0-9a-f]{64}$'),
      CONSTRAINT ck_arm2_file_metadata CHECK(extension IN ('pdf','png','jpg','gif','webp','docx','xlsx','pptx') AND stored_name<>'' AND original_upload_name<>'' AND storage_ref LIKE 'disk:/%'))""")
    op.execute("""CREATE TABLE arm2_document_revision_files (
      revision_id varchar NOT NULL, position integer NOT NULL, document_id varchar NOT NULL, file_id varchar NOT NULL,
      PRIMARY KEY(revision_id,position), CONSTRAINT uq_arm2_revision_file UNIQUE(revision_id,file_id),
      FOREIGN KEY(revision_id,document_id) REFERENCES arm2_document_revisions(id,document_id) ON DELETE RESTRICT,
      FOREIGN KEY(file_id,document_id) REFERENCES arm2_document_files(id,document_id) ON DELETE RESTRICT,
      CONSTRAINT ck_arm2_revision_file_position CHECK(position>=0 AND position<32))""")
    op.execute("""CREATE TABLE arm2_document_file_stages (
      id varchar PRIMARY KEY, document_id varchar NOT NULL REFERENCES arm2_documents(id) ON DELETE RESTRICT,
      principal_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT, session_hash varchar NOT NULL,
      file_id varchar NOT NULL UNIQUE, original_upload_name varchar NOT NULL, size bigint NOT NULL,
      sha256 varchar, media_type varchar, extension varchar, state varchar NOT NULL, expires_at timestamptz NOT NULL,
      CONSTRAINT ck_arm2_stage_identity CHECK(id ~ '^[0-9a-f]{64}$' AND file_id ~ '^FILE-[0-9]{6,}$' AND size>0 AND state IN ('empty','ready','consumed')))""")
    op.execute("""CREATE TABLE arm2_document_file_acceptances (
      id varchar PRIMARY KEY, document_id varchar NOT NULL REFERENCES arm2_documents(id) ON DELETE RESTRICT,
      principal_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT, session_hash varchar NOT NULL,
      expected_version integer NOT NULL, fingerprint varchar NOT NULL, plan jsonb NOT NULL, state varchar NOT NULL,
      result jsonb, expires_at timestamptz NOT NULL,
      CONSTRAINT ck_arm2_acceptance_identity CHECK(id ~ '^[0-9a-f-]{36}$' AND expected_version>0 AND state IN ('prepared','uploaded','accepted')))""")
    # Accepted bytes and full historical compositions cannot be rewritten or physically removed.
    op.execute("""CREATE FUNCTION arm2_immutable_document_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN RAISE EXCEPTION 'Accepted document artifacts are immutable'; END $$""")
    for table in ["arm2_document_revisions", "arm2_document_files", "arm2_document_revision_files"]:
        op.execute(
            f"CREATE TRIGGER immutable_artifact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION arm2_immutable_document_artifact()"
        )
    op.execute("""CREATE FUNCTION arm2_check_document_manifest() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE target varchar; expected jsonb; actual jsonb;
      BEGIN
        IF TG_TABLE_NAME='arm2_documents' THEN target:=NEW.current_revision_id;
        ELSIF TG_TABLE_NAME='arm2_document_revisions' THEN target:=NEW.id;
        ELSE target:=NEW.revision_id; END IF;
        IF target IS NOT NULL THEN
          SELECT file_ids INTO expected FROM arm2_document_revisions WHERE id=target;
          SELECT coalesce(jsonb_agg(file_id ORDER BY position),'[]'::jsonb) INTO actual FROM arm2_document_revision_files WHERE revision_id=target;
          IF expected IS DISTINCT FROM actual THEN RAISE EXCEPTION 'Document revision manifest is incomplete or immutable'; END IF;
        END IF;
        RETURN NEW;
      END $$""")
    for table in ["arm2_documents", "arm2_document_revisions", "arm2_document_revision_files"]:
        op.execute(
            f"CREATE CONSTRAINT TRIGGER complete_document_manifest AFTER INSERT OR UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION arm2_check_document_manifest()"
        )


def downgrade():
    db = op.get_bind()
    tables = [
        "arm2_document_revision_files",
        "arm2_document_files",
        "arm2_document_revisions",
        "arm2_document_file_stages",
        "arm2_document_file_acceptances",
    ]
    db.execute(
        sa.text(
            "LOCK TABLE "
            + ",".join(["arm2_documents", "arm2_document_kinds", "arm2_history_events", *tables])
            + " IN ACCESS EXCLUSIVE MODE"
        )
    )
    occupied = any(db.scalar(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})")) for table in tables)
    occupied |= db.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM arm2_documents WHERE current_revision_id IS NOT NULL) OR EXISTS(SELECT 1 FROM arm2_document_kinds WHERE file_naming_template IS NOT NULL)"
        )
    )
    occupied |= db.scalar(
        sa.text("""SELECT EXISTS(SELECT 1 FROM arm2_history_events WHERE
      (object_type='DOCUMENT' AND changes ? 'current_revision_id' AND
        (changes->'current_revision_id'->'before' <> 'null'::jsonb OR changes->'current_revision_id'->'after' <> 'null'::jsonb))
      OR (object_type='DOC-KIND' AND changes ? 'file_naming_template' AND
        (changes->'file_naming_template'->'before' <> 'null'::jsonb OR changes->'file_naming_template'->'after' <> 'null'::jsonb))
      OR related_objects @> '[{"object_type":"FILE"}]'::jsonb OR related_objects @> '[{"object_type":"DOCUMENT-REV"}]'::jsonb)""")
    )
    if occupied:
        raise RuntimeError(
            "DOCUMENT files/revisions/naming settings/history would be lost; downgrade refused without deleting data"
        )
    op.drop_constraint("fk_arm2_document_current_revision", "arm2_documents", type_="foreignkey")
    op.execute("DROP TRIGGER complete_document_manifest ON arm2_documents")
    op.drop_column("arm2_documents", "current_revision_id")
    for table in tables:
        op.drop_table(table)
    op.execute("DROP FUNCTION arm2_immutable_document_artifact()")
    op.execute("DROP FUNCTION arm2_check_document_manifest()")
    op.drop_constraint("ck_arm2_kind_file_naming", "arm2_document_kinds", type_="check")
    op.drop_column("arm2_document_kinds", "file_naming_template")
    for sequence in ["arm2_document_revision_id_seq", "arm2_document_file_id_seq"]:
        op.execute(sa.schema.DropSequence(sa.Sequence(sequence)))
