from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, StrictInt

from app.schemas import StrictModel

PositiveVersion = Annotated[int, Field(strict=True, gt=0)]
HistoryObjectType = Literal["TYPE", "VEH", "CP", "ORG", "CPREP", "ORGREP", "DOC-KIND", "DOCUMENT"]


class DocumentCreate(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    kind_ref: str
    name: str
    description: str | None = None


class DocumentUpdate(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    expected_version: PositiveVersion
    name: str
    description: str | None


class DocumentResponse(StrictModel):
    model_config = ConfigDict(str_strip_whitespace=False)
    id: str
    parent_type: Literal["CP", "ORG", "VEH"]
    parent_id: str
    kind_ref: str
    kind_name: str
    name: str
    description: str | None
    archive_state: Literal["Неархивный", "Архивный"]
    display_state: Literal["Не загружен", "Доступен", "Файл недоступен", "Архивный"]
    version: int


class DocumentOperation(StrictModel):
    expected_version: PositiveVersion
    kind_ref: str | None = None
    target_type: Literal["CP", "ORG", "VEH"] | None = None
    target_id: str | None = None


class DocumentKindCreate(StrictModel):
    # Preserve parent strings for the service's exact connected-type validation;
    # the same service already owns name trimming.
    model_config = ConfigDict(str_strip_whitespace=False)
    name: str
    applicable_object_types: list[str]
    max_current_documents: StrictInt | None
    file_naming_template: Literal["DOCUMENT_NAME", "PARENT_AND_KIND"] | None = None


class DocumentKindUpdate(DocumentKindCreate):
    expected_version: PositiveVersion


class DocumentKindResponse(StrictModel):
    file_naming_template: Literal["DOCUMENT_NAME", "PARENT_AND_KIND"] | None
    id: str
    name: str
    applicable_object_types: list[str]
    max_current_documents: int | None
    version: int


class VehicleTypeCreate(StrictModel):
    name: str
    requires_sts: bool | None = True
    requires_calibration: bool | None = None
    requires_wialon: bool | None = None


class VehicleTypeUpdate(StrictModel):
    expected_version: PositiveVersion
    name: str
    requires_sts: bool | None
    requires_calibration: bool | None
    requires_wialon: bool | None


class VehicleTypeResponse(StrictModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    requires_sts: bool | None
    requires_calibration: bool | None
    requires_wialon: bool | None
    status: str
    version: int


class VehicleCreate(StrictModel):
    name: str
    plate_number: str
    type_id: str | None = None
    accounting_state: Literal["Действующая", "Архивная"] = "Действующая"
    model: str | None = None
    wialon_unit_id: str | None = None
    owner_cp_id: str | None = None
    owner_org_id: str | None = None


class VehicleUpdate(StrictModel):
    expected_version: PositiveVersion
    name: str
    plate_number: str
    type_id: str | None
    accounting_state: Literal["Действующая", "Архивная"]
    model: str | None
    wialon_unit_id: str | None
    owner_cp_id: str | None = None
    owner_org_id: str | None = None


class VehicleResponse(StrictModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    plate_number: str
    type_id: str | None
    accounting_state: str
    model: str | None
    wialon_unit_id: str | None
    owner_cp_id: str | None
    owner_org_id: str | None
    version: int


class PartyCreate(StrictModel):
    name: str
    full_name: str | None = None
    legal_address: str | None = None
    postal_address: str | None = None
    legal_form: str | None = None
    short_name: str | None = None
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    phone: str | None = None
    email: str | None = None
    bank_name: str | None = None
    bik: str | None = None
    settlement_account: str | None = None
    correspondent_account: str | None = None


class PartyUpdate(StrictModel):
    expected_version: PositiveVersion
    name: str
    full_name: str | None = None
    legal_address: str | None = None
    postal_address: str | None = None
    legal_form: str | None = None
    short_name: str | None = None
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    phone: str | None = None
    email: str | None = None
    bank_name: str | None = None
    bik: str | None = None
    settlement_account: str | None = None
    correspondent_account: str | None = None
    primary_signer_id: str | None = None


class PartyResponse(StrictModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    full_name: str | None
    legal_address: str | None
    postal_address: str | None
    legal_form: str | None
    short_name: str | None
    inn: str | None
    kpp: str | None
    ogrn: str | None
    phone: str | None
    email: str | None
    bank_name: str | None
    bik: str | None
    settlement_account: str | None
    correspondent_account: str | None
    primary_signer_id: str | None
    status: str
    version: int


class RepresentativeCreate(StrictModel):
    name: str
    position: str | None = None
    authority_basis: str | None = None
    phone: str | None = None
    email: str | None = None
    state: Literal["Действующий", "Архивный"] = "Действующий"
    user_id: int | None = None
    last_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None


class RepresentativeUpdate(StrictModel):
    expected_version: PositiveVersion
    name: str
    position: str | None
    authority_basis: str | None
    phone: str | None
    email: str | None
    state: Literal["Действующий", "Архивный"]
    user_id: int | None
    last_name: str | None
    first_name: str | None
    middle_name: str | None


class RepresentativeResponse(StrictModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    parent_id: str
    name: str
    position: str | None
    authority_basis: str | None
    phone: str | None
    email: str | None
    state: str
    user_id: int | None
    last_name: str | None
    first_name: str | None
    middle_name: str | None
    version: int


class HistoryEventSummary(StrictModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int
    object_type: HistoryObjectType
    object_id: str
    operation: str
    occurred_at: datetime
    actor_kind: str
    actor_id: str
    source: str
    version_before: int | None
    version_after: int | None


class HistoryEventDetail(HistoryEventSummary):
    correlation_id: str | None
    changes: dict[str, Any]
    related_objects: list[dict[str, Any]]
