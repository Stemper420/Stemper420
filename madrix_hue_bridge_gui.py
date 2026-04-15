#!/usr/bin/env python3
"""GUI wrapper for MADRIX -> Hue bridge."""

from __future__ import annotations

import argparse
import json
import logging
import math
import queue
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional, Tuple

import madrix_hue_bridge as bridge

LOG = logging.getLogger("madrix_hue_bridge_gui")


def serialize_entertainment_configs(configs: dict) -> dict:
    return {
        cfg.id: {
            "name": cfg.name,
            "status": getattr(cfg.status, "value", str(cfg.status)),
            "type": getattr(cfg.configuration_type, "value", str(cfg.configuration_type)),
            "channel_ids": [int(ch.channel_id) for ch in cfg.channels],
        }
        for cfg in configs.values()
    }


def choose_best_area(areas: dict, preferred_area_id: str = "") -> Tuple[str, List[int]]:
    if preferred_area_id and preferred_area_id in areas:
        area = areas[preferred_area_id]
        return preferred_area_id, list(area.get("channel_ids", []))

    if len(areas) == 1:
        area_id, area = next(iter(areas.items()))
        return area_id, list(area.get("channel_ids", []))

    if not areas:
        return "", []

    area_id, area = max(
        areas.items(),
        key=lambda item: (len(item[1].get("channel_ids", [])), item[0]),
    )
    return area_id, list(area.get("channel_ids", []))


class QueueLogHandler(logging.Handler):
    def __init__(self, event_queue: "queue.Queue[dict]") -> None:
        super().__init__()
        self._queue = event_queue

    def emit(self, record: logging.LogRecord) -> None:
        self._queue.put({"type": "log", "message": self.format(record)})


