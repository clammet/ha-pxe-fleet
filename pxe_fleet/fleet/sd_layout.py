"""SD format v2 and a bounded FAT32 reader; shared with the standalone Pi updater."""
import json
import os
import struct

FORMAT = 2
SECTOR = 512
SLOT_BYTES = 64 * 1024**2
IMAGE_BYTES = 256 * 1024**2
STARTS = {part: 2048 + (part - 1) * SLOT_BYTES // SECTOR for part in (1, 2, 3)}


def selector(active):
    if active not in (2, 3):
        raise ValueError("Invalid active SD slot")
    text = f"[all]\ntryboot_a_b=1\nboot_partition={active}\n[tryboot]\nboot_partition={5-active}\n#"
    return (text + " " * (SECTOR - len(text) - 1) + "\n").encode()


def selected(data):
    for part in (2, 3):
        if data == selector(part):
            return part
    raise ValueError("SD selector is damaged or belongs to another layout")


def mbr(card_id):
    data = bytearray(SECTOR)
    data[440:444] = bytes.fromhex(card_id[:8])
    for part, start in STARTS.items():
        offset = 446 + (part - 1) * 16
        data[offset:offset+16] = struct.pack("<B3sB3sII", 128 if part == 1 else 0,
            b"\xfe\xff\xff", 12, b"\xfe\xff\xff", start, SLOT_BYTES // SECTOR)
    data[510:512] = b"\x55\xaa"
    return bytes(data)


def read_at(stream, offset, length):
    stream.seek(offset)
    result = stream.read(length)
    if len(result) != length:
        raise ValueError("Truncated SD media")
    return result


def validate_mbr(stream, card_id):
    if read_at(stream, 0, SECTOR) != mbr(card_id):
        raise ValueError("SD partition layout/identity does not match a Fleet v2 card")


class Fat:
    """Read only short-name files in a FAT32 root, without mounting any SD partition."""
    def __init__(self, stream, offset=0):
        self.stream, self.offset = stream, offset
        bpb = read_at(stream, offset, SECTOR)
        size, self.spc, reserved, fats = struct.unpack_from("<HBHB", bpb, 11)
        sectors = struct.unpack_from("<I", bpb, 32)[0]
        fat_sectors = struct.unpack_from("<I", bpb, 36)[0]
        self.root = struct.unpack_from("<I", bpb, 44)[0]
        if (size != SECTOR or self.spc not in (1, 2, 4, 8, 16, 32, 64, 128)
                or fats != 2 or not reserved or not fat_sectors
                or sectors != SLOT_BYTES // SECTOR or bpb[510:512] != b"\x55\xaa"):
            raise ValueError("Invalid Fleet FAT32 filesystem")
        self.fat = reserved * SECTOR
        self.data = (reserved + fats * fat_sectors) * SECTOR
        self.cluster_bytes = self.spc * SECTOR
        self.max_cluster = (SLOT_BYTES - self.data) // self.cluster_bytes + 1
        if self.max_cluster < 2 or fat_sectors * SECTOR // 4 <= self.max_cluster:
            raise ValueError("Invalid FAT bounds")

    def chain(self, cluster, maximum):
        seen = set()
        while 2 <= cluster < 0x0ffffff8:
            if cluster > self.max_cluster or cluster in seen or len(seen) >= maximum:
                raise ValueError("Invalid/oversized FAT chain")
            seen.add(cluster)
            yield self.offset + self.data + (cluster - 2) * self.cluster_bytes
            cluster = struct.unpack("<I", read_at(self.stream, self.offset + self.fat + cluster * 4, 4))[0] & 0x0fffffff
        if cluster < 0x0ffffff8:
            raise ValueError("Incomplete FAT chain")

    def locate(self, short_name):
        encoded = short_name.encode("ascii")
        if len(encoded) != 11:
            raise ValueError("Expected a short FAT filename")
        for offset in self.chain(self.root, 32):
            block = read_at(self.stream, offset, self.cluster_bytes)
            for i in range(0, len(block), 32):
                entry = block[i:i+32]
                if entry[0] == 0:
                    raise FileNotFoundError(short_name)
                if entry[0] == 0xe5 or entry[11] & 0x18 or entry[11] == 0x0f:
                    continue
                if entry[:11] == encoded:
                    cluster = (struct.unpack_from("<H", entry, 20)[0] << 16) | struct.unpack_from("<H", entry, 26)[0]
                    return cluster, struct.unpack_from("<I", entry, 28)[0]
        raise FileNotFoundError(short_name)

    def file(self, short_name, limit=16384):
        cluster, size = self.locate(short_name)
        if not 0 < size <= limit:
            raise ValueError("Invalid FAT file size")
        chunks = []
        remaining = size
        for offset in self.chain(cluster, (size + self.cluster_bytes - 1) // self.cluster_bytes):
            count = min(remaining, self.cluster_bytes)
            chunks.append(read_at(self.stream, offset, count))
            remaining -= count
        if remaining:
            raise ValueError("Short FAT file")
        return b"".join(chunks)

    def selector_offset(self):
        cluster, size = self.locate("AUTOBOOTTXT")
        if size != SECTOR:
            raise ValueError("Selector must occupy exactly one preallocated sector")
        offsets = list(self.chain(cluster, 1))
        if len(offsets) != 1 or offsets[0] % SECTOR:
            raise ValueError("Selector is not sector aligned")
        return offsets[0]

    def metadata(self, name="SLOT    ID "):
        value = json.loads(self.file(name))
        if not isinstance(value, dict) or value.get("format") != FORMAT:
            raise ValueError("Not Fleet SD format v2")
        return value


def write_at(stream, offset, data):
    stream.seek(offset)
    if stream.write(data) != len(data):
        raise OSError("Short SD write")


def sync(stream):
    stream.flush()
    os.fsync(stream.fileno())
