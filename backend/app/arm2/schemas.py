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
