from sqlalchemy import text
from sqlalchemy.orm import Session

_NEXT_VALUE = {
    "APPROVAL": text("SELECT nextval('arm2_approval_id_seq')"),
    "APPROVAL-ITEM": text("SELECT nextval('arm2_approval_item_id_seq')"),
    "DOCUMENT-REV": text("SELECT nextval('arm2_document_revision_id_seq')"),
    "FILE": text("SELECT nextval('arm2_document_file_id_seq')"),
    "DOCUMENT": text("SELECT nextval('arm2_document_id_seq')"),
    "DOC-KIND": text("SELECT nextval('arm2_document_kind_id_seq')"),
    "TYPE": text("SELECT nextval('arm2_vehicle_type_id_seq')"),
    "VEH": text("SELECT nextval('arm2_vehicle_id_seq')"),
    "CP": text("SELECT nextval('arm2_cp_id_seq')"),
    "ORG": text("SELECT nextval('arm2_org_id_seq')"),
    "CPREP": text("SELECT nextval('arm2_cprep_id_seq')"),
    "ORGREP": text("SELECT nextval('arm2_orgrep_id_seq')"),
}


def next_id(db: Session, prefix: str) -> str:
    try:
        statement = _NEXT_VALUE[prefix]
    except KeyError as error:
        raise ValueError(f"Unknown ARM 2.0 ID prefix: {prefix}") from error

    number = db.execute(statement).scalar_one()
    return f"{prefix}-{number:06d}"
