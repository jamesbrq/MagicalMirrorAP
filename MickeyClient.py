"""Archipelago Dolphin client for Disney's Magical Mirror."""
import asyncio
import logging
import os
import subprocess
import sys
import Patch
import Utils
from .lib import setup_dme_path

setup_dme_path()
import dolphin_memory_engine_mickey as dolphin
from settings import get_settings

from CommonClient import CommonContext, server_loop
from NetUtils import ClientStatus
from .Items import ITEM_KINDS, TRICK_ITEM_IDS, DOOR_ITEM_IDS
from Utils import gui_enabled

logger = logging.getLogger('Client')


NET = 0x80004D00
BANK = 0x80003DD8


def read_bytes(address, size):
    return bytes(dolphin.read_bytes(address, size))


def read_u32(address):
    return dolphin.read_word(address)


def write_u32(address, value):
    dolphin.write_word(address, value)


def read_game_state(ctx):
    if read_bytes(0x80000000, 6) != b'GDME01':
        return None
    if read_u32(0x80003D14) != 5:
        return None
    header = read_bytes(NET, 80)
    abi = int.from_bytes(header[12:16], 'big')
    if header[:4] != b'MKN1' or abi != 3:
        return None
    if header[16:32] != ctx.token:
        return None
    status = int.from_bytes(header[32:36], 'big')
    bank = read_bytes(BANK, 512)
    if bank[:8] != b'MKS1\0\0\0\2' or bank[8:24] != ctx.token:
        return None
    goal = bool(bank[95] & 0x80)  # durable marker at the final mirror escape
    if status != 1 and not (status == 0 and goal):
        return None
    checked = {loc for loc, bit in ctx.checks.items() if bank[64 + bit // 8] & (1 << (bit % 8))}
    flags = read_bytes(0x801599CC + 0x24, 64)
    checked.update(loc for loc, flag in ctx.flag_checks.items()
                   if flags[flag // 8] & (1 << (flag % 8)))
    # Both Library key scenes persist flag 0x8A and remove the same pickup.
    # Read it directly so a completed scene remains reportable after reconnect.
    if 79800161 in ctx.checks and flags[0x8A // 8] & (1 << (0x8A % 8)):
        checked.add(79800161)
    # Recheck after the save reads to reject a concurrent load/transition.
    if read_bytes(NET + 16, 20) != ctx.token + status.to_bytes(4, 'big'):
        return None
    return {'received': int.from_bytes(bank[24:28], 'big'), 'checked': checked,
            'goal': goal, 'ready': status == 1,
            'door_keys_enabled': read_u32(NET + 88) == 1,
            'door_keys': {i for i in DOOR_ITEM_IDS if bank[40 + i // 8] & (1 << (i % 8))},
            'open_doors': {i for i in range(33) if bank[48 + i // 8] & (1 << (i % 8))},
            'tricks': {i for i in TRICK_ITEM_IDS if bank[32 + i // 8] & (1 << (i % 8))},
            'pending': int.from_bytes(header[40:44], 'big'),
            'error': int.from_bytes(header[52:56], 'big')}


def receive_items(ctx):
    state = read_game_state(ctx)
    if state is None or not state['ready']:
        return state
    index = state['received']
    if index > len(ctx.items_received):
        return None
    if state['pending']:
        # A save rollback can leave an unsaved command in the transient
        # mailbox. Cancel it, then rebuild from the SAVED index next poll.
        if state['pending'] != index + 1 or state['error'] in (1, 2):
            write_u32(NET + 40, 0)
        return state
    if index == len(ctx.items_received):
        return state
    item_id = ctx.items_received[index].item
    if item_id not in ITEM_KINDS:
        return None
    dolphin.write_bytes(NET + 64, ctx.token)
    write_u32(NET + 44, ITEM_KINDS[item_id])
    current = read_game_state(ctx)
    if current is None or not current['ready'] or current['received'] != index:
        return state
    write_u32(NET + 40, index + 1)  # publish last; runtime acknowledges after grant
    return state


class MickeyContext(CommonContext):
    game = "Disney's Magical Mirror"
    items_handling = 0b111  # include own-world and starting items; native grants suppressed
    want_slot_data = True

    def __init__(self, server_address=None, password=None):
        super().__init__(server_address, password)
        self.token = None
        self.checks = {}
        self.flag_checks = {}
        self.slot_validated = False

    async def server_auth(self, password_requested=False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def reset_server_state(self):
        super().reset_server_state()
        self.slot_validated = False
        self.token = None
        self.checks = {}
        self.flag_checks = {}

    def make_gui(self):
        ui = super().make_gui()
        ui.base_title = 'Archipelago Mickey Client'
        return ui

    def on_package(self, cmd, args):
        super().on_package(cmd, args)
        if cmd == 'Connected':
            slot = args['slot_data']
            self.slot_validated = slot['protocol'] == 3
            if self.slot_validated:
                self.token = bytes.fromhex(slot['token'])
                self.checks = {int(k): v for k, v in slot['checks'].items()}
                self.flag_checks = {int(k): v for k, v in slot.get('flag_checks', {}).items()}
        elif cmd == 'ConnectionRefused':
            self.slot_validated = False

    async def disconnect(self, allow_autoreconnect=False):
        self.slot_validated = False
        await super().disconnect(allow_autoreconnect)


async def game_watcher(ctx):
    connected = None
    last_sent = None
    while not ctx.exit_event.is_set():
        try:
            if not dolphin.is_hooked():
                if connected is not False:
                    logger.info('Waiting for Dolphin')
                connected = False
                last_sent = None
                dolphin.hook()
            if dolphin.is_hooked():
                if connected is not True:
                    logger.info('Connected to Dolphin')
                connected = True
                if not ctx.server or not ctx.slot_validated:
                    last_sent = None
                elif (state := read_game_state(ctx)) is not None:
                    if state['goal'] and not ctx.finished_game:
                        await ctx.send_msgs([{'cmd': 'StatusUpdate', 'status': ClientStatus.CLIENT_GOAL}])
                        ctx.finished_game = True
                    # Server authority is checked before every command, including after reconnect.
                    missing = state['checked'] - ctx.checked_locations
                    fingerprint = frozenset(missing)
                    if missing and fingerprint != last_sent:
                        await ctx.send_msgs([{'cmd': 'LocationChecks', 'locations': sorted(missing)}])
                        last_sent = fingerprint
                    elif not missing:
                        last_sent = None
                    receive_items(ctx)
        except (RuntimeError, OSError):
            last_sent = None
            if dolphin.is_hooked():
                dolphin.un_hook()
        await asyncio.sleep(0.25)
    if dolphin.is_hooked():
        dolphin.un_hook()


def run_game(rom):
    settings = get_settings().mickey_options
    if not settings.rom_start:
        return
    dolphin_path = settings.dolphin_path
    if not dolphin_path:
        return
    exec_arg = f"--exec={os.path.realpath(rom)}"
    if sys.platform == 'darwin' and dolphin_path.endswith('.app'):
        executable = os.path.join(dolphin_path, 'Contents', 'MacOS', 'Dolphin')
        command = ([executable, exec_arg] if os.path.isfile(executable)
                   else ['open', '-a', dolphin_path, '--args', exec_arg])
    else:
        command = [dolphin_path, exec_arg]
    subprocess.Popen(command, cwd=Utils.local_path('.'), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def main(patch_file=None):
    metadata = {}
    if patch_file:
        metadata, output_file = Patch.create_rom_file(patch_file)
        run_game(output_file)
    ctx = MickeyContext(metadata.get('server'))
    ctx.auth = metadata.get('player_name')
    ctx.server_task = asyncio.create_task(server_loop(ctx), name='AP server')
    if gui_enabled:
        ctx.run_gui()
    ctx.run_cli()
    watcher = asyncio.create_task(game_watcher(ctx), name='Dolphin')
    try:
        await ctx.exit_event.wait()
    finally:
        ctx.server_address = None
        ctx.exit_event.set()
        await watcher
        await ctx.shutdown()


def launch(patch_file=None):
    logging.getLogger().setLevel(logging.INFO)
    import colorama
    colorama.just_fix_windows_console()
    try:
        asyncio.run(main(patch_file))
    finally:
        colorama.deinit()
