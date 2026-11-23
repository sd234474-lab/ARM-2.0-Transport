"""Document lifecycle, immutable parent snapshots and protected correction audit."""

import sqlalchemy as sa

from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_arm2_documents_archive", "arm2_documents", type_="check")
    op.create_check_constraint(
        "ck_arm2_documents_archive", "arm2_documents", "archive_state IN ('Неархивный','Архивный')"
    )
    op.drop_constraint("ck_arm2_revision_identity", "arm2_document_revisions", type_="check")
    op.create_check_constraint(
        "ck_arm2_revision_identity",
        "arm2_document_revisions",
        "id ~ '^DOCUMENT-REV-[0-9]{6,}$' AND number>0 AND source IN ('arm2-api-manual','arm2-api-correct')",
    )
    op.execute("""CREATE TABLE arm2_document_revision_contexts (
      revision_id varchar PRIMARY KEY, document_id varchar NOT NULL, parent_type varchar NOT NULL, parent_id varchar NOT NULL,
      _cp varchar GENERATED ALWAYS AS (CASE WHEN parent_type='CP' THEN parent_id END) STORED REFERENCES arm2_counterparties(id) ON DELETE RESTRICT,
      _org varchar GENERATED ALWAYS AS (CASE WHEN parent_type='ORG' THEN parent_id END) STORED REFERENCES arm2_organizations(id) ON DELETE RESTRICT,
      _veh varchar GENERATED ALWAYS AS (CASE WHEN parent_type='VEH' THEN parent_id END) STORED REFERENCES arm2_vehicles(id) ON DELETE RESTRICT,
      FOREIGN KEY(revision_id,document_id) REFERENCES arm2_document_revisions(id,document_id) ON DELETE RESTRICT,
      CONSTRAINT ck_arm2_revision_context_parent CHECK((parent_type='CP' AND parent_id ~ '^CP-[0-9]{6,}$') OR (parent_type='ORG' AND parent_id ~ '^ORG-[0-9]{6,}$') OR (parent_type='VEH' AND parent_id ~ '^VEH-[0-9]{6,}$')))
    """)
    # B3-C has no MOVE: its current parent is exactly the historical parent.
    # This only adds context rows; original revision/file/membership rows stay intact.
    op.execute("""INSERT INTO arm2_document_revision_contexts(revision_id,document_id,parent_type,parent_id)
      SELECT r.id,r.document_id,d.parent_type,d.parent_id FROM arm2_document_revisions r JOIN arm2_documents d ON d.id=r.document_id""")
    op.execute("""CREATE TABLE arm2_document_file_corrections (
      file_id varchar PRIMARY KEY, document_id varchar NOT NULL, reason_code varchar NOT NULL,
      actor_id integer NOT NULL REFERENCES users(id) ON DELETE RESTRICT, occurred_at timestamptz NOT NULL DEFAULT now(),
      operation varchar NOT NULL, correlation_id varchar NOT NULL UNIQUE,
      history_event_id bigint NOT NULL UNIQUE REFERENCES arm2_history_events(event_id) ON DELETE RESTRICT,
      FOREIGN KEY(file_id,document_id) REFERENCES arm2_document_files(id,document_id) ON DELETE RESTRICT,
      CONSTRAINT ck_arm2_correction_identity CHECK(reason_code='incorrect_upload' AND operation='CORRECT' AND correlation_id ~ '^[0-9a-f-]{36}$'))""")
    for table in ("arm2_document_revision_contexts", "arm2_document_file_corrections"):
        op.execute(
            f"CREATE TRIGGER immutable_artifact BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION arm2_immutable_document_artifact()"
        )
    op.execute("""CREATE FUNCTION arm2_check_document_correction() RETURNS trigger LANGUAGE plpgsql AS $$
      DECLARE docid varchar; target varchar;
      BEGIN
        IF TG_TABLE_NAME='arm2_documents' THEN docid:=NEW.id; ELSE docid:=NEW.document_id; END IF;
        SELECT current_revision_id INTO target FROM arm2_documents WHERE id=docid;
        IF EXISTS(SELECT 1 FROM arm2_document_revision_files f JOIN arm2_document_file_corrections c ON c.file_id=f.file_id
          WHERE f.revision_id=target) THEN RAISE EXCEPTION 'Corrected file cannot be current'; END IF;
        IF TG_TABLE_NAME='arm2_document_file_corrections' THEN
        IF NOT EXISTS(
          SELECT 1 FROM arm2_history_events h WHERE h.event_id=NEW.history_event_id AND h.object_type='DOCUMENT'
          AND h.object_id=docid AND h.source='arm2-api-correct' AND h.operation='UPDATED'
          AND h.actor_id=NEW.actor_id::text AND h.correlation_id=NEW.correlation_id AND h.changes ? 'current_revision_id'
          AND h.changes->'current_revision_id'->>'after'=target)
        THEN RAISE EXCEPTION 'Correction requires matching common history'; END IF;
        END IF;
        RETURN NEW;
      END $$""")
    for table in ("arm2_documents", "arm2_document_file_corrections"):
        op.execute(
            f"CREATE CONSTRAINT TRIGGER document_correction_guard AFTER INSERT OR UPDATE ON {table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION arm2_check_document_correction()"
        )
    op.execute("""CREATE FUNCTION arm2_check_revision_context() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN IF NOT EXISTS(SELECT 1 FROM arm2_document_revision_contexts WHERE revision_id=NEW.id)
      THEN RAISE EXCEPTION 'Revision requires immutable parent context'; END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER revision_context_guard AFTER INSERT ON arm2_document_revisions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION arm2_check_revision_context()"
    )


def downgrade():
    db = op.get_bind()
    db.execute(
        sa.text(
            "LOCK TABLE arm2_documents,arm2_document_file_corrections,arm2_document_revision_contexts,arm2_document_revisions,arm2_history_events IN ACCESS EXCLUSIVE MODE"
        )
    )
    occupied = db.scalar(
        sa.text("""SELECT EXISTS(SELECT 1 FROM arm2_documents WHERE archive_state='Архивный')
      OR EXISTS(SELECT 1 FROM arm2_document_file_corrections)
      OR EXISTS(SELECT 1 FROM arm2_document_revisions WHERE source='arm2-api-correct')
      OR EXISTS(SELECT 1 FROM arm2_document_revision_contexts c JOIN arm2_documents d ON d.id=c.document_id WHERE (c.parent_type,c.parent_id) IS DISTINCT FROM (d.parent_type,d.parent_id))
      OR EXISTS(SELECT 1 FROM arm2_history_events WHERE object_type='DOCUMENT' AND operation='UPDATED'
         AND (changes ? 'parent_id' OR changes ? 'kind_ref' OR changes ? 'archive_state'))""")
    )
    if occupied:
        raise RuntimeError(
            "DOCUMENT lifecycle/correction/history would be lost; downgrade refused without deleting data"
        )
    op.execute("DROP TRIGGER document_correction_guard ON arm2_documents")
    op.drop_table("arm2_document_file_corrections")
    op.execute("DROP FUNCTION arm2_check_document_correction()")
    op.execute("DROP TRIGGER revision_context_guard ON arm2_document_revisions")
    op.execute("DROP FUNCTION arm2_check_revision_context()")
    op.drop_table("arm2_document_revision_contexts")
    op.drop_constraint("ck_arm2_revision_identity", "arm2_document_revisions", type_="check")
    op.create_check_constraint(
        "ck_arm2_revision_identity",
        "arm2_document_revisions",
        "id ~ '^DOCUMENT-REV-[0-9]{6,}$' AND number>0 AND source='arm2-api-manual'",
    )
    op.drop_constraint("ck_arm2_documents_archive", "arm2_documents", type_="check")
    op.create_check_constraint("ck_arm2_documents_archive", "arm2_documents", "archive_state='Неархивный'")
