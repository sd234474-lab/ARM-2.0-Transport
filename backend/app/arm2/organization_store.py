from app.arm2.counterparty_rules import party_status
from app.arm2.counterparty_store import PartyAdapter, party_update_values, party_values
from app.arm2.object_registry import OBJECT_REGISTRY
from app.arm2.object_save import SaveContext, create_object, update_object
from app.arm2.organization_models import Organization
from app.arm2.representative_models import OrganizationRepresentative
from app.models import User

ORGANIZATION_ADAPTER = PartyAdapter(Organization, "ORG", OrganizationRepresentative, "ORGREP")


def register_organization_adapter(registry=OBJECT_REGISTRY):
    registry.attach_adapter(
        "ORG", ORGANIZATION_ADAPTER, status_field="status", status_rule=party_status,
        reference_rules=(ORGANIZATION_ADAPTER.signer_reference,),
    )


def create_organization(db, name, *, principal: User, context: SaveContext, **values):
    return create_object(
        db, object_type="ORG", values=party_values(name, **values), principal=principal, context=context
    ).record


def update_organization(db, organization_id, expected_version, name, *, principal: User, context: SaveContext, **values):
    return update_object(
        db, object_type="ORG", object_id=organization_id, expected_version=expected_version,
        values=party_update_values(name, **values), principal=principal, context=context,
        partial=True,
    ).record
