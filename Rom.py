"""ROM-free AP descriptors and the installed APworld patching entry point."""
import hashlib
import json
import logging
import os
import pkgutil
import struct
import tempfile
from io import BytesIO
from pathlib import Path

import bsdiff4

from settings import get_settings
from worlds.Files import APProcedurePatch, APPatchExtension, AutoPatchExtensionRegister

from .Items import items_by_id
from .MickeyPatcher import MickeyPatcher
from .Rules import load_rules
from .Regions import all_entrances
from gclib import fs_helpers as fs


def resource(name: str) -> bytes:
    data = pkgutil.get_data(__package__, name)
    if data is None:
        raise ValueError(f"Missing installed Mickey resource: {name}")
    return data


def load_json(name: str):
    return json.loads(resource(name))


def configure_runtime(runtime, layout, descriptor):
    result = bytearray(runtime)
    config = layout['config_offset']
    result[config + 4:config + 20] = bytes.fromhex(descriptor['token'])
    struct.pack_into('>III', result, config + 20, int(descriptor['tricks_enabled']),
                     descriptor['runtime_abi'], int(descriptor['door_keys_enabled']))
    mask = bytearray(5)
    for door in load_rules()['doors']:
        if 'lock_index' in door and door['id'] not in descriptor['locked_doors']:
            index = door['lock_index']
            mask[index // 8] |= 1 << (index % 8)
    result[config + 32:config + 37] = mask
    enabled_checks = bytearray(32)
    for bit in descriptor.get('checks', {}).values():
        enabled_checks[bit // 8] |= 1 << (bit % 8)
    result[config + 40:config + 72] = enabled_checks
    return bytes(result)


def load_template():
    return load_json('patches/runtime_template.json')


def identity(seed_name, player, name):
    return hashlib.sha256(json.dumps([seed_name, player, name], separators=(',', ':')).encode()).hexdigest()[:32]


def pickup_model(original, appearance, texture):
    """Change a scene mesh while preserving its placement and interaction data."""
    model = bytearray(appearance)
    model[15] = texture
    model[0x38:0x54] = original[0x38:0x54]
    model[0x5c:0x60] = original[0x5c:0x60]
    joints = original[13]
    model[13] = joints + 1
    old_transforms, old_parents = struct.unpack_from('>II', original, 0x20)
    parents = len(model)
    model.extend(original[old_parents:old_parents + 2 * joints] + b'\xff\xff')
    model.extend(bytes(-len(model) % 4))
    transforms = len(model)
    model.extend(original[old_transforms:old_transforms + 36 * joints])
    model.extend(struct.pack('>9f', 0, 0, 0, 0, 0, 0, 1, 1, 1))
    struct.pack_into('>II', model, 0x20, transforms, parents)
    position = struct.unpack_from('>I', model, 0x10)[0]
    while model[position] in (0x80, 0x88, 0x90, 0x98, 0xa0):
        count = struct.unpack_from('>H', model, position + 1)[0]
        position += 3
        for vertex in range(count):
            model[position + vertex * 7] = joints * 3
        position += count * 7
    collision, hit = struct.unpack_from('>II', original, 0x54)
    target = len(model)
    model.extend(original[collision:])
    struct.pack_into('>II', model, 0x54, target, target + hit - collision)
    centre = list(struct.unpack_from('>3f', original, 0x28))
    start, end = struct.unpack_from('>II', original)
    old_y = [struct.unpack_from('>f', original, p + 4)[0] for p in range(start, end, 12)]
    start, end = struct.unpack_from('>II', model)
    vertices = [struct.unpack_from('>3f', model, p) for p in range(start, end, 12)]
    if max(old_y) - min(old_y) < 1:
        centre[1] = min(old_y) - min(v[1] for v in vertices) + .4
    for offset, vertex in zip(range(start, end, 12), vertices):
        struct.pack_into('>3f', model, offset, *(vertex[i] + centre[i] for i in range(3)))
    old_centre = struct.unpack_from('>3f', original, 0x28)
    distance = sum((centre[i] - old_centre[i]) ** 2 for i in range(3)) ** .5
    radius = max(struct.unpack_from('>f', original, 0x34)[0],
                 struct.unpack_from('>f', model, 0x34)[0] + distance)
    struct.pack_into('>4f', model, 0x28, *old_centre, radius)
    model.extend(bytes(-len(model) % 32))
    return bytes(model)


class MickeyPatchExtension(APPatchExtension):
    game = "Disney's Magical Mirror"

    @staticmethod
    def patch_mod(caller):
        caller.report('Applying the bundled Mickey mod...')
        patcher = caller.patcher
        costumes = load_json('patches/costumes.json')
        costume = caller.descriptor['costume']
        patches = [patcher.manifest['patches']]
        if costume != 'original':
            patches.append(costumes['shared'])
        patches.append(costumes['costumes'][costume])
        for entries in patches:
            for name, entry in entries.items():
                before = fs.read_all_bytes(patcher.iso.get_changed_file_data(name))
                patcher.iso.changed_files[name] = BytesIO(bsdiff4.patch(before, resource(entry)))
        patcher.iso.add_new_file('files/mickey/ap.rel', BytesIO(resource(patcher.manifest['runtime'])))

    @staticmethod
    def patch_items(caller):
        caller.report('Writing item placements and seed settings into the ISO...')
        patcher, descriptor = caller.patcher, caller.descriptor
        template = load_template()
        for rec in template['recipe']['locations']:
            area = int(rec['rel'][4:])
            name = f'files/mickey/area{area:02d}/{rec["rel"]}.rel'
            data = patcher.iso.get_changed_file_data(name)
            index = rec['items'].index('ap')
            kind, script = rec['bases']
            ground, display, reward = (int(offset, 16) for offset in rec['offsets'])
            fs.write_u8(data, ground, kind + 2 * index)
            fs.write_u8(data, ground + 3, script + 4 * index)
            fs.write_u8(data, display, kind + 2 * index + 1)
            fs.write_u8(data, reward, script + 4 * index + 3)
            for offset in rec.get('extra', []):
                fs.write_u8(data, int(offset, 16), kind + 2 * index)
            patcher.iso.changed_files[name] = data
        visuals = template['pickup_models']
        models, rooms, textures = {}, {}, {}
        doorways = {entrance.name: entrance.doorway for entrance in all_entrances if entrance.doorway}
        for location, visual in descriptor['placements'].items():
            rec = visuals['locations'][location]
            area = rec['area']
            if actor := rec.get('actor'):
                name = f'files/mickey/area{area:02d}/{actor}'
                before = fs.read_all_bytes(patcher.iso.get_changed_file_data(name + '.bin'))
                delta = resource(f'patches/models/souvenirs/{actor}-{visual}.bsdiff')
                patcher.iso.changed_files[name + '.bin'] = BytesIO(bsdiff4.patch(before, delta))
                if visual not in textures:
                    source, delta = visuals['models'][visual]['texture']
                    before = fs.read_all_bytes(patcher.iso.read_file_data(source))
                    textures[visual] = bsdiff4.patch(before, resource(delta))
                patcher.iso.changed_files[name + '.tpl'] = BytesIO(textures[visual])
                continue
            name = f'files/mickey/area{area:02d}/sobj{area:03d}.bin'
            if visual not in models:
                model = visuals['models'][visual]
                source, offset, size = model['source']
                before = fs.read_bytes(patcher.iso.read_file_data(source), offset, size)
                models[visual] = bsdiff4.patch(before, resource(model['patch']))
            if name not in rooms:
                data = fs.read_all_bytes(patcher.iso.get_changed_file_data(name))
                count = struct.unpack_from('>I', data)[0]
                offsets = list(struct.unpack_from(f'>{count}I', data, 4)) + [len(data)]
                rooms[name] = (data[:offsets[0]], [data[a:b] for a, b in zip(offsets, offsets[1:])])
            header, blobs = rooms[name]
            texture = visuals['textures'][str(area)][visual]
            for slot in rec['slots']:
                blobs[slot] = pickup_model(blobs[slot], models[visual], texture)
            for slot in rec.get('hide', ()):
                hidden = bytearray(blobs[slot])
                struct.pack_into('>I', hidden, 0x14, 0)  # no display list; object/handshake remains
                blobs[slot] = bytes(hidden)
        for name, (header, blobs) in rooms.items():
            data = BytesIO(header)
            for slot, blob in enumerate(blobs):
                data.seek(0, 2)
                offset = data.tell()
                fs.write_u32(data, 4 + slot * 4, offset)
                fs.write_bytes(data, offset, blob)
                fs.align_data_to_nearest(data, 32, padding_bytes=b'\0')
            patcher.iso.changed_files[name] = data
        for trick, writes in template['trick_cost_writes'].items():
            for name, offset, state_flag in writes:
                data = patcher.iso.get_changed_file_data(name)
                fs.write_u8(data, offset, state_flag | descriptor['trick_costs'][trick])
                patcher.iso.changed_files[name] = data
        for name, offset, operand, *value in template['shard_threshold_writes']:
            data = patcher.iso.get_changed_file_data(name)
            if operand == 'branch':
                fs.write_u32(data, offset, value[0])
            else:
                fs.write_u16(data, offset, min(operand, descriptor['shards_required']))
            patcher.iso.changed_files[name] = data
        for source, target in descriptor['entrance_connections'].items():
            doorway = doorways[source]
            if 'hidden_hat_arrival' in doorway:
                data = patcher.iso.get_changed_file_data(doorway['file'])
                fs.write_u16(data, doorway['hidden_hat_arrival'], doorways[target]['arrival'][0])
                patcher.iso.changed_files[doorway['file']] = data
            # Room, spawn and post-spawn walk target are all destination data.
            for copy in (doorway, *doorway.get('copies', ())):
                name = copy['file']
                data = patcher.iso.get_changed_file_data(name)
                fs.write_bytes(data, copy['offset'], bytes(doorways[target]['arrival']))
                patcher.iso.changed_files[name] = data
            if 'transitions' in doorway:
                data = patcher.iso.get_changed_file_data(doorway['file'])
                for offset in doorway['transitions']:
                    # Native accepted-warp handler reads the seed-patched door
                    # row. Yield afterward so the door arrival owns player mode.
                    fs.write_bytes(data, offset, b'\x59\xfe\x0b\xc4')
                patcher.iso.changed_files[doorway['file']] = data
        for source, doorway in doorways.items():
            if sequence := doorway.get('minigame'):
                target = descriptor['entrance_connections'].get(source, doorway['reverse'])
                area, spawn, walk = doorways[target]['arrival']
                name = sequence['file']
                data = patcher.iso.get_changed_file_data(name)
                fs.write_u16(data, sequence['area_operand'], 0x100 | area)
                fs.write_u16(data, sequence['arrival_operand'], spawn << 8 | walk)
                patcher.iso.changed_files[name] = data
        for source, index in descriptor['door_locks'].items():
            doorway = doorways[source]
            for copy in (doorway, *doorway.get('copies', ())):
                name = copy['file']
                data = patcher.iso.get_changed_file_data(name)
                for offset, base in copy.get('lock_writes', ()):
                    fs.write_u16(data, offset, base + index)
                patcher.iso.changed_files[name] = data
        name = 'files/mickey/ap.rel'
        runtime = configure_runtime(fs.read_all_bytes(patcher.iso.get_changed_file_data(name)),
                                    patcher.manifest['resident'], descriptor)
        patcher.iso.changed_files[name] = BytesIO(runtime)

    @staticmethod
    def close_iso(caller):
        caller.report('Building the randomized ISO...')
        # gclib owns construction/export; no separate build_iso implementation.
        for _ in caller.patcher.iso.export_disc_to_iso_with_changed_files(str(caller.output_path)):
            pass


class MickeyProcedurePatch(APProcedurePatch):
    """TTYD-style AP procedures operate on gclib streams and export directly."""
    game = "Disney's Magical Mirror"
    hash = None
    patch_file_ending = '.apmickey'
    result_file_ending = '.iso'
    procedure = [("patch_mod", []), ("patch_items", []), ("close_iso", [])]

    def __init__(self, *args, descriptor=None, base_iso=None, progress=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.descriptor = descriptor
        self.base_iso = base_iso
        self.report = progress or logging.getLogger('MickeyPatch').info

    def write_contents(self, archive):
        if self.descriptor is None:
            raise ValueError('No Mickey seed descriptor to write')
        self.write_file('mickey.json', json.dumps(self.descriptor).encode())
        super().write_contents(archive)

    def read_contents(self, archive):
        manifest = super().read_contents(archive)
        self.descriptor = json.loads(self.get_file('mickey.json'))
        self.procedure = type(self).procedure
        return manifest

    def patch(self, target):
        self.read()
        output = Path(target).resolve()
        if output == Path(self.path).resolve():
            raise ValueError('Output must not replace the AP patch')
        base = Path(self.base_iso or get_settings().mickey_options.rom_file).resolve()
        if base == output or (output.exists() and os.path.samefile(base, output)):
            raise ValueError('Output must be a different file from the base game')
        self.report('Checking the base game...')
        self.patcher = MickeyPatcher(base, load_json('patches/base.json'))
        extensions = AutoPatchExtensionRegister.get_handler(self.game)
        if not isinstance(extensions, list):
            extensions = [extensions]
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.mickey-', dir=output.parent) as folder:
            self.output_path = Path(folder) / output.name
            for step, args in self.procedure:
                handler = next((getattr(ext, step) for ext in extensions if hasattr(ext, step)), None)
                if handler is None:
                    raise NotImplementedError(f'Unknown procedure {step} for {self.game}')
                handler(self, *args)
            with self.output_path.open('r+b') as stream:
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(self.output_path, output)
        self.report(f'Created {output}')


def write_files(world, patch: MickeyProcedurePatch) -> None:
    """Serialize every generated placement, with visuals for mapped ROM pickups."""
    template = load_template()
    locations = {location.address: location for location in world.get_locations()}
    placements = {}
    for location in template['pickup_models']['locations']:
        if int(location) not in locations:
            continue
        item = locations[int(location)].item
        visual = 'ap'
        if item.player == world.player:
            data = items_by_id[item.code]
            if data.door:
                visual = 'key'
            elif data.grant == 'inventory':
                visual = f'inventory_{data.rom_id:02x}'
            elif data.grant == 'ap_flag':
                visual = 'trick'
            else:
                visual = {79790001: 'shard', 79790002: 'vessel', 79790076: 'refill'}[item.code]
        placements[location] = visual
    patch.descriptor = {
        **{key: value for key, value in template.items() if key in ('patch_layout', 'runtime_abi', 'runtime_build_stamp')},
        **world.fill_slot_data(),
        'costume': world.options.costume.current_key,
        'tricks_enabled': bool(world.options.tricks),
        'door_keys_enabled': True,
        'seed_name': world.multiworld.seed_name,
        'placements': placements,
        'items': [{'location': location.address, 'item': location.item.code,
                   'player': location.item.player, 'name': location.item.name}
                  for location in locations.values()],
    }