class BackgroundTask(threading.Thread):
    def __init__(
        self,
        *,
        target: Callable[[], dict],
        event_queue: "queue.Queue[dict]",
        result_type: str,
    ) -> None:
        super().__init__(daemon=True)
        self._target = target
        self._queue = event_queue
        self._result_type = result_type

    def run(self) -> None:
        try:
            self._queue.put({"type": self._result_type, **self._target()})
        except Exception as exc:
            self._queue.put(
                {
                    "type": "error",
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )


class BridgeWorker(threading.Thread):
    def __init__(self, config: dict, event_queue: "queue.Queue[dict]") -> None:
        super().__init__(daemon=True)
        self._config = config
        self._queue = event_queue
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def _emit(self, event_type: str, **payload: object) -> None:
        self._queue.put({"type": event_type, **payload})

    def run(self) -> None:
        listener: Optional[bridge.ArtNetListener] = None
        streaming = None
        packets_total = 0
        hue_updates_total = 0
        try:
            config = self._config
            mapping = bridge.build_mapping(config)
            hue_cfg = config["hue"]
            bridge_obj = bridge.authenticated_bridge(
                hue_cfg["bridge_ip"], hue_cfg["username"], hue_cfg["clientkey"]
            )
            entertainment = bridge.Entertainment(bridge_obj)
            selected_area = bridge.resolve_entertainment_config(entertainment, config)

            valid_channel_ids = {int(ch.channel_id) for ch in selected_area.channels}
            mapped_ids = {m.channel_id for m in mapping}
            missing = mapped_ids - valid_channel_ids
            if missing:
                raise ValueError(
                    f"Config maps unknown Hue channel IDs {sorted(missing)}. "
                    f"Area contains {sorted(valid_channel_ids)}"
                )

            listener = bridge.ArtNetListener(
                bind_ip=config.get("artnet", {}).get("bind", "0.0.0.0"),
                port=int(config.get("artnet", {}).get("port", bridge.ARTNET_PORT)),
            )
            streaming = bridge.Streaming(bridge_obj, selected_area, entertainment.get_ent_conf_repo())
            fps = float(config.get("stream", {}).get("fps", bridge.DEFAULT_FPS))
            interval = 1.0 / fps if fps > 0 else 1.0 / bridge.DEFAULT_FPS
            threshold = int(config.get("stream", {}).get("change_threshold", 0))
            color_space = str(config.get("stream", {}).get("color_space", "rgb")).lower()
            if color_space not in {"rgb", "xyb"}:
                raise ValueError("stream.color_space must be 'rgb' or 'xyb'")

            self._emit("mapping", channel_ids=sorted(mapped_ids))
            self._emit(
                "started",
                started_at=time.time(),
                area=f"{selected_area.name} ({selected_area.id})",
            )

            streaming.start_stream()
            streaming.set_color_space(color_space)

            last_sent: Dict[int, Tuple[int, int, int]] = {}
            next_tick = time.monotonic()
            while not self._stop_event.is_set():
                now = time.monotonic()
                changed_ports = listener.poll(max(0.0, min(0.05, next_tick - now)))
                if changed_ports:
                    packets_total += len(changed_ports)
                    self._emit(
                        "artnet",
                        packets_total=packets_total,
                        last_port=changed_ports[-1],
                        last_packet_monotonic=time.monotonic(),
                    )

                now = time.monotonic()
                if now < next_tick:
                    continue

                frame = bridge.frame_from_store(listener.store, mapping)
                for channel_id, rgb in frame.items():
                    prev = last_sent.get(channel_id)
                    if prev is not None and max(abs(a - b) for a, b in zip(prev, rgb)) <= threshold:
                        continue
                    streaming.set_input((rgb[0], rgb[1], rgb[2], channel_id))
                    last_sent[channel_id] = rgb
                    hue_updates_total += 1
                self._emit("frame", frame=frame, hue_updates_total=hue_updates_total)
                next_tick = now + interval
        except Exception as exc:
            self._emit("error", message=str(exc), traceback=traceback.format_exc())
        finally:
            if streaming is not None:
                try:
                    streaming.stop_stream()
                except Exception:
                    LOG.exception("Error while stopping Hue stream")
            if listener is not None:
                listener.close()
            self._emit("stopped", packets_total=packets_total, hue_updates_total=hue_updates_total)


class App(tk.Tk):
    def __init__(self, initial_config: Optional[Path]) -> None:
        super().__init__()
        self.title("MADRIX Hue Bridge Monitor")
        self.geometry("1100x800")
        self.minsize(960, 680)
        self.configure(bg="#11161b")

        self.event_queue: "queue.Queue[dict]" = queue.Queue()
        self.worker: Optional[BridgeWorker] = None
        self.runtime_started_at: Optional[float] = None
        self.last_packet_monotonic: Optional[float] = None
        self.mapping_cfg: dict = {
            "type": "sequential_rgb",
            "port_address_start": 0,
            "dmx_start": 1,
            "channel_order": "RGB",
            "channels": [],
        }
        self.channel_items: Dict[int, Tuple[int, int]] = {}

        self.config_path_var = tk.StringVar(value=str(initial_config) if initial_config else "")
        self.bridge_ip_var = tk.StringVar(value="192.168.1.100")
        self.username_var = tk.StringVar()
        self.clientkey_var = tk.StringVar()
        self.area_id_var = tk.StringVar()
        self.bind_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value=str(bridge.ARTNET_PORT))
        self.fps_var = tk.StringVar(value=str(int(bridge.DEFAULT_FPS)))
        self.threshold_var = tk.StringVar(value="0")
        self.color_space_var = tk.StringVar(value="rgb")
        self.status_var = tk.StringVar(value="Idle")
        self.area_var = tk.StringVar(value="-")
        self.mapping_var = tk.StringVar(value="No mapping loaded")
        self.packets_var = tk.StringVar(value="0")
        self.updates_var = tk.StringVar(value="0")
        self.last_port_var = tk.StringVar(value="-")
        self.packet_age_var = tk.StringVar(value="-")
        self.uptime_var = tk.StringVar(value="-")

        self._build_ui()
        self._attach_logs()

        if initial_config and initial_config.exists():
            self.load_config(initial_config)
        else:
            default_config = Path(__file__).with_name("config.example.json")
            if default_config.exists():
                self.load_config(default_config)

        self.after(100, self.process_events)
        self.after(250, self.refresh_labels)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _attach_logs(self) -> None:
        handler = QueueLogHandler(self.event_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        self._log_handler = handler

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        top = ttk.Frame(self, padding=12)
        top.pack(fill="both", expand=True)

        controls = ttk.LabelFrame(top, text="Config And Controls", padding=10)
        controls.pack(fill="x")
        ttk.Entry(controls, textvariable=self.config_path_var, width=90).grid(row=0, column=0, columnspan=6, sticky="ew")
        ttk.Button(controls, text="Browse", command=self.browse_config).grid(row=0, column=6, padx=4)
        ttk.Button(controls, text="Load", command=self.load_config_from_entry).grid(row=0, column=7, padx=4)
        ttk.Button(controls, text="Save As", command=self.save_config).grid(row=0, column=8, padx=4)
        ttk.Button(controls, text="Find Bridge", command=self.on_find_bridge).grid(row=1, column=5, padx=4, pady=(8, 0))
        ttk.Button(controls, text="Pair", command=self.on_pair).grid(row=1, column=6, padx=4, pady=(8, 0))
        ttk.Button(controls, text="Areas", command=self.on_list_areas).grid(row=1, column=7, padx=4, pady=(8, 0))
        ttk.Button(controls, text="Find Lamps", command=self.on_find_lights).grid(row=1, column=8, padx=4, pady=(8, 0))
        ttk.Button(controls, text="Start", command=self.start_bridge).grid(row=1, column=9, padx=4, pady=(8, 0))
        ttk.Button(controls, text="Stop", command=self.stop_bridge).grid(row=1, column=10, padx=4, pady=(8, 0))

        form = ttk.Frame(top, padding=(0, 12, 0, 12))
        form.pack(fill="x")
        left = ttk.LabelFrame(form, text="Hue", padding=10)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        right = ttk.LabelFrame(form, text="Art-Net / Stream", padding=10)
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self._entry(left, 0, "Bridge IP", self.bridge_ip_var)
        self._entry(left, 1, "Username", self.username_var)
        self._entry(left, 2, "Clientkey", self.clientkey_var)
        self._entry(left, 3, "Area ID", self.area_id_var)
        self._entry(right, 0, "Bind", self.bind_var)
        self._entry(right, 1, "Port", self.port_var)
        self._entry(right, 2, "FPS", self.fps_var)
        self._entry(right, 3, "Threshold", self.threshold_var)
        ttk.Label(right, text="Color space").grid(row=4, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Combobox(right, textvariable=self.color_space_var, values=["rgb", "xyb"], state="readonly").grid(row=4, column=1, sticky="ew", pady=4)
        right.columnconfigure(1, weight=1)

        status = ttk.LabelFrame(top, text="Runtime", padding=10)
        status.pack(fill="x")
        self._stat(status, 0, 0, "State", self.status_var)
        self._stat(status, 0, 1, "Area", self.area_var)
        self._stat(status, 0, 2, "Mapping", self.mapping_var)
        self._stat(status, 1, 0, "Art-Net packets", self.packets_var)
        self._stat(status, 1, 1, "Hue updates", self.updates_var)
        self._stat(status, 1, 2, "Last port", self.last_port_var)
        self._stat(status, 2, 0, "Uptime", self.uptime_var)
        self._stat(status, 2, 1, "Last packet age", self.packet_age_var)

        visuals = ttk.LabelFrame(top, text="Channel Activity", padding=10)
        visuals.pack(fill="both", expand=True, pady=(12, 0))
        self.canvas = tk.Canvas(visuals, bg="#121a20", highlightthickness=0, height=320)
        self.canvas.pack(fill="both", expand=True)

        logs = ttk.LabelFrame(top, text="Events", padding=10)
        logs.pack(fill="both", expand=True, pady=(12, 0))
        self.log_widget = scrolledtext.ScrolledText(logs, height=12, bg="#0f1418", fg="#dce7f0", insertbackground="#dce7f0")
        self.log_widget.pack(fill="both", expand=True)
        self.log_widget.configure(state="disabled")

    def _entry(self, parent: ttk.LabelFrame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 8))
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4)
        parent.columnconfigure(1, weight=1)

    def _stat(self, parent: ttk.LabelFrame, row: int, column: int, label: str, variable: tk.StringVar) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=column, sticky="ew", padx=8, pady=4)
        ttk.Label(frame, text=label).pack(anchor="w")
        ttk.Label(frame, textvariable=variable, font=("Segoe UI", 10, "bold")).pack(anchor="w")
        parent.columnconfigure(column, weight=1)

    def append_log(self, message: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", message.rstrip() + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def browse_config(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if path:
            self.config_path_var.set(path)

    def load_config_from_entry(self) -> None:
        value = self.config_path_var.get().strip()
        if value:
            self.load_config(Path(value))

    def current_config(self) -> dict:
        return {
            "hue": {
                "bridge_ip": self.bridge_ip_var.get().strip(),
                "username": self.username_var.get().strip(),
                "clientkey": self.clientkey_var.get().strip(),
                "entertainment_area_id": self.area_id_var.get().strip() or None,
            },
            "artnet": {"bind": self.bind_var.get().strip() or "0.0.0.0", "port": int(self.port_var.get())},
            "stream": {
                "fps": float(self.fps_var.get()),
                "change_threshold": int(self.threshold_var.get()),
                "color_space": self.color_space_var.get(),
            },
            "mapping": self.mapping_cfg,
        }

    def load_config(self, path: Path) -> None:
        config = json.loads(path.read_text(encoding="utf-8"))
        self.config_path_var.set(str(path))
        self.bridge_ip_var.set(str(config.get("hue", {}).get("bridge_ip", "")))
        self.username_var.set(str(config.get("hue", {}).get("username", "")))
        self.clientkey_var.set(str(config.get("hue", {}).get("clientkey", "")))
        self.area_id_var.set(str(config.get("hue", {}).get("entertainment_area_id", "")))
        self.bind_var.set(str(config.get("artnet", {}).get("bind", "0.0.0.0")))
        self.port_var.set(str(config.get("artnet", {}).get("port", bridge.ARTNET_PORT)))
        self.fps_var.set(str(config.get("stream", {}).get("fps", bridge.DEFAULT_FPS)))
        self.threshold_var.set(str(config.get("stream", {}).get("change_threshold", 0)))
        self.color_space_var.set(str(config.get("stream", {}).get("color_space", "rgb")))
        self.mapping_cfg = config.get("mapping", self.mapping_cfg)
        mapping = bridge.build_mapping(self.current_config())
        self.mapping_var.set(f"{self.mapping_cfg.get('type', 'unknown')} / {len(mapping)} channels")
        self.render_channels([item.channel_id for item in mapping])
        self.append_log(f"Loaded config: {path}")

    def save_config(self) -> None:
        config = self.current_config()
        bridge.build_mapping(config)
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        Path(path).write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        self.config_path_var.set(path)
        self.append_log(f"Saved config: {path}")

    def on_pair(self) -> None:
        bridge_ip = self.bridge_ip_var.get().strip()
        if not bridge_ip:
            messagebox.showerror("Pair", "Bridge IP is required.")
            return

        def task() -> dict:
            username, clientkey = bridge.hue_pair(bridge_ip, "madrix-hue-bridge#gui", timeout=60.0)
            _bridge, _service, configs = bridge.fetch_entertainment_configs(bridge_ip, username, clientkey)
            areas = serialize_entertainment_configs(configs)
            first_id = next(iter(areas.keys()), "")
            return {"username": username, "clientkey": clientkey, "areas": areas, "area_id": first_id}

        self.status_var.set("Pairing with bridge...")
        BackgroundTask(target=task, event_queue=self.event_queue, result_type="pair_result").start()

    def on_find_bridge(self) -> None:
        def task() -> dict:
            bridges = bridge.discover_bridges()
            return {"bridges": bridges}

        self.status_var.set("Searching for Hue Bridge...")
        BackgroundTask(target=task, event_queue=self.event_queue, result_type="bridge_result").start()

    def on_list_areas(self) -> None:
        def task() -> dict:
            _bridge, _service, configs = bridge.fetch_entertainment_configs(
                self.bridge_ip_var.get().strip(),
                self.username_var.get().strip(),
                self.clientkey_var.get().strip(),
            )
            return {"areas": serialize_entertainment_configs(configs)}

        self.status_var.set("Loading areas...")
        BackgroundTask(target=task, event_queue=self.event_queue, result_type="areas_result").start()

    def on_find_lights(self) -> None:
        bridge_ip = self.bridge_ip_var.get().strip()
        username = self.username_var.get().strip()
        if not bridge_ip:
            messagebox.showerror("Find Lamps", "Bridge IP is required.")
            return
        if not username:
            messagebox.showerror("Find Lamps", "Username is required. Pair with the bridge first.")
            return

        def task() -> dict:
            clientkey = self.clientkey_var.get().strip() or None
            lights = bridge.fetch_bridge_lights(bridge_ip, username, clientkey)
            areas: dict[str, Any] = {}
            if clientkey:
                _bridge, _service, configs = bridge.fetch_entertainment_configs(
                    bridge_ip,
                    username,
                    clientkey,
                )
                areas = serialize_entertainment_configs(configs)
            selected_area_id, channel_ids = choose_best_area(areas, self.area_id_var.get().strip())
            return {
                "lights": lights,
                "areas": areas,
                "selected_area_id": selected_area_id,
                "channel_ids": channel_ids,
            }

        self.status_var.set("Searching for Hue lamps...")
        BackgroundTask(target=task, event_queue=self.event_queue, result_type="lights_result").start()

    def start_bridge(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            config = self.current_config()
            mapping = bridge.build_mapping(config)
        except Exception as exc:
            messagebox.showerror("Start Bridge", str(exc))
            return
        self.render_channels([item.channel_id for item in mapping])
        self.packets_var.set("0")
        self.updates_var.set("0")
        self.last_port_var.set("-")
        self.packet_age_var.set("-")
        self.runtime_started_at = None
        self.last_packet_monotonic = None
        self.status_var.set("Starting bridge...")
        self.worker = BridgeWorker(config, self.event_queue)
        self.worker.start()

    def stop_bridge(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self.status_var.set("Stopping bridge...")

    def render_channels(self, channel_ids: List[int]) -> None:
        self.canvas.delete("all")
        self.channel_items.clear()
        if not channel_ids:
            return
        width = max(self.canvas.winfo_width(), 720)
        columns = max(1, min(6, math.ceil(math.sqrt(len(channel_ids)))))
        tile_width = max(120, (width - 24 - (columns - 1) * 12) // columns)
        tile_height = 72
        for index, channel_id in enumerate(channel_ids):
            row = index // columns
            col = index % columns
            x1 = 12 + col * (tile_width + 12)
            y1 = 12 + row * (tile_height + 12)
            x2 = x1 + tile_width
            y2 = y1 + tile_height
            rect = self.canvas.create_rectangle(x1, y1, x2, y2, fill="#1c2730", outline="#31414d", width=2)
            text = self.canvas.create_text(x1 + 10, y1 + 10, anchor="nw", fill="#e8f1f8", font=("Segoe UI", 11, "bold"), text=f"CH {channel_id}\nRGB 0, 0, 0")
            self.channel_items[channel_id] = (rect, text)

    def update_frame(self, frame: Dict[int, Tuple[int, int, int]]) -> None:
        for channel_id, rgb in frame.items():
            if channel_id not in self.channel_items:
                continue
            rect, text = self.channel_items[channel_id]
            fill = "#%02x%02x%02x" % rgb
            self.canvas.itemconfigure(rect, fill=fill)
            self.canvas.itemconfigure(text, text=f"CH {channel_id}\nRGB {rgb[0]}, {rgb[1]}, {rgb[2]}")

    def process_events(self) -> None:
        while True:
            try:
                event = self.event_queue.get_nowait()
            except queue.Empty:
                break
            event_type = event["type"]
            if event_type == "log":
                self.append_log(event["message"])
            elif event_type == "bridge_result":
                bridges = event["bridges"]
                if not bridges:
                    self.status_var.set("Bridge not found")
                    self.append_log("Hue Bridge was not found on the local network.")
                else:
                    first_id, first_bridge = next(iter(bridges.items()))
                    self.bridge_ip_var.set(str(first_bridge.get("internalipaddress", "")))
                    self.status_var.set("Bridge found")
                    self.append_log("Discovered Hue Bridges:")
                    self.append_log(json.dumps(bridges, indent=2, ensure_ascii=False))
            elif event_type == "mapping":
                self.mapping_var.set(f"{self.mapping_cfg.get('type', 'unknown')} / {len(event['channel_ids'])} channels")
            elif event_type == "started":
                self.runtime_started_at = float(event["started_at"])
                self.area_var.set(str(event["area"]))
                self.status_var.set("Bridge running")
                self.append_log("Hue stream started.")
            elif event_type == "artnet":
                self.packets_var.set(str(event["packets_total"]))
                self.last_port_var.set(str(event["last_port"]))
                self.last_packet_monotonic = float(event["last_packet_monotonic"])
            elif event_type == "frame":
                self.updates_var.set(str(event["hue_updates_total"]))
                self.update_frame(event["frame"])
            elif event_type == "stopped":
                self.status_var.set("Bridge stopped")
                self.runtime_started_at = None
            elif event_type == "pair_result":
                self.username_var.set(str(event["username"]))
                self.clientkey_var.set(str(event["clientkey"]))
                self.area_id_var.set(str(event["area_id"]))
                first_channels = event["areas"].get(event["area_id"], {}).get("channel_ids", [])
                if first_channels:
                    self.mapping_cfg["channels"] = list(first_channels)
                    self.mapping_var.set(f"sequential_rgb / {len(first_channels)} channels")
                    self.render_channels(first_channels)
                self.append_log(json.dumps(event["areas"], indent=2, ensure_ascii=False))
                self.status_var.set("Bridge paired")
            elif event_type == "areas_result":
                self.append_log(json.dumps(event["areas"], indent=2, ensure_ascii=False))
                self.status_var.set("Areas loaded")
            elif event_type == "lights_result":
                lights = event["lights"]
                areas = event["areas"]
                selected_area_id = str(event.get("selected_area_id", ""))
                channel_ids = list(event.get("channel_ids", []))
                if selected_area_id:
                    self.area_id_var.set(selected_area_id)
                if channel_ids:
                    self.mapping_cfg["channels"] = channel_ids
                    self.mapping_var.set(f"sequential_rgb / {len(channel_ids)} channels")
                    self.render_channels(channel_ids)
                self.append_log("Discovered Hue lamps:")
                self.append_log(json.dumps(lights, indent=2, ensure_ascii=False))
                if areas:
                    self.append_log("Entertainment Areas:")
                    self.append_log(json.dumps(areas, indent=2, ensure_ascii=False))
                if channel_ids:
                    self.status_var.set(f"Lamps found: {len(lights)} / mapped channels: {len(channel_ids)}")
                else:
                    self.status_var.set(f"Lamps found: {len(lights)}")
            elif event_type == "error":
                self.status_var.set("Operation failed")
                self.append_log(event["traceback"])
                messagebox.showerror("Error", event["message"])
        self.after(100, self.process_events)

    def refresh_labels(self) -> None:
        if self.runtime_started_at is not None:
            elapsed = int(max(0, time.time() - self.runtime_started_at))
            hours, remainder = divmod(elapsed, 3600)
            minutes, seconds = divmod(remainder, 60)
            self.uptime_var.set(f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        else:
            self.uptime_var.set("-")
        if self.last_packet_monotonic is None:
            self.packet_age_var.set("-")
        else:
            self.packet_age_var.set(f"{max(0.0, time.monotonic() - self.last_packet_monotonic):0.1f}s")
        self.after(250, self.refresh_labels)

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.stop()
        logging.getLogger().removeHandler(self._log_handler)
        self.destroy()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="MADRIX Hue Bridge GUI")
    parser.add_argument("--config", help="Optional config path")
    args, unknown = parser.parse_known_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if unknown:
        LOG.warning("Ignoring unknown startup arguments: %s", unknown)
    config_path = Path(args.config).resolve() if args.config else None
    app = App(config_path)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
