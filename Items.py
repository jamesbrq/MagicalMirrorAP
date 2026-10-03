import json
import pkgutil
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from BaseClasses import Item, ItemClassification


class ItemData:
    id: int
    item_name: str
    progression: ItemClassification
    frequency: int
    grant: str
    rom_id: int
    door: str
    lock_index: int | None

    def __init__(self, id: int, item_name: str, progression: int, frequency: int,
                 grant: str, rom_id: str, door: str = "",
                 lock_index: int | None = None):
        self.id = id
        self.item_name = item_name
        self.progression = ItemClassification(progression)
        self.frequency = frequency
        self.grant = grant
        self.rom_id = int(rom_id, 16)
        self.door = door
        self.lock_index = lock_index


class MickeyItem(Item):
    game = "Disney's Magical Mirror"


item_list: Final[tuple[ItemData, ...]] = tuple(
    ItemData(**item) for item in json.loads(pkgutil.get_data(__package__, "json/items.json"))
)
# All keys have stable identities even when their doors are open in this seed.
door_key_list: Final[tuple[ItemData, ...]] = tuple(item for item in item_list if item.door)

door_keys: Final[Mapping[str, str]] = MappingProxyType(
    {item.door: item.item_name for item in door_key_list})
item_table: Final[Mapping[str, ItemData]] = MappingProxyType({item.item_name: item for item in item_list})
items_by_id: Final[Mapping[int, ItemData]] = MappingProxyType({item.id: item for item in item_list})


tricks: Final[tuple[str, ...]] = tuple(item.item_name for item in item_list if item.grant == "ap_flag")
quest_items: Final[tuple[str, ...]] = tuple(
    item.item_name for item in item_list if item.grant == "inventory" and item.frequency > 0
)

# Every item definition is receivable; souvenir flags belong to locations.
mickey_item_name_groups: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType({
    "Trick": tricks,
    "Quest Item": quest_items,
    "Key": tuple(item.item_name for item in door_key_list),
})

# Native mailbox command IDs for the supported item grants.
TRICK_ITEMS = {item.id: item.rom_id for item in item_list if item.grant == 'ap_flag'}
TRICK_ITEM_IDS = {index: item for item, index in TRICK_ITEMS.items()}
DOOR_ITEMS = {item.id: item.lock_index for item in door_key_list if item.lock_index is not None}
DOOR_ITEM_IDS = {index: item for item, index in DOOR_ITEMS.items()}
ITEM_KINDS = {79790001: 2, 79790002: 3, 79790076: 0}
ITEM_KINDS.update({item: 0x100 + index for item, index in TRICK_ITEMS.items()})
ITEM_KINDS.update({item: 0x200 + index for item, index in DOOR_ITEMS.items()})

ITEM_KINDS.update({item.id: 0x300 + item.rom_id for item in item_list
                   if item.grant == "inventory" and item.frequency > 0})
