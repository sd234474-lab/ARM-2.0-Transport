from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from app.schemas import StrictModel

PositiveVersion = Annotated[int, Field(strict=True, gt=0)]


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
