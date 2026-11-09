"""Frozen product structure. Grids are measured from the local source TIFFs."""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

SOURCE_VERSION = "2.0.0"
DATASET_VERSION = "0.1.0"
SOURCE_URL = "https://www.futurewater.nl/solutions/hihydrosoil/"
DEPTH_DIM = "depth_interval"
DEPTH_BOUNDS = ((0, 5), (5, 15), (15, 30), (30, 60), (60, 100), (100, 200))
DEPTH_LABELS = tuple(f"{top}_{bottom}" for top, bottom in DEPTH_BOUNDS)
DEPTH_TOKENS = tuple(f"{top}-{bottom}cm" for top, bottom in DEPTH_BOUNDS)
OVERVIEW_FACTORS = (2, 4, 8, 16, 32, 64, 128, 256)
PIXEL_SIZE = 0.00227618823616542
CITATION = (
    "Simons, G.W.H., R. Koster, P. Droogers. 2020. HiHydroSoil v2.0 - "
    "A high resolution soil map of global hydraulic properties. FutureWater Report 213."
)


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class GridSpec(FrozenModel):
    crs: str = "EPSG:4326"
    width: int
    height: int
    x_min: float
    y_max: float
    pixel_size: float = PIXEL_SIZE

    @model_validator(mode="after")
    def _check(self):
        if self.width <= 0 or self.height <= 0 or self.pixel_size <= 0:
            raise ValueError("grid dimensions and pixel size must be positive")
        return self

    @property
    def shape(self):
        return self.height, self.width

    def shape_at(self, factor: int = 1):
        # Pad incomplete edge blocks with fill rather than discard source pixels.
        return (self.height + factor - 1) // factor, (self.width + factor - 1) // factor

    def transform(self, factor: int = 1):
        from affine import Affine

        px = self.pixel_size * factor
        return Affine(px, 0, self.x_min, 0, -px, self.y_max)

    def coords(self, factor: int = 1):
        h, w = self.shape_at(factor)
        px = self.pixel_size * factor
        return self.x_min + (np.arange(w) + 0.5) * px, self.y_max - (np.arange(h) + 0.5) * px


GRIDS = {
    "soil_properties": GridSpec(width=158159, height=60934, x_min=-179.99999985285478, y_max=82.71928405344526),
    "profile_properties": GridSpec(width=158159, height=79080, x_min=-180.0, y_max=90.0),
}


class EncodingSpec(FrozenModel):
    chunk_y: int = 64
    chunk_x: int = 64
    shard_y: int = 1024
    shard_x: int = 1024
    zstd_level: int = 3

    @model_validator(mode="after")
    def _check(self):
        if min(self.chunk_y, self.chunk_x, self.shard_y, self.shard_x) <= 0:
            raise ValueError("chunk and shard dimensions must be positive")
        if self.shard_y % self.chunk_y or self.shard_x % self.chunk_x:
            raise ValueError("shards must be divisible by chunks")
        return self

    def chunks(self, ndim: int):
        return (1, self.chunk_y, self.chunk_x) if ndim == 3 else (self.chunk_y, self.chunk_x)

    def shards(self, ndim: int, depths: int):
        return (depths, self.shard_y, self.shard_x) if ndim == 3 else (self.shard_y, self.shard_x)


ENCODING = EncodingSpec()


class PropertySpec(FrozenModel):
    name: str
    source_token: str
    long_name: str
    units: str
    group: Literal["soil_properties", "profile_properties"] = "soil_properties"
    categorical: bool = False
    source_dtype: str = "int32"
    source_nodata: int = -9999
    dtype: str = "float32"
    flags: tuple[int, ...] = ()
    meanings: tuple[str, ...] = ()
    class_codes: dict[int, str | dict[str, str]] = Field(default_factory=dict)

    @property
    def ndim(self):
        return 3 if self.group == "soil_properties" else 2

    @property
    def depths(self):
        return 6 if self.ndim == 3 else 1

    @property
    def dims(self):
        return (DEPTH_DIM, "y", "x") if self.ndim == 3 else ("y", "x")

    @property
    def fill(self):
        return self.source_nodata if self.categorical else float("nan")

    @property
    def path(self):
        return f"{self.group}/{self.name}"


_CONTINUOUS = (
    ("ormc", "ORMC", "Organic matter content", "%"),
    ("alpha", "ALFA", "Mualem van Genuchten alpha parameter", "cm-1"),
    ("n", "N", "Mualem van Genuchten n parameter", "1"),
    ("wcsat", "WCsat", "Saturated water content", "m3 m-3"),
    ("wcres", "WCres", "Residual water content", "m3 m-3"),
    ("ksat", "Ksat", "Saturated hydraulic conductivity", "cm d-1"),
    ("wcpf2", "WCpF2", "Water content at pF2 (field capacity)", "m3 m-3"),
    ("wcpf3", "WCpF3", "Water content at pF3 (critical point)", "m3 m-3"),
    ("wcpf42", "WCpF4.2", "Water content at pF4.2 (permanent wilting point)", "m3 m-3"),
    ("wcavail", "WCavail", "Available water content", "m3 m-3"),
    ("sat_field", "SAT-FIELD", "Water content between saturation and field capacity", "m3 m-3"),
    ("field_crit", "FIELD-CRIT", "Water content between field capacity and critical point", "m3 m-3"),
    ("crit_wilt", "CRIT-WILT", "Water content between critical and permanent wilting points", "m3 m-3"),
)
PROPERTIES = {
    name: PropertySpec(name=name, source_token=token, long_name=long_name, units=units)
    for name, token, long_name, units in _CONTINUOUS
}
PROPERTIES["stc"] = PropertySpec(
    name="stc",
    source_token="STC",
    long_name="Simplified FAO soil texture class",
    units="1",
    categorical=True,
    source_dtype="int16",
    dtype="int16",
    flags=(1, 2, 3, 4, 5, 6),
    meanings=("organic", "very_fine", "fine", "medium_fine", "coarse", "medium"),
    class_codes={1: "Organic", 2: "Very fine", 3: "Fine", 4: "Medium fine", 5: "Coarse", 6: "Medium"},
)
PROPERTIES["hydrologic_soil_group"] = PropertySpec(
    name="hydrologic_soil_group",
    source_token="Hydrologic_Soil_Group",
    long_name="Hydrologic soil group",
    units="1",
    group="profile_properties",
    categorical=True,
    source_dtype="uint8",
    source_nodata=255,
    dtype="uint8",
    flags=(1, 2, 3, 4, 14, 24, 34),
    meanings=("A", "B", "C", "D", "A_D", "B_D", "C_D"),
    class_codes={
        1: {"group": "A", "meaning": "Low runoff potential"},
        2: {"group": "B", "meaning": "Moderately low runoff potential"},
        3: {"group": "C", "meaning": "Moderately high runoff potential"},
        4: {"group": "D", "meaning": "High runoff potential"},
        14: {"group": "A/D", "meaning": "A when drained; D when undrained"},
        24: {"group": "B/D", "meaning": "B when drained; D when undrained"},
        34: {"group": "C/D", "meaning": "C when drained; D when undrained"},
    },
)


def select_properties(value: str | None = None):
    names = [s.strip() for s in value.split(",")] if value else list(PROPERTIES)
    if not names or any(n not in PROPERTIES for n in names) or len(set(names)) != len(names):
        raise ValueError(f"expected unique property names from: {', '.join(PROPERTIES)}")
    return [PROPERTIES[n] for n in names]
