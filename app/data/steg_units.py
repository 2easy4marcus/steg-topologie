"""Load STEG service units (districts/agencies) from the .xls or a CSV.

The real artifact (docs/data/tnlistedistrictsteg.xls) is a legacy OLE2 .xls
that only ``xlrd`` can read (``openpyxl`` cannot open this format). Its
columns are ``Region, Gouvernorat, Type, District, Adresse, Standard,
Reclamation, Fax, Lat, Lon`` -- tests use a CSV fixture with the same header
names so the suite never has to ship a second binary file.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

import xlrd
from pydantic import BaseModel

from .geography import LoadWarning

TUNISIA_LAT_RANGE = (30.0, 38.0)
TUNISIA_LON_RANGE = (7.0, 12.0)


class ServiceUnit(BaseModel):
    name: str
    unit_type: str
    region: str
    governorate: str
    latitude: float | None = None
    longitude: float | None = None
    coordinate_complete: bool


@dataclass
class LoadResult:
    accepted: list[ServiceUnit]
    quarantined: list[LoadWarning]


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _rows(path: Path):
    if path.suffix.lower() == ".xls":
        sheet = xlrd.open_workbook(path).sheet_by_index(0)
        headers = [str(value).strip() for value in sheet.row_values(0)]
        for index in range(1, sheet.nrows):
            yield dict(zip(headers, sheet.row_values(index)))
        return
    with path.open(newline="", encoding="utf-8-sig") as handle:
        yield from csv.DictReader(handle)


def load_service_units(path: Path) -> LoadResult:
    accepted: list[ServiceUnit] = []
    quarantined: list[LoadWarning] = []
    for row in _rows(path):
        name = str(row["District"]).strip()
        lat, lon = _number(row.get("Lat")), _number(row.get("Lon"))
        out_of_bounds = (
            lat is not None and not TUNISIA_LAT_RANGE[0] <= lat <= TUNISIA_LAT_RANGE[1]
        ) or (
            lon is not None and not TUNISIA_LON_RANGE[0] <= lon <= TUNISIA_LON_RANGE[1]
        )
        if out_of_bounds:
            quarantined.append(
                LoadWarning(record_key=name, reason_code="coordinate_out_of_bounds")
            )
            continue
        complete = lat is not None and lon is not None
        accepted.append(
            ServiceUnit(
                name=name,
                unit_type=str(row["Type"]).strip(),
                region=str(row["Region"]).strip(),
                governorate=str(row["Gouvernorat"]).strip(),
                latitude=lat if complete else None,
                longitude=lon if complete else None,
                coordinate_complete=complete,
            )
        )
    return LoadResult(accepted=accepted, quarantined=quarantined)
