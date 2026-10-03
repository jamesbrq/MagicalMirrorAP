import json
import pkgutil
import typing
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from BaseClasses import Entrance, EntranceType, Region
from entrance_rando import EntranceRandomizationError, disconnect_entrance_for_randomization, randomize_entrances

from .Locations import LocationData, MickeyLocation, all_locations

if typing.TYPE_CHECKING:
    from . import MickeyWorld


class MickeyEntrance(Entrance):
    def can_connect_to(self, other, dead_end, er_state):
        # Introduce these rooms through their main floor/door, before using
        # the Pole landings or Storage's painting-side entrance.
        main_doors = {'Broken Room': 'Broken Room -> Old Hall',
                      'Storage Room': 'Storage Room -> Dark Hallway'}
        for entrance, region in ((self, self.parent_region), (other, other.connected_region)):
            if (region.name in main_doors and region not in er_state.placed_regions
                    and entrance.name != main_doors[region.name]):
                return False
        return super().can_connect_to(other, dead_end, er_state)


class MickeyRegion(Region):
    entrance_type = MickeyEntrance


class EntranceData:
    """One gameplay connection, combining alternate state/retry routes.

    Internal room-state transitions are omitted. Rules retain any state-specific
    conditions; physical area/walk-point identities remain in the door registry.
    Shard cinematics, bonus replays and kids-mode-only routes are excluded.
    """

    name: str
    frm: str
    to: str

    def __init__(self, name: str, frm: str, to: str, doorway: dict | None = None):
        self.name = name
        self.frm = frm
        self.to = to
        self.doorway = doorway


class RegionData:
    name: str
    areas: Final[tuple[int, ...]]
    locations: Final[tuple[int, ...]]

    def __init__(self, name: str, areas: list[int], locations: list[int]):
        self.name = name
        self.areas = tuple(areas)
        self.locations = tuple(locations)


region_table: Final[Mapping[str, RegionData]] = MappingProxyType({
    name: RegionData(name, **data)
    for name, data in json.loads(pkgutil.get_data(__package__, "json/regions.json")).items()
})
all_entrances: Final[tuple[EntranceData, ...]] = tuple(
    EntranceData(**entrance)
    for entrance in json.loads(pkgutil.get_data(__package__, "json/entrances.json"))
)

locations_by_region: Final[Mapping[str, tuple[LocationData, ...]]] = MappingProxyType({
    name: tuple(loc for loc in all_locations if loc.region == name)
    for name in region_table
})


def create_regions(world: "MickeyWorld") -> None:
    """Create the Menu region plus one region per room, and attach its checks."""
    multiworld = world.multiworld
    player = world.player

    menu = MickeyRegion("Menu", player, multiworld)
    multiworld.regions.append(menu)

    created = {"Menu": menu}
    # Include transit rooms with no catalog locations, in entrance discovery order.
    names = dict.fromkeys([*region_table, *(name for entrance in all_entrances
                                          for name in (entrance.frm, entrance.to))])
    for name in names:
        region = MickeyRegion(name, player, multiworld)
        for loc in locations_by_region.get(name, ()):
            if loc.name in world.disabled_locations:
                continue
            region.locations.append(MickeyLocation(player, loc.name, loc.id, region))
        multiworld.regions.append(region)
        created[name] = region

    world.created_regions = created


def connect_regions(world: "MickeyWorld") -> None:
    """Connect the complete room graph and its starting region."""
    created = world.created_regions
    for entrance in all_entrances:
        created[entrance.frm].connect(created[entrance.to], entrance.name)
    created["Menu"].connect(created[world.start_region_name], "Start")


def connect_minigame_exits(world: "MickeyWorld") -> None:
    """A door's first-use minigame shares its destination and key requirement."""
    for data in all_entrances:
        sequence = data.doorway.get('minigame') if data.doorway else None
        if not sequence:
            continue
        door = world.multiworld.get_entrance(data.name, world.player)
        entry = world.multiworld.get_entrance(sequence['entrance'], world.player)
        exit = world.multiworld.get_entrance(sequence['exit'], world.player)
        world.entrance_requirements[entry.name] = world.entrance_requirements.get(data.name, True)
        entry.access_rule = door.access_rule
        if exit.connected_region:
            exit.connected_region.entrances.remove(exit)
        exit.connect(door.connected_region)
        exit.access_rule = lambda state: True


def shuffle_entrances(world: "MickeyWorld") -> None:
    """Shuffle audited doorways, keeping locked and unlocked endpoints separate."""
    mode = world.options.entrance_shuffle
    if not mode:
        return
    locked = {name for door in world.locked_doors for name in door['entrances']}
    candidates = {e.name: e for e in all_entrances if e.doorway}
    if candidates:
        from .Rules import assign_shuffled_locks, restrict_secondary_arrivals, _solvable
        requirements = dict(world.entrance_requirements)
        access_rules = {name: world.multiworld.get_entrance(name, world.player).access_rule
                        for name in candidates}
        for attempt in range(100):
            for name in candidates:
                entrance = world.multiworld.get_entrance(name, world.player)
                entrance.randomization_type = EntranceType.TWO_WAY
                entrance.randomization_group = int(name in locked)
                disconnect_entrance_for_randomization(entrance)
                sequence = candidates[name].doorway.get('minigame')
                if sequence:
                    # Until a pair is chosen, its old minigame destination
                    # must not grant the shuffler a second route to a room.
                    world.multiworld.get_entrance(sequence['exit'], world.player).access_rule = lambda state: False
            try:
                result = randomize_entrances(world, coupled=True, target_group_lookup={0: [0], 1: [1]})
                world.entrance_connections.update(result.pairings)
                assign_shuffled_locks(world)
                restrict_secondary_arrivals(world)
                connect_minigame_exits(world)
                targets = {name: candidates[target].frm for name, target in result.pairings}
                if not _solvable(world, world.location_requirements, world.entrance_requirements,
                                 world.locked_doors, targets):
                    raise EntranceRandomizationError('Shuffled doors cannot bootstrap progression')
                break
            except EntranceRandomizationError:
                world.entrance_connections.clear()
                world.door_locks.clear()
                world.entrance_requirements = dict(requirements)
                for region in world.created_regions.values():
                    region.entrances[:] = [e for e in region.entrances
                                          if e.parent_region or e.name not in candidates]
                for name, data in candidates.items():
                    entrance = world.multiworld.get_entrance(name, world.player)
                    entrance.access_rule = access_rules[name]
                    if entrance.connected_region:
                        entrance.connected_region.entrances.remove(entrance)
                    entrance.connect(world.created_regions[data.to])
                connect_minigame_exits(world)
                if attempt == 99:
                    raise
    connect_minigame_exits(world)
    for source, target in world.entrance_connections.items():
        arrival = candidates[target]
        world.multiworld.spoiler.set_entrance(
            source, f'{arrival.frm} (door to {arrival.to})', 'entrance', world.player)
        if sequence := candidates[source].doorway.get('minigame'):
            world.multiworld.spoiler.set_entrance(
                sequence['exit'], f'{arrival.frm} (door to {arrival.to})', 'entrance', world.player)
