from sqlalchemy import select

from app.arm2.counterparty_models import Counterparty
from app.arm2.counterparty_rules import optional_email, optional_text, required_name
from app.arm2.ids import next_id
from app.arm2.object_registry import OBJECT_REGISTRY, ObjectReference
from app.arm2.object_save import create_object, update_object
from app.arm2.organization_models import Organization
from app.arm2.representative_models import CounterpartyRepresentative, OrganizationRepresentative
from app.models import User


class RepresentativeAdapter:
    def __init__(self, model, prefix, parent_model, parent_type):
        self.model, self.prefix = model, prefix
        self.parent_model, self.parent_type = parent_model, parent_type

    def normalize(self, values):
        state = values["state"]
        if state not in {"Действующий", "Архивный"}:
            raise ValueError("state must be Действующий or Архивный")
        email = optional_email(values["email"])
        return {
            "parent_id": values["parent_id"], "name": required_name(values["name"]),
            "position": optional_text(values["position"], "position", 500),
            "authority_basis": optional_text(values["authority_basis"], "authority_basis", 1000),
            "phone": optional_text(values["phone"], "phone", 200), "email": email,
            "state": state, "user_id": values["user_id"],
            "last_name": optional_text(values["last_name"], "last_name", 200),
            "first_name": optional_text(values["first_name"], "first_name", 200),
            "middle_name": optional_text(values["middle_name"], "middle_name", 200),
        }

    def create_record(self, db, values):
        record = self.model(id=next_id(db, self.prefix), version=1, **values)
        db.add(record)
        db.flush()
        return record

    def lock_record(self, db, object_id):
        return db.scalar(select(self.model).where(self.model.id == object_id).with_for_update())

    def apply(self, record, values):
        if record.parent_id != values["parent_id"]:
            raise ValueError("Representative parent cannot be changed")
        for field, value in values.items():
            setattr(record, field, value)

    def references(self, db, current, values):
        parent = db.get(self.parent_model, values["parent_id"])
        if parent is None:
            raise ValueError("Representative parent does not exist")
        user_id = values["user_id"]
        if user_id is not None and db.get(User, user_id) is None:
            raise ValueError("Representative user_id does not exist")
        return (ObjectReference(self.parent_type, parent.id, "parent"),)


CPREP_ADAPTER = RepresentativeAdapter(CounterpartyRepresentative, "CPREP", Counterparty, "CP")
ORGREP_ADAPTER = RepresentativeAdapter(OrganizationRepresentative, "ORGREP", Organization, "ORG")


def register_representative_adapters(registry=OBJECT_REGISTRY):
    registry.attach_adapter("CPREP", CPREP_ADAPTER, reference_rules=(CPREP_ADAPTER.references,))
    registry.attach_adapter("ORGREP", ORGREP_ADAPTER, reference_rules=(ORGREP_ADAPTER.references,))


def representative_values(parent_id, name, **values):
    return {"parent_id": parent_id, "name": name, "position": values.get("position"),
            "authority_basis": values.get("authority_basis"), "phone": values.get("phone"),
            "email": values.get("email"), "state": values.get("state", "Действующий"),
            "user_id": values.get("user_id"), "last_name": values.get("last_name"),
            "first_name": values.get("first_name"), "middle_name": values.get("middle_name")}


def create_representative(db, kind, parent_id, name, *, principal, context, **values):
    object_type = "CPREP" if kind == "CP" else "ORGREP" if kind == "ORG" else None
    if object_type is None:
        raise ValueError("kind must be CP or ORG")
    return create_object(db, object_type=object_type, values=representative_values(parent_id, name, **values), principal=principal, context=context).record


def update_representative(db, kind, representative_id, expected_version, parent_id, name, *, principal, context, **values):
    object_type = "CPREP" if kind == "CP" else "ORGREP" if kind == "ORG" else None
    if object_type is None:
        raise ValueError("kind must be CP or ORG")
    return update_object(db, object_type=object_type, object_id=representative_id,
                         expected_version=expected_version, values=representative_values(parent_id, name, **values),
                         principal=principal, context=context).record
