"""Verified upstream image discovery and extraction into private build directories."""
from contextlib import ExitStack
import hashlib
import lzma
from pathlib import Path
import re
import shutil
import urllib.request

from .util import capture, run

LATEST = "https://downloads.raspberrypi.com/raspios_lite_arm64_latest"


def open_url(url, method="GET"):
    request = urllib.request.Request(url, method=method, headers={"User-Agent": "pxe-fleet/0.1"})
    response = urllib.request.urlopen(request, timeout=60)
    if not response.url.startswith("https://"):
        response.close()
        raise ValueError("Refusing a download redirected away from HTTPS")
    return response


def discover(pinned=None):
    if pinned:
        return dict(pinned)
    with open_url(LATEST, "HEAD") as response:
        url = response.url
    if not re.fullmatch(r"https://downloads\.raspberrypi\.(?:com|org)/raspios_lite_arm64/images/[^\s]+\.img\.xz", url):
        raise ValueError(f"Unexpected Raspberry Pi image URL: {url}")
    with open_url(url + ".sha256") as response:
        checksum = response.read(4096).decode().strip()
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?([^\s]+)", checksum)
    if not match or match[2] != url.rsplit("/", 1)[1]:
        raise ValueError("Upstream checksum does not identify the selected image")
    return {"url": url, "sha256": match[1].lower()}


def fetch_verified(url, destination, expected):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and file_hash(destination) == expected.lower():
        return destination
    temp = destination.with_suffix(".partial")
    try:
        digest = hashlib.sha256()
        with open_url(url) as response, temp.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != expected.lower():
            raise ValueError(f"SHA256 mismatch for {url}")
        temp.replace(destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def image_file(release, cache):
    archive = fetch_verified(release["url"], cache / (release["sha256"] + ".xz"), release["sha256"])
    image = cache / (release["sha256"] + ".img")
    if not image.exists():
        temporary = image.with_suffix(".extracting")
        try:
            with lzma.open(archive) as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
            temporary.replace(image)
        finally:
            temporary.unlink(missing_ok=True)
    return image


def partitions(output):
    result = {}
    for line in output.splitlines():
        number, start, sectors = map(int, line.split())
        if start <= 0 or sectors <= 0 or number in result:
            raise ValueError("Invalid image partition table")
        result[number] = (start * 512, sectors * 512)
    if set(result) != {1, 2}:
        raise ValueError("Expected a Raspberry Pi image with boot and root partitions")
    return result


def extract(image, root, scratch):
    table = partitions(capture(["partx", "--raw", "--noheadings", "-o", "NR,START,SECTORS", image]))
    with ExitStack() as stack:
        for number, target, fs in ((2, root, "ext4"), (1, root / "boot/firmware", "vfat")):
            offset, size = table[number]
            if offset + size > image.stat().st_size:
                raise ValueError("Partition extends beyond image")
            loop = capture(["losetup", "--find", "--show", "--read-only", "--offset", offset, "--sizelimit", size, image])
            stack.callback(run, ["losetup", "--detach", loop])
            mount = scratch / f"partition-{number}"
            mount.mkdir()
            run(["mount", "-t", fs, "-o", "ro,noload" if fs == "ext4" else "ro", loop, mount])
            stack.callback(run, ["umount", mount])
            target.mkdir(parents=True, exist_ok=True)
            run(["rsync", "-aHAX", "--numeric-ids", f"{mount}/", f"{target}/"])
