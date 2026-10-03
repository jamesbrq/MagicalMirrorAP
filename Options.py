from dataclasses import dataclass

from Options import Choice, DefaultOnToggle, PerGameCommonOptions, Range, StartInventoryPool, Toggle


class Tricks(DefaultOnToggle):
    """
    Tricks will be added to the pool as items, and each trick's interaction is
    locked until you receive it.
    """
    display_name = "Tricks"


class TrickChecks(DefaultOnToggle):
    """
    Performing a trick for the first time is a check.
    """
    display_name = "Trick Checks"


class HiddenHats(Toggle):
    """
    Add the 30 hidden hat boxes as checks, three in each of ten rooms.
    """
    display_name = "Hidden Hats"


class TrickCostShuffle(Choice):
    """
    Randomize what each trick costs in stars.
    Shuffle: Shuffle trick costs between eachother.
    Randomized: Randomize trick costs between 1 and 6.
    """
    display_name = "Trick Cost Shuffle"
    option_off = 0
    option_shuffle = 1
    option_randomized = 2
    default = 0


class LockedDoorCount(Range):
    """
    Number of locked doors, each with its own matching key in the pool.
    With Vanilla locked doors, fewer than 7 selects a random subset of the
    seven eligible vanilla locks; more than 7 adds random extra doors.
    With Randomized locked doors, the whole set is chosen randomly.
    """
    display_name = "Locked Door Count"
    range_start = 0
    range_end = 28
    default = 8


class LockedDoors(Choice):
    """
    Choose where locks appear. Every locked door uses its own matching key.
    Vanilla: use the seven eligible vanilla locks; Locked Door Count can remove
    some or add random extra doors while retaining all seven.
    Randomized: choose from all 28 eligible two-way doors.
    """
    display_name = "Locked Doors"
    option_vanilla = 0
    option_randomized = 1
    default = 0


class ShardsRequired(Range):
    """
    How many Mirror Shards are needed to finish.
    The ending and its Mirror Room route require at least this many shards.
    """
    display_name = "Shards Required"
    range_start = 1
    range_end = 12
    default = 12


class StartingStarContainers(Range):
    """
    How many Star Containers to start with.
    """
    display_name = "Starting Star Containers"
    range_start = 0
    range_end = 12
    default = 0


class EntranceShuffle(Choice):
    """
    Shuffle where doors lead.
    Disabled: Doors lead to their original rooms.
    Shuffled: Randomize which rooms doors connect.
    """
    display_name = "Entrance Shuffle"
    option_disabled = 0
    option_shuffled = 1
    default = 0


class Costume(Choice):
    """Mickey's appearance. Cosmetic only."""
    display_name = "Costume"
    option_original = 0
    option_black_hooded_cloak = 1
    option_steamboat_willie = 2
    option_sorcerer = 3
    default = 0


@dataclass
class MickeyOptions(PerGameCommonOptions):
    costume: Costume
    start_inventory_from_pool: StartInventoryPool
    tricks: Tricks
    trick_checks: TrickChecks
    hidden_hats: HiddenHats
    trick_cost_shuffle: TrickCostShuffle
    locked_door_count: LockedDoorCount
    locked_doors: LockedDoors
    shards_required: ShardsRequired
    starting_star_containers: StartingStarContainers
    entrance_shuffle: EntranceShuffle
