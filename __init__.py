from pathlib import Path
from typing import Any, ClassVar

import settings

from BaseClasses import Region, Tutorial
from Options import OptionError
from worlds.AutoWorld import WebWorld, World
from worlds.LauncherComponents import Component, SuffixIdentifier, Type, components, launch

from .Items import (MickeyItem, door_keys, item_list, item_table,
                    mickey_item_name_groups, tricks)
from .Locations import (CHECK_FLAGS, NON_RANDOMIZED_LOCATION_IDS, all_locations,
                        get_locations_by_type, non_randomized_locations)
from .Options import MickeyOptions
from .Regions import connect_regions, create_regions, shuffle_entrances
from .Rules import build_requirements, impassable_locations, load_rules, order_fill, set_rules
from .Rom import MickeyProcedurePatch, identity, write_files


def launch_client(*args: str) -> None:
    from .MickeyClient import launch as launch_mickey_client
    launch(launch_mickey_client, name="MickeyClient", args=args)


components.append(
    Component(
        "Mickey Client",
        func=launch_client,
        component_type=Type.CLIENT,
        file_identifier=SuffixIdentifier(".apmickey"),
        game_name="Disney's Magical Mirror",
        description="Open the Disney's Magical Mirror client.",
    ),
)


class MickeyWebWorld(WebWorld):
    theme = "partyTime"
    tutorials = [
        Tutorial(
            tutorial_name="Setup Guide",
            description="A guide to setting up Disney's Magical Mirror for Archipelago.",
            language="English",
            file_name="setup_en.md",
            link="setup/en",
            authors=["jamesbrq"],
        ),
    ]


class MickeySettings(settings.Group):
    class DolphinPath(settings.UserFilePath):
        """Dolphin executable used to open the patched game."""
        is_exe = True
        description = "Dolphin Executable"

    class RomFile(settings.UserFilePath):
        """File name of the clean GDME01 US revision 0 ISO."""
        copy_to = "Disneys Magical Mirror Starring Mickey Mouse.iso"
        description = "US Magical Mirror .iso File"

    dolphin_path: DolphinPath = DolphinPath(None)
    rom_file: RomFile = RomFile(RomFile.copy_to)
    rom_start: bool = True


