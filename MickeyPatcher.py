"""Open the player's supported GC disc, following TTYDPatcher."""

from .lib import setup_gclib_path

setup_gclib_path()
from gclib.gcm import GCM
from gclib import fs_helpers as fs


class MickeyPatcher:
    def __init__(self, base, manifest):
        with open(base, 'rb') as stream:
            header = fs.read_bytes(stream, 0, 0x20)
        if len(header) != 0x20 or header[:8] != b'GDME01\0\0' or header[0x1c:] != bytes.fromhex('c2339f3d'):
            raise ValueError('Select a clean GDME01 US revision 0 ISO/GCM; convert RVZ to ISO in Dolphin first')
        self.iso = GCM(str(base))
        self.iso.read_entire_disc()
        self.manifest = manifest
        if 'files/mickey/ap.rel' in self.iso.files_by_path:
            raise ValueError('Select your clean GDME01 revision 0 ISO, not an already patched or prepared ISO')
