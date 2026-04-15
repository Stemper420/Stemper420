#!/usr/bin/env python3
"""MADRIX 5 -> Philips Hue Entertainment bridge.

Architecture:
    MADRIX 5 -> Art-Net UDP -> this bridge -> Hue Entertainment API

This tool intentionally avoids MADRIX Script. Instead, MADRIX sends Art-Net to a
local UDP socket, and this process remaps DMX RGB triplets into Hue
Entertainment Area channel updates.

Commands:
    pair   - create Hue credentials and optionally write a starter config
    areas  - list Entertainment Areas and channel IDs
    run    - run the bridge using a JSON config file

Dependencies:
    pip install requests hue-entertainment-pykit

Notes:
    * Uses local HTTPS to the Hue Bridge for pairing.
    * For simplicity, certificate verification is disabled by default during the
      local pairing calls. The streaming layer is handled by hue-entertainment-pykit.
    * Tested only for syntax here; live Hue/MADRIX validation is still required
      on the target machine.
"""

from __future__ import annotations

import argparse
import json
import logging
import select
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
import urllib3
from hue_entertainment_pykit import Discovery, Entertainment, Streaming
from hue_entertainment_pykit.models.bridge import Bridge

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ARTNET_PORT = 6454
ARTNET_HEADER = b"Art-Net\x00"
ARTNET_OPCODE_DMX = 0x5000
DMX_UNIVERSE_SIZE = 512
DEFAULT_FPS = 20.0

LOG = logging.getLogger("madrix_hue_bridge")


@dataclass(frozen=True)
class DmxAddress:
    port_address: int
    dmx_address: int  # 1..512

    def __post_init__(self) -> None:
        if self.port_address < 0:
            raise ValueError(f"port_address must be >= 0, got {self.port_address}")
        if not 1 <= self.dmx_address <= DMX_UNIVERSE_SIZE:
            raise ValueError(
                f"dmx_address must be in 1..{DMX_UNIVERSE_SIZE}, got {self.dmx_address}"
            )


@dataclass(frozen=True)
class ChannelMap:
    channel_id: int
    red: DmxAddress
    green: DmxAddress
    blue: DmxAddress

    def __post_init__(self) -> None:
        if self.channel_id < 0:
            raise ValueError(f"channel_id must be >= 0, got {self.channel_id}")


class ArtNetStore:
    """Stores latest Art-Net DMX frames by port-address."""

    def __init__(self) -> None:
        self._universes: Dict[int, bytearray] = {}
        self._sequence: Dict[int, int] = {}

    def update_packet(self, payload: bytes) -> Optional[int]:
        if len(payload) < 18:
            return None
        if payload[:8] != ARTNET_HEADER:
            return None
        opcode = struct.unpack("<H", payload[8:10])[0]
        if opcode != ARTNET_OPCODE_DMX:
            return None

        port_address = struct.unpack("<H", payload[14:16])[0]
        length = struct.unpack(">H", payload[16:18])[0]
        dmx = payload[18 : 18 + length]
        if not dmx:
            return None

        universe = bytearray(DMX_UNIVERSE_SIZE)
        universe[: min(len(dmx), DMX_UNIVERSE_SIZE)] = dmx[:DMX_UNIVERSE_SIZE]
        self._universes[port_address] = universe
        self._sequence[port_address] = payload[12]
        return port_address

    def get_value(self, addr: DmxAddress) -> int:
        universe = self._universes.get(addr.port_address)
        if universe is None:
            return 0
        index = addr.dmx_address - 1
        if index < 0 or index >= DMX_UNIVERSE_SIZE:
            return 0
        return int(universe[index])


class ArtNetListener:
    def __init__(self, bind_ip: str, port: int) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((bind_ip, port))
        self._sock.setblocking(False)
        self.store = ArtNetStore()

    def poll(self, timeout: float) -> bool:
        ready, _, _ = select.select([self._sock], [], [], timeout)
        changed = False
        if not ready:
            return changed
        while True:
            try:
                packet, _addr = self._sock.recvfrom(2048)
            except BlockingIOError:
                break
            if self.store.update_packet(packet) is not None:
                changed = True
        return changed

    def close(self) -> None:
        self._sock.close()


class GracefulExit:
    def __init__(self) -> None:
        self.stop = False
        signal.signal(signal.SIGINT, self._handle)
        signal.signal(signal.SIGTERM, self._handle)

    def _handle(self, signum, frame) -> None:  # type: ignore[no-untyped-def]
        LOG.info("Received signal %s, stopping...", signum)
        self.stop = True


# ----------------------- Hue helpers -----------------------