class MickeyWorld(World):
    """
    Explore the mansion, perform tricks, and collect Mirror Shards to help Mickey
    return home through the mirror.
    """

    game = "Disney's Magical Mirror"
    web = MickeyWebWorld()
    options_dataclass = MickeyOptions
    options: MickeyOptions
    settings: ClassVar[MickeySettings]

    item_name_to_id = {name: data.id for name, data in item_table.items()}
    location_name_to_id = {loc.name: loc.id for loc in all_locations
                           if loc.id not in NON_RANDOMIZED_LOCATION_IDS}
    item_name_groups = mickey_item_name_groups
    location_name_groups = {
        group: {loc.name for loc in get_locations_by_type(kind)
                if loc.id not in NON_RANDOMIZED_LOCATION_IDS}
        for group, kind in (
            ('Trick', 'trick'), ('Souvenir', 'souvenir'), ('Quest Item', 'quest_item'),
            ('Star Container', 'vessel'), ('Mirror Shard', 'shard'), ('Small Key', 'key'),
            ('Star', 'star'), ('Hidden Hats', 'hidden_hat'),
        )
    }

    start_region_name: ClassVar[str] = load_rules()['start_region']
    disabled_locations: set[str]
    created_regions: dict[str, Region]
    # Set by Rules.build_requirements, at the end of create_regions.
    trick_costs: dict[str, int]
    locked_doors: list[dict[str, Any]]
    location_requirements: dict[str, Any]
    entrance_requirements: dict[str, Any]
    entrance_connections: dict[str, str]
    door_locks: dict[str, int]

    def generate_early(self) -> None:
        self.disabled_locations = set()
        self.entrance_connections = {}
        self.door_locks = {}
        for _ in range(self.options.starting_star_containers.value):
            self.multiworld.push_precollected(self.create_item("Star Container"))

    def create_regions(self) -> None:
        if not self.options.trick_checks:
            self.disabled_locations.update(loc.name for loc in get_locations_by_type('trick'))
        if not self.options.hidden_hats:
            self.disabled_locations.update(loc.name for loc in get_locations_by_type('hidden_hat'))
        self.disabled_locations |= impassable_locations()
        self.disabled_locations |= non_randomized_locations()

        create_regions(self)
        connect_regions(self)

        # Select locks before create_items adds their matching keys to the pool.
        self.location_requirements, self.entrance_requirements = \
            build_requirements(self)

    def create_items(self) -> None:
        skipped = set(door_keys.values())
        if not self.options.tricks:
            skipped.update(tricks)

        pool = [door_keys[door["id"]] for door in self.locked_doors]
        for item in item_list:
            if item.item_name in skipped:
                continue
            copies = item.frequency
            if item.item_name == "Star Container":
                copies -= self.options.starting_star_containers.value
            pool += [item.item_name] * copies

        checks = len(self.multiworld.get_unfilled_locations(self.player))
        if len(pool) > checks:
            raise OptionError(
                f"{self.game} ({self.player_name}): {len(pool)} items for {checks} locations. "
                "Enable more checks, disable Tricks, or lower Locked Door Count.")

        self.multiworld.itempool += [self.create_item(name) for name in pool]
        self.multiworld.itempool += [self.create_item(self.get_filler_item_name()) for _ in range(checks - len(pool))]

    def set_rules(self) -> None:
        set_rules(self)

    def pre_fill(self) -> None:
        shuffle_entrances(self)

    def fill_hook(self, progitempool, usefulitempool, filleritempool, fill_locations) -> None:
        order_fill(self, progitempool, fill_locations)

    def create_item(self, name: str) -> MickeyItem:
        item = item_table[name]
        return MickeyItem(item.item_name, item.progression, item.id, self.player)

    def get_filler_item_name(self) -> str:
        return "Star Refill"

    def fill_slot_data(self) -> dict[str, Any]:
        return {
            "protocol": 3,
            "token": identity(self.multiworld.seed_name, self.player, self.player_name),
            "checks": {loc.address: CHECK_FLAGS[loc.address]
                       for loc in self.get_locations() if loc.address in CHECK_FLAGS},
            "flag_checks": {loc.id: loc.flag
                            for loc in all_locations if loc.flag is not None
                            and loc.name not in self.disabled_locations},
            "tricks": self.options.tricks.value,
            "trick_checks": self.options.trick_checks.value,
            "hidden_hats": self.options.hidden_hats.value,
            "shards_required": self.options.shards_required.value,
            "locked_door_mode": self.options.locked_doors.value,
            "locked_door_count": self.options.locked_door_count.value,
            "starting_star_containers": self.options.starting_star_containers.value,
            "entrance_shuffle": self.options.entrance_shuffle.value,
            "entrance_connections": self.entrance_connections,
            "door_locks": self.door_locks,
            # These same assignments drive logic and static ISO operands.
            "trick_costs": self.trick_costs,
            "locked_doors": [door["id"] for door in self.locked_doors],
        }

    def write_spoiler(self, spoiler_handle) -> None:
        if self.door_locks:
            spoiler_handle.write("\nShuffled door keys:\n")
            for door in self.locked_doors:
                sides = sorted(name for name, index in self.door_locks.items() if index == door['lock_index'])
                if sides:
                    spoiler_handle.write(f"{door['key_item']}: {sides[0]} <=> {sides[1]}\n")

    def generate_output(self, output_directory: str) -> None:
        patch = MickeyProcedurePatch(player=self.player, player_name=self.player_name)
        write_files(self, patch)
        path = Path(output_directory) / (
            f"{self.multiworld.get_out_file_name_base(self.player)}{patch.patch_file_ending}"
        )
        patch.write(str(path))

