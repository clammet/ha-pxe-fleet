"""Local operator commands, queued atomically for the controller."""
import argparse
import json
from pathlib import Path
import uuid

from .util import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["status", "check", "retry", "rollback"])
    parser.add_argument("serial", nargs="?")
    parser.add_argument("--storage", type=Path, default=Path("/data/fleet"))
    args = parser.parse_args()
    if args.command == "status":
        state = json.loads((args.storage / "state.json").read_text())
        result = {serial: {k: v for k, v in entry.items() if k not in ("token", "client")} for serial, entry in state["clients"].items()}
        print(json.dumps(result, indent=2))
        return
    if args.command in ("retry", "rollback") and not args.serial:
        parser.error("This command requires the eight-character serial suffix")
    request = {"command": args.command, "serial": args.serial}
    write_json(args.storage / "requests" / (uuid.uuid4().hex + ".json"), request)
    print("Request queued; inspect the add-on log for the result")


if __name__ == "__main__":
    main()
