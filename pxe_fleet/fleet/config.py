"""Strict desired-state validation. Never interpolate configuration into a shell."""
import copy
import ipaddress
from pathlib import PurePosixPath
import re
from urllib.parse import urlparse


class ConfigError(ValueError):
    pass


def fail(message):
    raise ConfigError(message)


def keys(value, allowed, label):
    if not isinstance(value, dict):
        fail(f"{label} must be a mapping")
    unknown = set(value) - set(allowed.split())
    if unknown:
        fail(f"Unknown {label} keys: {', '.join(sorted(unknown))}")


def string(value, pattern, label):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        fail(f"Invalid {label}: {value!r}")
    return value


def line(value, label):
    return string(value, r"[^\x00-\x1f\x7f]+", label)


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        fail(f"{label} must be an integer between {low} and {high}")
    return value


def array(value, label):
    if not isinstance(value, list):
        fail(f"{label} must be a list")
    return value


def ipv4(value):
    if not isinstance(value, str):
        fail("IPv4 addresses must be strings")
    try:
        return str(ipaddress.IPv4Address(value))
    except (ValueError, TypeError):
        fail(f"Invalid IPv4 address: {value!r}")


def https(value):
    line(value, "HTTPS URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        fail("Repository and image URLs must use HTTPS without embedded credentials")
    return value


def data_path(value):
    string(value, r"[a-zA-Z0-9_./-]+", "relative data path")
    path = PurePosixPath(value)
    if path.is_absolute() or value.split("/")[0] == ".fleet" or any(p in ("", ".", "..") for p in value.split("/")):
        fail("Data paths must be relative, without empty, '.' or '..' components")
    return value


def target_path(value):
    string(value, r"/[a-zA-Z0-9_./-]+", "mount target")
    if ".." in value.split("/") or "//" in value or value.endswith("/"):
        fail("Invalid mount target")
    return value


def validate(raw):
    cfg = copy.deepcopy(raw)
    keys(cfg, "server_ip control_port os_check_hours app_update_minutes boot_timeout_seconds min_free_gib overlay_size podman_size dns ssh_authorized_keys image clients", "fleet")
    cfg["server_ip"] = ipv4(cfg.get("server_ip"))
    for key, default, low, high in (
        ("control_port", 8099, 1024, 65535), ("os_check_hours", 24, 1, 720),
        ("app_update_minutes", 60, 5, 10080), ("boot_timeout_seconds", 900, 120, 7200),
        ("min_free_gib", 8, 1, 1024),
    ):
        cfg[key] = integer(cfg.get(key, default), low, high, key)
    for key, default in (("overlay_size", "50%"), ("podman_size", "25%")):
        cfg[key] = string(cfg.get(key, default), r"(?:[1-9][0-9]?%|[1-9][0-9]*[MG])", key)
    cfg["dns"] = [ipv4(v) for v in array(cfg.get("dns", [cfg["server_ip"]]), "dns")]
    if not cfg["dns"]:
        fail("At least one DNS server is required")
    cfg["ssh_authorized_keys"] = [line(v, "SSH public key") for v in array(cfg.get("ssh_authorized_keys", []), "ssh_authorized_keys")]
    image = cfg.setdefault("image", {})
    keys(image, "url sha256", "image")
    if image:
        https(image.get("url"))
        string(image.get("sha256"), r"[a-fA-F0-9]{64}", "image.sha256")
        image["sha256"] = image["sha256"].lower()
    clients = array(cfg.get("clients"), "clients")
    if not clients:
        fail("Configure at least one client")
    serials, ips, hosts = set(), set(), set()
    for c in clients:
        keys(c, "serial ip hostname model boot_options apt containers", "client")
        serial = string(c.get("serial"), r"(?:0x)?[a-fA-F0-9]{8,16}", "serial").lower().removeprefix("0x")[-8:]
        c["serial"] = serial
        c["ip"] = ipv4(c.get("ip"))
        string(c.get("hostname"), r"[a-z][a-z0-9-]{0,62}", "hostname")
        if c.get("model") not in ("pi3", "pi4", "pi5"):
            fail("model must be pi3, pi4 or pi5")
        if serial in serials or c["ip"] in ips or c["hostname"] in hosts or c["ip"] == cfg["server_ip"]:
            fail("Serial suffixes, IP addresses and hostnames must be unique")
        serials.add(serial); ips.add(c["ip"]); hosts.add(c["hostname"])
        opts = c.setdefault("boot_options", [])
        for opt in array(opts, "boot_options"):
            # Boot routing is controller-owned. Expose only board/peripheral settings.
            string(opt, r"(?:dtparam|dtoverlay|gpu_mem|enable_uart|force_turbo|arm_freq)=[a-zA-Z0-9_,.=-]+", "boot option")
        apt = c.setdefault("apt", {})
        keys(apt, "sources packages services persistent users", "apt")
        for key in ("sources", "packages", "services", "persistent", "users"):
            array(apt.setdefault(key, []), f"apt.{key}")
        usernames, uids, gids = set(), set(), set()
        for user in apt["users"]:
            keys(user, "name uid gid", "APT user")
            string(user.get("name"), r"[a-z_][a-z0-9_-]{0,30}", "service user name")
            integer(user.get("uid"), 2000, 60000, "service UID")
            integer(user.get("gid"), 2000, 60000, "service GID")
            if user["name"] in usernames or user["uid"] in uids or user["gid"] in gids:
                fail("Service user names, UIDs and GIDs must be unique")
            usernames.add(user["name"]); uids.add(user["uid"]); gids.add(user["gid"])
        names = set()
        for source in apt["sources"]:
            keys(source, "name url suites components key_url key_sha256", "APT source")
            name = string(source.get("name"), r"[a-z][a-z0-9-]*", "source.name")
            if name in names:
                fail("APT source names must be unique")
            names.add(name)
            https(source.get("url")); https(source.get("key_url"))
            string(source.get("key_sha256"), r"[a-fA-F0-9]{64}", "key_sha256")
            string(source.get("suites"), r"(?:[a-zA-Z0-9_.-]+|\{codename\})(?: [a-zA-Z0-9_.-]+)*", "suites")
            string(source.get("components", "main"), r"[a-zA-Z0-9_ /-]+", "components")
        for package in apt["packages"]:
            string(package, r"[a-z0-9][a-z0-9+.-]+", "package name")
            if package.startswith(("linux-image", "raspberrypi-kernel", "raspi-firmware")):
                fail("Kernel and firmware packages are managed by the server")
        for service in apt["services"]:
            string(service, r"[a-zA-Z0-9][a-zA-Z0-9_.@-]*\.service", "APT service")
        mounts = set()
        for bind in apt["persistent"]:
            keys(bind, "source target uid gid", "persistent directory")
            data_path(bind.get("source")); target_path(bind.get("target"))
            if not bind["target"].startswith(("/var/lib/", "/var/cache/", "/opt/")):
                fail("APT persistence targets must be application directories under /var/lib, /var/cache or /opt")
            if bind["target"].startswith(("/var/lib/dpkg", "/var/lib/apt", "/var/lib/containers")):
                fail("Package-manager and Podman storage cannot be persistent NFS mounts")
            if any(bind["target"] == p or bind["target"].startswith(p + "/") or p.startswith(bind["target"] + "/") for p in mounts):
                fail("Persistent targets must not overlap")
            mounts.add(bind["target"])
            for key in ("uid", "gid"):
                integer(bind.get(key), 0, 65534, key)
        containers = array(c.setdefault("containers", []), "containers")
        names = set()
        for app in containers:
            keys(app, "name image environment command ports volumes devices network", "container")
            name = string(app.get("name"), r"[a-z][a-z0-9-]{0,40}", "container.name")
            if name in names:
                fail("Container names must be unique")
            names.add(name)
            string(app.get("image"), r"[a-zA-Z0-9][a-zA-Z0-9.:-]*/[a-zA-Z0-9_./:@+-]+", "fully-qualified container image")
            registry = app["image"].split("/", 1)[0]
            if "." not in registry and ":" not in registry and registry != "localhost":
                fail("Container images must include an explicit registry hostname")
            if app.get("network", "bridge") not in ("bridge", "host"):
                fail("Container network must be bridge or host")
            env = app.setdefault("environment", {})
            if not isinstance(env, dict):
                fail("environment must be a mapping")
            for key, value in env.items():
                string(key, r"[A-Za-z_][A-Za-z0-9_]*", "environment name")
                if not isinstance(value, str) or "\x00" in value or "\n" in value or "\r" in value:
                    fail("Environment values must be single-line strings")
            for arg in array(app.setdefault("command", []), "command"):
                line(arg, "command argument")
            for port in array(app.setdefault("ports", []), "ports"):
                string(port, r"(?:[0-9.]+:)?[0-9]+:[0-9]+(?:/(?:tcp|udp))?", "port")
            for dev in array(app.setdefault("devices", []), "devices"):
                string(dev, r"/dev/[a-zA-Z0-9_/-]+(?::/dev/[a-zA-Z0-9_/-]+)?", "device")
            for vol in array(app.setdefault("volumes", []), "volumes"):
                keys(vol, "source target uid gid read_only", "container volume")
                data_path(vol.get("source")); target_path(vol.get("target"))
                for key in ("uid", "gid"):
                    integer(vol.get(key, 0), 0, 65534, key)
                if type(vol.get("read_only", False)) is not bool:
                    fail("read_only must be a boolean")
    return cfg


def client_spec(cfg, client):
    return {**client, **{k: cfg[k] for k in ("server_ip", "control_port", "app_update_minutes", "overlay_size", "podman_size", "dns", "ssh_authorized_keys")}}
