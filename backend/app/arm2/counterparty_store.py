from sqlalchemy import select

from app.arm2.counterparty_models import Counterparty
from app.arm2.counterparty_rules import normalize_party, party_status
from app.arm2.ids import next_id
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectReference
from app.arm2.object_save import SaveContext, create_object, update_object
from app.arm2.representative_models import CounterpartyRepresentative
from app.models import User

PARTY_FIELDS = (
    "name", "full_name", "legal_address", "postal_address", "legal_form", "short_name",
    "inn", "kpp", "ogrn", "phone", "email", "bank_name", "bik",
    "settlement_account", "correspondent_account", "primary_signer_id",
)


def party_values(name, **values):
    return {field: name if field == "name" else values.get(field) for field in PARTY_FIELDS}


def party_update_values(name, **values):
    return {"name": name, **values}


class PartyAdapter:
    def __init__(self, model, prefix, representative_model, representative_type):
        self.model = model
        self.prefix = prefix
        self.representative_model = representative_model
        self.representative_type = representative_type

    def normalize(self, values):
        return normalize_party(values)

    def create_record(self, db, values):
        record = self.model(id=next_id(db, self.prefix), version=1, **values)
        db.add(record)
        db.flush()
        return record

    def lock_record(self, db, object_id):
        return db.scalar(select(self.model).where(self.model.id == object_id).with_for_update())

    def apply(self, record, values):
        for field, value in values.items():
            setattr(record, field, value)

    def signer_reference(self, db, current, values):
        signer_id = values["primary_signer_id"]
        if signer_id is None:
            return ()
        if current is None:
            raise ValueError("primary_signer_id can be assigned only after card creation")
        signer = db.get(self.representative_model, signer_id)
        if signer is None or signer.parent_id != current.id:
            raise ValueError("primary_signer_id must belong to the same card")
        return (ObjectReference(self.representative_type, signer_id, "primary_signer"),)


COUNTERPARTY_ADAPTER = PartyAdapter(Counterparty, "CP", CounterpartyRepresentative, "CPREP")


def register_counterparty_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter(
        "CP", COUNTERPARTY_ADAPTER, status_field="status", status_rule=party_status,
        reference_rules=(COUNTERPARTY_ADAPTER.signer_reference,),
    )


def create_counterparty(db, name, *, principal: User, context: SaveContext, **values):
    return create_object(
        db, object_type="CP", values=party_values(name, **values), principal=principal, context=context
    ).record


def update_counterparty(db, counterparty_id, expected_version, name, *, principal: User, context: SaveContext, **values):
    return update_object(
        db, object_type="CP", object_id=counterparty_id, expected_version=expected_version,
        values=party_update_values(name, **values), principal=principal, context=context,
        partial=True,
    ).record
