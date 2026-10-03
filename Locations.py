import json
import pkgutil
from typing import Final

from BaseClasses import Location


class LocationData:
    name: str
    id: int
    region: str
    area: int
    type: str
    offsets: Final[tuple[str, ...]]
    vanilla_item: int | None
    star_point: int | None
    flag: int | None

    def __init__(self, name: str, id: int, region: str, area: int, type: str,
                 offsets: list[str], vanilla_item: int | None, star_point: int | None = None,
                 flag: int | None = None):
        self.name = name
        self.id = id
        self.region = region
        self.area = area
        self.type = type
        self.offsets = tuple(offsets)
        self.vanilla_item = vanilla_item
        self.star_point = star_point
        self.flag = flag


class MickeyLocation(Location):
    game = "Disney's Magical Mirror"


all_locations: Final[tuple[LocationData, ...]] = tuple(
    LocationData(**location) for location in json.loads(pkgutil.get_data(__package__, "json/locations.json"))
)

# These sources stay in the research catalog but are not registered AP checks.
# Alcove returns keep their original item_get/consumption scripts.
NON_RANDOMIZED_LOCATION_IDS: Final[frozenset[int]] = frozenset({
    79800048,  # Alcove marble return, sec6+11A4
    79800049,  # Alcove marble return, sec6+1230
    79800050,  # Alcove marble return, sec6+12BC
})


def non_randomized_locations() -> frozenset[str]:
    return frozenset(loc.name for loc in all_locations if loc.id in NON_RANDOMIZED_LOCATION_IDS)


def get_locations_by_type(kind: str) -> list[LocationData]:
    return [loc for loc in all_locations if loc.type == kind]

# Bank bit zero is the Entrance's existing native pickup; other IDs remain stable.
CHECK_FLAGS = {loc.id: (0 if loc.id == 79800116 else loc.id - 79800000 + 1)
               for loc in all_locations if loc.id not in NON_RANDOMIZED_LOCATION_IDS}