def hue_pair(bridge_ip: str, device_name: str, timeout: float = 60.0) -> Tuple[str, str]:
    """Pair with Hue Bridge and request username + clientkey.

    The user must press the physical link button on the bridge.
    """
    deadline = time.monotonic() + timeout
    url = f"https://{bridge_ip}/api"
    body = {"devicetype": device_name, "generateclientkey": True}

    LOG.info("Press the link button on the Hue Bridge now.")
    last_error = ""
    while time.monotonic() < deadline:
        resp = requests.post(url, json=body, timeout=5, verify=False)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not data:
            raise RuntimeError(f"Unexpected Hue response: {data!r}")
        item = data[0]
        success = item.get("success")
        if success:
            username = success["username"]
            clientkey = success["clientkey"]
            return username, clientkey
        err = item.get("error", {})
        last_error = err.get("description", "unknown error")
        if "link button not pressed" in last_error.lower():
            time.sleep(1.0)
            continue
        raise RuntimeError(f"Hue pairing failed: {last_error}")

    raise TimeoutError(f"Timed out waiting for bridge button press. Last error: {last_error}")


def discover_bridge(bridge_ip: str) -> Bridge:
    bridges = Discovery().discover_bridges(bridge_ip)
    if not bridges:
        raise RuntimeError(f"Hue Bridge was not discovered at {bridge_ip}")
    return list(bridges.values())[0]


def discover_bridges() -> Dict[str, dict]:
    result = Discovery().discover_bridges()
    return {bridge_id: bridge.to_dict() for bridge_id, bridge in result.items()}


def authenticated_bridge(bridge_ip: str, username: str, clientkey: str) -> Bridge:
    base = discover_bridge(bridge_ip)
    data = base.to_dict()
    data["username"] = username
    data["clientkey"] = clientkey
    return Bridge.from_dict(data)


def fetch_entertainment_configs(bridge_ip: str, username: str, clientkey: str):
    bridge = authenticated_bridge(bridge_ip, username, clientkey)
    service = Entertainment(bridge)
    configs = service.get_entertainment_configs()
    return bridge, service, configs


def _hue_v2_get(bridge_ip: str, username: str, resource: str) -> list[dict[str, Any]]:
    url = f"https://{bridge_ip}/clip/v2/resource/{resource}"
    headers = {"hue-application-key": username}
    resp = requests.get(url, headers=headers, timeout=10, verify=False)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or "data" not in data or not isinstance(data["data"], list):
        raise RuntimeError(f"Unexpected Hue v2 response for {resource}: {data!r}")
    return data["data"]


def fetch_bridge_lights(
    bridge_ip: str,
    username: str,
    clientkey: Optional[str] = None,
) -> List[dict[str, Any]]:
    lights = _hue_v2_get(bridge_ip, username, "light")
    channel_ids_by_rid: Dict[str, set[int]] = {}
    area_names_by_rid: Dict[str, set[str]] = {}

    if clientkey:
        _bridge, _service, configs = fetch_entertainment_configs(bridge_ip, username, clientkey)
        for cfg in configs.values():
            area_label = cfg.name or cfg.id
            for channel in cfg.channels:
                for member in channel.members:
                    rid = member.service.rid
                    channel_ids_by_rid.setdefault(rid, set()).add(int(channel.channel_id))
                    area_names_by_rid.setdefault(rid, set()).add(area_label)

    result: List[dict[str, Any]] = []
    for light in lights:
        metadata = light.get("metadata", {})
        light_id = str(light.get("id", ""))
        result.append(
            {
                "id": light_id,
                "id_v1": light.get("id_v1"),
                "name": metadata.get("name", light_id),
                "archetype": metadata.get("archetype", ""),
                "channel_ids": sorted(channel_ids_by_rid.get(light_id, set())),
                "areas": sorted(area_names_by_rid.get(light_id, set())),
            }
        )

    result.sort(key=lambda item: (item["name"], item["id"]))
    return result


# ----------------------- Mapping helpers -----------------------

def roll_dmx(base_port_address: int, base_dmx_address: int, offset: int) -> DmxAddress:
    idx = (base_dmx_address - 1) + offset
    port_address = base_port_address + (idx // DMX_UNIVERSE_SIZE)
    dmx_address = (idx % DMX_UNIVERSE_SIZE) + 1
    return DmxAddress(port_address=port_address, dmx_address=dmx_address)


def build_sequential_mapping(mapping_cfg: dict) -> List[ChannelMap]:
    channel_ids = mapping_cfg["channels"]
    if not isinstance(channel_ids, list) or not channel_ids:
        raise ValueError("mapping.channels must be a non-empty list of Hue channel IDs")

    base_port_address = int(mapping_cfg.get("port_address_start", 0))
    base_dmx_address = int(mapping_cfg.get("dmx_start", 1))
    order = str(mapping_cfg.get("channel_order", "RGB")).upper()
    if sorted(order) != ["B", "G", "R"]:
        raise ValueError("mapping.channel_order must be a permutation of RGB")

    mapping: List[ChannelMap] = []
    for idx, channel_id in enumerate(channel_ids):
        start = idx * 3
        components = {
            order[0]: roll_dmx(base_port_address, base_dmx_address, start + 0),
            order[1]: roll_dmx(base_port_address, base_dmx_address, start + 1),
            order[2]: roll_dmx(base_port_address, base_dmx_address, start + 2),
        }
        mapping.append(
            ChannelMap(
                channel_id=int(channel_id),
                red=components["R"],
                green=components["G"],
                blue=components["B"],
            )
        )
    return mapping


def build_explicit_mapping(mapping_cfg: dict) -> List[ChannelMap]:
    items = mapping_cfg["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("mapping.items must be a non-empty list")

    mapping: List[ChannelMap] = []
    for item in items:
        mapping.append(
            ChannelMap(
                channel_id=int(item["channel_id"]),
                red=DmxAddress(int(item["r"]["port_address"]), int(item["r"]["dmx_address"])),
                green=DmxAddress(int(item["g"]["port_address"]), int(item["g"]["dmx_address"])),
                blue=DmxAddress(int(item["b"]["port_address"]), int(item["b"]["dmx_address"])),
            )
        )
    return mapping


def build_mapping(config: dict) -> List[ChannelMap]:
    mapping_cfg = config["mapping"]
    mapping_type = mapping_cfg.get("type", "sequential_rgb")
    if mapping_type == "sequential_rgb":
        return build_sequential_mapping(mapping_cfg)
    if mapping_type == "explicit":
        return build_explicit_mapping(mapping_cfg)
    raise ValueError(f"Unsupported mapping.type: {mapping_type}")


def frame_from_store(store: ArtNetStore, mapping: Iterable[ChannelMap]) -> Dict[int, Tuple[int, int, int]]:
    frame: Dict[int, Tuple[int, int, int]] = {}
    for entry in mapping:
        frame[entry.channel_id] = (
            store.get_value(entry.red),
            store.get_value(entry.green),
            store.get_value(entry.blue),
        )
    return frame


# ----------------------- Commands -----------------------

def cmd_pair(args: argparse.Namespace) -> int:
    username, clientkey = hue_pair(args.bridge_ip, args.device_name, timeout=args.timeout)
    bridge, service, configs = fetch_entertainment_configs(args.bridge_ip, username, clientkey)

    channel_ids: List[int] = []
    default_area_id = None
    if configs:
        first = list(configs.values())[0]
        default_area_id = first.id
        channel_ids = [int(ch.channel_id) for ch in first.channels]

    starter = {
        "hue": {
            "bridge_ip": args.bridge_ip,
            "username": username,
            "clientkey": clientkey,
            "entertainment_area_id": default_area_id,
        },
        "artnet": {
            "bind": "0.0.0.0",
            "port": ARTNET_PORT,
        },
        "stream": {
            "fps": DEFAULT_FPS,
            "change_threshold": 0,
        },
        "mapping": {
            "type": "sequential_rgb",
            "port_address_start": 0,
            "dmx_start": 1,
            "channel_order": "RGB",
            "channels": channel_ids,
        },
    }

    output = json.dumps(
        {
            "username": username,
            "clientkey": clientkey,
            "discovered_bridge": bridge.to_dict(),
            "entertainment_areas": {
                cfg.id: {
                    "name": cfg.name,
                    "channel_ids": [int(ch.channel_id) for ch in cfg.channels],
                    "configuration_type": cfg.configuration_type.value,
                }
                for cfg in configs.values()
            },
            "starter_config": starter,
        },
        indent=2,
        ensure_ascii=False,
    )
    print(output)

    if args.write_config:
        path = Path(args.write_config)
        path.write_text(json.dumps(starter, indent=2, ensure_ascii=False), encoding="utf-8")
        LOG.info("Starter config written to %s", path)

    return 0


def cmd_areas(args: argparse.Namespace) -> int:
    _bridge, _service, configs = fetch_entertainment_configs(
        args.bridge_ip, args.username, args.clientkey
    )

    result = {
        cfg.id: {
            "name": cfg.name,
            "status": cfg.status.value,
            "type": cfg.configuration_type.value,
            "channels": [
                {
                    "channel_id": int(ch.channel_id),
                    "position": {"x": ch.position.x, "y": ch.position.y, "z": ch.position.z},
                }
                for ch in cfg.channels
            ],
        }
        for cfg in configs.values()
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def resolve_entertainment_config(service: Entertainment, config: dict):
    configs = service.get_entertainment_configs()
    if not configs:
        raise ValueError(
            "No Entertainment Areas found on the Hue Bridge. Create one in the Hue app first."
        )
    area_id = config["hue"].get("entertainment_area_id")
    if area_id:
        selected = service.get_config_by_id(area_id)
        if selected is None:
            raise ValueError(f"Entertainment area not found: {area_id}")
        return selected
    if len(configs) == 1:
        return list(configs.values())[0]
    available = ", ".join(cfg.id for cfg in configs.values())
    raise ValueError(
        "Multiple Entertainment Areas found. Set hue.entertainment_area_id in config. "
        f"Available IDs: {available}"
    )


def cmd_run(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding="utf-8"))

    hue_cfg = config["hue"]
    bridge = authenticated_bridge(
        hue_cfg["bridge_ip"], hue_cfg["username"], hue_cfg["clientkey"]
    )
    entertainment = Entertainment(bridge)
    selected_area = resolve_entertainment_config(entertainment, config)
    mapping = build_mapping(config)

    valid_channel_ids = {int(ch.channel_id) for ch in selected_area.channels}
    mapped_ids = {m.channel_id for m in mapping}
    missing = mapped_ids - valid_channel_ids
    if missing:
        raise ValueError(
            f"Config maps unknown Hue channel IDs {sorted(missing)}. "
            f"Area contains {sorted(valid_channel_ids)}"
        )

    listener = ArtNetListener(
        bind_ip=config.get("artnet", {}).get("bind", "0.0.0.0"),
        port=int(config.get("artnet", {}).get("port", ARTNET_PORT)),
    )

    streaming = Streaming(bridge, selected_area, entertainment.get_ent_conf_repo())
    fps = float(config.get("stream", {}).get("fps", DEFAULT_FPS))
    interval = 1.0 / fps if fps > 0 else 1.0 / DEFAULT_FPS
    threshold = int(config.get("stream", {}).get("change_threshold", 0))
    color_space = str(config.get("stream", {}).get("color_space", "rgb")).lower()
    if color_space not in {"rgb", "xyb"}:
        raise ValueError("stream.color_space must be 'rgb' or 'xyb'")

    LOG.info("Listening Art-Net on %s:%s", config.get("artnet", {}).get("bind", "0.0.0.0"), config.get("artnet", {}).get("port", ARTNET_PORT))
    LOG.info("Using Hue area '%s' (%s)", selected_area.name, selected_area.id)
    LOG.info("Mapped Hue channel IDs: %s", sorted(mapped_ids))

    stop = GracefulExit()
    last_sent: Dict[int, Tuple[int, int, int]] = {}
    next_tick = time.monotonic()

    try:
        streaming.start_stream()
        streaming.set_color_space(color_space)

        while not stop.stop:
            now = time.monotonic()
            timeout = max(0.0, min(0.05, next_tick - now))
            listener.poll(timeout)
            now = time.monotonic()
            if now < next_tick:
                continue

            frame = frame_from_store(listener.store, mapping)
            any_sent = False
            for channel_id, rgb in frame.items():
                prev = last_sent.get(channel_id)
                if prev is not None and max(abs(a - b) for a, b in zip(prev, rgb)) <= threshold:
                    continue
                streaming.set_input((rgb[0], rgb[1], rgb[2], channel_id))
                last_sent[channel_id] = rgb
                any_sent = True

            if any_sent:
                LOG.debug("Sent %d Hue channel updates", len(last_sent))
            next_tick = now + interval
    finally:
        try:
            streaming.stop_stream()
        except Exception:  # pragma: no cover - best effort shutdown
            LOG.exception("Error while stopping Hue stream")
        listener.close()

    return 0


# ----------------------- CLI -----------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MADRIX 5 to Hue Entertainment bridge")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = parser.add_subparsers(dest="command", required=True)

    pair = sub.add_parser("pair", help="Pair with Hue Bridge and print starter config")
    pair.add_argument("--bridge-ip", required=True)
    pair.add_argument("--device-name", default="madrix-hue-bridge#local")
    pair.add_argument("--timeout", type=float, default=60.0)
    pair.add_argument("--write-config", help="Write starter config JSON to this file")
    pair.set_defaults(func=cmd_pair)

    areas = sub.add_parser("areas", help="List Entertainment Areas")
    areas.add_argument("--bridge-ip", required=True)
    areas.add_argument("--username", required=True)
    areas.add_argument("--clientkey", required=True)
    areas.set_defaults(func=cmd_areas)

    run = sub.add_parser("run", help="Run the Art-Net -> Hue bridge")
    run.add_argument("--config", required=True, help="Path to JSON config file")
    run.set_defaults(func=cmd_run)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOG.exception("Fatal error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
