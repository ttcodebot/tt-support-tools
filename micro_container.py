# SPDX-License-Identifier: Apache-2.0

"""Micro tile containers, assembled at configure time.

Micro tile projects (tiles: micro) are not placed by the mux. They are grouped four at a time
in submission order (sort_id), and every group gets a container: an ordinary 1x1 project to the
rest of the flow, generated into projects/<group>/ and not committed. Slot i of a container is
selected with uio_in[1:0] == i; empty slots hold a filler micro tile.

All micro tiles of a PDK share one interface geometry: the outline, the signal pins (DEF
template) and the power stripes (micro_librelane_config). So the container is hardened once,
around four copies of a placeholder macro ("slot") that has just that interface, and frozen in
tech/<pdk>/micro_container. A container for four real micro tiles is the frozen layout with the
placeholders swapped for the micro tile layouts: no place and route.

Antenna: the container reaches a slot pin only through a jumper drawn by the tools (via3 on the
pin, a met4 bridge, via3 down onto a met3 landing pad, which is the slot's LEF pin). Container
and micro tile metal then meet only on via3/met4, where a micro tile has nothing, so the antenna
result of an assembled container is that of the frozen container checked alone (with the
worst-case placeholder) and of each micro tile checked alone.

    python micro_container.py placeholder <pdk> <output dir>
        Writes the placeholder macro the generic container is hardened with:
        tt_micro_tile_slot.lef / .gds / .v

    python micro_container.py freeze <pdk>
        Updates the checksums in tech/<pdk>/micro_container/manifest.json, after the frozen
        views (the generic container and the filler micro tile) were replaced.

To change the generic container: write the placeholder, harden src/ in that directory as an
ordinary 1x1 project with macros/ holding the placeholder, check it alone (DRC, LVS, and the
antenna check with Magic: the placeholder is the worst case for every micro tile), copy its
.oas, .lef, .v and pdk.json here, update the provenance in manifest.json and run freeze.
"""

import glob
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple, TypedDict, cast

import klayout.db as pya
import yaml

from tech import TechName, tech_map

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))

SLOT = "tt_micro_tile_slot"
GENERIC = "tt_um_micro_container"
FILLER = "tt_um_micro_tile_filler"
GROUP_PREFIX = "tt_um_micro_tiles_group_"
GROUP_SIZE = 4
SELECT_BITS = 2
# commit_id.json key of a generated container
GROUP_MARKER = "micro_tile_group"
LAYOUT_EXTENSIONS = ("oas", "lef", "v")

Box = Tuple[float, float, float, float]
GdsLayer = Tuple[int, int]
POWER_NETS = ("VPWR", "VGND")
# VDPWR, the power port of the other Tiny Tapeout projects, is VPWR in a micro tile
POWER_PIN_ALIASES = {"VDPWR": "VPWR"}


@dataclass(frozen=True)
class MicroContainerTech:
    site: Tuple[float, float]
    # Core margins of a micro tile: left/right in sites, top/bottom in rows
    margin_sites: Tuple[int, int]
    # GDS layer/datatype of the drawing of every layer named below
    gds: Dict[str, GdsLayer]
    # GDS layer/datatype of pin and label, per layer the container connects to
    pin_label_gds: Dict[str, Tuple[GdsLayer, GdsLayer]]
    # Routing layers of a micro tile, bottom up; the last one carries the pins and power
    routing_layers: Tuple[str, ...]
    # Layers a micro tile must not draw on: the container's, from the pin access via up
    forbidden_layers: Tuple[str, ...]
    # Gate area the placeholder declares on every input: the smallest gate of the library
    input_gate_area: float
    # Met3 enclosure of the via3 that lands on a pin, and its gap to the micro tile interior
    pad: Tuple[float, float]
    pad_gap: float
    # Free pin-layer metal above and below a pin, below the pin layer: no via from below
    pin_clearance: float
    # Offset of the jumper's landing pad from its pin, and free pin-layer metal around it
    jump: float
    landing_clearance: float
    via_size: float
    bridge_half_width: float
    # Width of the pin strip at the pin edge: nothing but the pins may be in it
    strip_width: float
    # Wire ends may stick out of a pin only this close to the strip's inner edge
    strip_tolerance: float
    # Locations and orientations of the slots in the container, in select order
    slots: Tuple[Tuple[Tuple[float, float], str], ...]
    stdcell_spice: str
    stdcell_prefix: str


MICRO_CONTAINER_TECH: Dict[TechName, MicroContainerTech] = {
    "sky130A": MicroContainerTech(
        site=(0.46, 2.72),
        margin_sites=(6, 1),
        gds={
            "li1": (67, 20),
            "met1": (68, 20),
            "met2": (69, 20),
            "met3": (70, 20),
            "via3": (70, 44),
            "met4": (71, 20),
            "via4": (71, 44),
            "met5": (72, 20),
        },
        pin_label_gds={
            "met3": ((70, 16), (70, 5)),
            "met4": ((71, 16), (71, 5)),
        },
        routing_layers=("li1", "met1", "met2", "met3"),
        forbidden_layers=("via3", "met4", "via4", "met5"),
        input_gate_area=0.126,
        pad=(0.32, 0.38),
        pad_gap=0.35,
        pin_clearance=0.45,
        jump=1.36,
        landing_clearance=0.6,
        via_size=0.2,
        bridge_half_width=0.165,
        strip_width=1.0,
        strip_tolerance=0.05,
        slots=(
            ((0.0, 0.0), "N"),
            ((90.62, 0.0), "FN"),
            ((0.0, 51.68), "N"),
            ((90.62, 51.68), "FN"),
        ),
        stdcell_spice="libs.ref/sky130_fd_sc_hd/spice/*.spice",
        stdcell_prefix="sky130_fd_sc_hd__",
    ),
}


class MicroTileError(Exception):
    pass


def frozen_dir(pdk: TechName) -> str:
    return os.path.join(SCRIPT_DIR, "tech", pdk, "micro_container")


def get_tech(pdk: TechName) -> MicroContainerTech:
    if pdk not in MICRO_CONTAINER_TECH or not os.path.isdir(frozen_dir(pdk)):
        raise MicroTileError(f"micro tile containers are not available for {pdk}")
    return MICRO_CONTAINER_TECH[pdk]


# --- Grouping ---------------------------------------------------------------------------


def group_name(index: int) -> str:
    """Macro name of the container of group `index` (0-based)."""
    return f"{GROUP_PREFIX}{index + 1}"


def make_groups(micro_projects: Sequence) -> List[List]:
    """Groups micro tile projects four at a time in submission order (sort_id). Slot i of a
    group is selected with uio_in == i; the slots after the last group's micro tiles hold
    fillers.

    Withdrawing a micro tile moves every micro tile after it by one slot."""
    ordered = sorted(micro_projects, key=lambda p: p.sort_id)
    return [
        ordered[start : start + GROUP_SIZE]
        for start in range(0, len(ordered), GROUP_SIZE)
    ]


GROUP_DOC = """## How it works

Micro tile container: {count} micro tile projects in a single Tiny Tapeout tile.

Each micro tile is a separately hardened macro with a reduced interface: `clk`, `rst_n`, `ui_in[7:0]` and `uo_out[7:0]`. `uio_in[1:0]` selects one of them: only the selected micro tile gets `clk`, `rst_n` and `ui_in`, and drives `uo_out`. The others get no clock and are held in reset.

| `uio_in[1:0]` | Micro tile | Author | Macro |
| ------------- | ---------- | ------ | ----- |
{rows}

## How to test

Select a micro tile with `uio_in[1:0]`, then follow the instructions of that micro tile.

## External hardware

None
"""


def write_group_project(
    project_dir: str,
    index: int,
    members: Sequence,
    pdk: TechName,
    repo_url: str,
    commit: str,
) -> str:
    """Writes the project directory of a container: info.yaml, docs/info.md, commit_id.json,
    pdk.json and a .gitignore that ignores the whole directory. The layout, LEF and netlist are
    written by assemble(); they are removed here when the group's micro tiles have changed.
    Returns the directory."""
    name = group_name(index)
    group_dir = os.path.join(project_dir, name)
    os.makedirs(os.path.join(group_dir, "docs"), exist_ok=True)
    micro_tiles = [
        {"macro": m.info.top_module, "repo": m.git_url, "commit": m.commit_id}
        for m in members
    ]

    commit_id_file = os.path.join(group_dir, "commit_id.json")
    if os.path.exists(commit_id_file):
        with open(commit_id_file) as f:
            assembled_from = json.load(f).get("micro_tiles")
        if assembled_from != micro_tiles:
            for ext in LAYOUT_EXTENSIONS:
                for path in glob.glob(os.path.join(group_dir, f"*.{ext}")):
                    os.remove(path)

    with open(os.path.join(group_dir, ".gitignore"), "w") as f:
        f.write("# Generated by tt-support-tools (micro_container.py)\n*\n")

    rows = [
        f"| {slot} | {m.info.title} | {m.info.author} | `{m.info.top_module}` |"
        for slot, m in enumerate(members)
    ]
    rows += [
        f"| {slot} | (empty: outputs low) | | `{FILLER}` |"
        for slot in range(len(members), GROUP_SIZE)
    ]
    with open(os.path.join(group_dir, "docs", "info.md"), "w") as f:
        f.write(GROUP_DOC.format(count=len(members), rows="\n".join(rows)))

    pinout = {f"ui[{i}]": f"micro tile ui_in[{i}]" for i in range(8)}
    pinout.update({f"uo[{i}]": f"micro tile uo_out[{i}]" for i in range(8)})
    pinout.update({f"uio[{i}]": f"select[{i}]" if i < 2 else "" for i in range(8)})
    info = {
        "project": {
            "title": f"Micro tiles group {index + 1}",
            "author": "Tiny Tapeout",
            "discord": "",
            "description": f"{len(members)} micro tile projects in a single Tiny Tapeout tile",
            "language": "Verilog",
            "clock_hz": 0,
            "tiles": "1x1",
            "top_module": name,
            "source_files": [f"{name}.v"],
        },
        "pinout": pinout,
        "yaml_version": 6,
    }
    with open(os.path.join(group_dir, "info.yaml"), "w") as f:
        yaml.safe_dump(info, f, sort_keys=False)

    with open(commit_id_file, "w") as f:
        json.dump(
            {
                "app": "tt-support-tools micro tile container",
                "repo": repo_url,
                "commit": commit,
                "sort_id": members[0].sort_id,
                GROUP_MARKER: True,
                "micro_tiles": micro_tiles,
            },
            f,
            indent=2,
        )
        f.write("\n")

    shutil.copy(
        os.path.join(frozen_dir(pdk), "pdk.json"), os.path.join(group_dir, "pdk.json")
    )
    return group_dir


def is_group_dir(path: str) -> bool:
    commit_id_file = os.path.join(path, "commit_id.json")
    if not os.path.exists(commit_id_file):
        return False
    with open(commit_id_file) as f:
        return bool(json.load(f).get(GROUP_MARKER, False))


def remove_stale_groups(project_dir: str, keep: Sequence[str]):
    """Removes generated container directories that are not in `keep`."""
    for entry in sorted(os.listdir(project_dir)):
        path = os.path.join(project_dir, entry)
        if entry not in keep and os.path.isdir(path) and is_group_dir(path):
            logging.info(f"removing stale micro tile container {entry}")
            shutil.rmtree(path)


# --- Interface ----------------------------------------------------------------------------


def micro_tile_interface(pdk: TechName):
    """Outline, signal pins and power stripes of a micro tile, from the tools' own data."""
    t = MICRO_CONTAINER_TECH[pdk]
    with open(os.path.join(SCRIPT_DIR, f"tech/{pdk}/tile_sizes.yaml")) as f:
        _, _, width, height = map(float, yaml.safe_load(f)["micro"].split())
    with open(os.path.join(SCRIPT_DIR, f"tech/{pdk}/def/tt_block_micro_pg.def")) as f:
        template = f.read()
    pins: List[Tuple[str, str, str, Box]] = []
    for name, direction, layer, x1, y1, x2, y2, ox, oy in re.findall(
        r"- (\S+) \+ NET \S+ \+ DIRECTION (\w+) \+ USE SIGNAL\s+\+ PORT\s+"
        r"\+ LAYER (\w+) \( (-?\d+) (-?\d+) \) \( (-?\d+) (-?\d+) \)\s+\+ PLACED \( (\d+) (\d+) \)",
        template,
    ):
        x_1, y_1, x_2, y_2 = (
            (int(o) + int(d)) / 1000
            for o, d in ((ox, x1), (oy, y1), (ox, x2), (oy, y2))
        )
        pins.append((name, direction, layer, (x_1, y_1, x_2, y_2)))
    assert (
        len(pins) == 18
    ), f"expected 18 signal pins in the micro tile template of {pdk}"

    pdn = tech_map[pdk].micro_librelane_config
    core = (
        t.margin_sites[0] * t.site[0],
        t.margin_sites[1] * t.site[1],
        width - t.margin_sites[0] * t.site[0],
        height - t.margin_sites[1] * t.site[1],
    )
    layer = str(pdn["PDN_HORIZONTAL_LAYER"])
    w, pitch, offset, spacing = (
        float(pdn[k])
        for k in ("PDN_HWIDTH", "PDN_HPITCH", "PDN_HOFFSET", "PDN_HSPACING")
    )
    stripes: Dict[str, List[Box]] = {"VPWR": [], "VGND": []}
    y = core[1] + offset
    while y + w / 2 <= core[3]:
        stripes["VPWR"].append(
            (core[0], round(y - w / 2, 3), core[2], round(y + w / 2, 3))
        )
        yg = y + w + spacing
        if yg + w / 2 <= core[3]:
            stripes["VGND"].append(
                (core[0], round(yg - w / 2, 3), core[2], round(yg + w / 2, 3))
            )
        y += pitch
    return width, height, pins, layer, stripes


def template_interface(pdk: TechName):
    """What lef_interface() must return for every micro tile LEF."""
    width, height, pins, power_layer, stripes = micro_tile_interface(pdk)
    result = [
        (name, layer, tuple(round(v, 3) for v in r)) for name, _, layer, r in pins
    ]
    result += [
        (net, power_layer, tuple(round(v, 3) for v in s))
        for net in stripes
        for s in stripes[net]
    ]
    return (width, height), sorted(result)


def lef_interface(lef_text: str, pdk: TechName):
    """Pin geometry of a LEF macro on the layers a container connects to."""
    t = MICRO_CONTAINER_TECH[pdk]
    result = []
    for name, body in re.findall(r"  PIN (\S+)\n(.*?)  END \1\n", lef_text, re.S):
        for layer, r in re.findall(r"LAYER (\S+) ;\s+RECT ([\d. -]+) ;", body):
            if layer in t.pin_label_gds:
                result.append(
                    (name, layer, tuple(round(float(v), 3) for v in r.split()))
                )
    size = re.search(r"SIZE ([\d.]+) BY ([\d.]+)", lef_text)
    assert size is not None, "no SIZE in LEF"
    return (float(size.group(1)), float(size.group(2))), sorted(result)


def pin_stack(pdk: TechName, r: Box) -> Tuple[Box, List[Tuple[str, Box]]]:
    """The jumper the tools draw on every pin, all of it container metal:
    - a via3 on the pin, with its met3 pad `pad_gap` away from the micro tile's interior,
    - a met4 bridge going up (met4's direction) by `jump`, into the free strip between pins,
    - a via3 down onto a met3 landing pad that runs to the slot edge: the slot's LEF pin.
    Returns the landing pad and a list of (layer, box)."""
    t = MICRO_CONTAINER_TECH[pdk]
    x1, y1, x2, y2 = r
    cy = (y1 + y2) / 2
    by = cy + t.jump
    cx = x1 + t.pad_gap + t.pad[0] / 2

    def box(a: float, b: float, c: float, d: float) -> Box:
        return (round(a, 3), round(b, 3), round(c, 3), round(d, 3))

    v = t.via_size / 2
    h = t.bridge_half_width
    landing = box(cx - t.pad[0] / 2, by - t.pad[1] / 2, x2, by + t.pad[1] / 2)
    shapes = [
        ("via3", box(cx - v, cy - v, cx + v, cy + v)),
        (
            "met3",
            box(
                cx - t.pad[0] / 2,
                cy - t.pad[1] / 2,
                cx + t.pad[0] / 2,
                cy + t.pad[1] / 2,
            ),
        ),
        ("met4", box(cx - h, cy - h, cx + h, by + h)),
        ("via3", box(cx - v, by - v, cx + v, by + v)),
        ("met3", landing),
    ]
    return landing, shapes


def insert_pin_stacks(pdk: TechName, layout: pya.Layout, cell: pya.Cell, trans):
    t = MICRO_CONTAINER_TECH[pdk]
    for _, _, _, r in micro_tile_interface(pdk)[2]:
        for layer, box in pin_stack(pdk, r)[1]:
            cell.shapes(layout.layer(*t.gds[layer])).insert(
                pya.DBox(*box).transformed(trans)
            )


def write_placeholder(pdk: TechName, out_dir: str):
    """Writes the slot macro (LEF, GDS, Verilog) the generic container is hardened with."""
    t = MICRO_CONTAINER_TECH[pdk]
    width, height, pins, power_layer, stripes = micro_tile_interface(pdk)
    os.makedirs(out_dir, exist_ok=True)

    def rect(r) -> str:
        return "RECT " + " ".join(f"{v:.3f}" for v in r) + " ;"

    lef = [
        "VERSION 5.7 ;",
        "  NOWIREEXTENSIONATPIN ON ;",
        '  DIVIDERCHAR "/" ;',
        '  BUSBITCHARS "[]" ;',
        f"MACRO {SLOT}",
        "  CLASS BLOCK ;",
        f"  FOREIGN {SLOT} ;",
        "  ORIGIN 0.000 0.000 ;",
        f"  SIZE {width:.3f} BY {height:.3f} ;",
    ]
    for net, use in (("VGND", "GROUND"), ("VPWR", "POWER")):
        lef += [f"  PIN {net}", "    DIRECTION INOUT ;", f"    USE {use} ;"]
        for stripe in stripes[net]:
            lef += [
                "    PORT",
                f"      LAYER {power_layer} ;",
                f"        {rect(stripe)}",
                "    END",
            ]
        lef += [f"  END {net}"]
    for name, direction, layer, r in pins:
        lef += [f"  PIN {name}", f"    DIRECTION {direction} ;", "    USE SIGNAL ;"]
        if direction == "INPUT":
            lef += [f"    ANTENNAGATEAREA {t.input_gate_area:.6f} ;"]
        lef += [
            "    PORT",
            f"      LAYER {layer} ;",
            f"        {rect(pin_stack(pdk, r)[0])}",
            "    END",
            f"  END {name}",
        ]
    lef += ["  OBS"]
    dbu = 0.001
    outline = pya.Region(pya.DBox(0, 0, width, height).to_itype(dbu))
    # The micro tile's interior is obstructed on all its layers. In the pin strip only the
    # jumpers' landing pads (the router's pins) are open on the pin layer, with free metal
    # around them; the layer below is obstructed around the pins, so that no via can land on
    # a pin from below.
    strip_x = min(p[3][0] for p in pins)
    inner = pya.Region(pya.DBox(0, 0, strip_x, height).to_itype(dbu))
    pin_layer = pins[0][2]
    below_pin = t.routing_layers[t.routing_layers.index(pin_layer) - 1]
    pin_rows = pya.Region()
    pin_keep = pya.Region()
    for p in pins:
        x1, y1, x2, y2 = p[3]
        pin_rows.insert(
            pya.DBox(strip_x, y1 - t.pin_clearance, x2, y2 + t.pin_clearance).to_itype(
                dbu
            )
        )
        lx1, ly1, lx2, ly2 = pin_stack(pdk, p[3])[0]
        pin_keep.insert(
            pya.DBox(
                strip_x, ly1 - t.landing_clearance, lx2, ly2 + t.landing_clearance
            ).to_itype(dbu)
        )
    for layer in t.routing_layers:
        obs = inner
        if layer == below_pin:
            obs = inner + pin_rows
        if layer == power_layer or any(p[2] == layer for p in pins):
            keep = pin_keep.dup()
            for r in [s for net in stripes.values() for s in net]:
                keep.insert(pya.DBox(*r).to_itype(dbu))
            obs = outline - keep
        lef += [f"      LAYER {layer} ;"]
        for trap in obs.decompose_trapezoids_to_region().each():
            b = trap.bbox().to_dtype(dbu)
            lef += [f"        {rect((b.left, b.bottom, b.right, b.top))}"]
    # The jumpers' bridges and cuts above the pin layer, so that the router keeps its spacing
    for stack_layer in ("via3", "met4"):
        lef += [f"      LAYER {stack_layer} ;"]
        for p in pins:
            for shape_layer, stack_box in pin_stack(pdk, p[3])[1]:
                if shape_layer == stack_layer:
                    lef += [f"        {rect(stack_box)}"]
    lef += ["  END", f"END {SLOT}", "END LIBRARY", ""]
    with open(os.path.join(out_dir, f"{SLOT}.lef"), "w") as f:
        f.write("\n".join(lef))

    # GDS: outline, pins, power stripes and the jumpers
    layout = pya.Layout()
    layout.dbu = dbu
    cell = layout.create_cell(SLOT)
    cell.shapes(layout.layer(*tech_map[pdk].prboundary_layer)).insert(
        pya.DBox(0, 0, width, height)
    )
    shapes = [(name, layer, r) for name, _, layer, r in pins]
    shapes += [(net, power_layer, s) for net in stripes for s in stripes[net]]
    for name, layer, r in shapes:
        pin, label = t.pin_label_gds[layer]
        pin_box = pya.DBox(*r)
        cell.shapes(layout.layer(*t.gds[layer])).insert(pin_box)
        cell.shapes(layout.layer(*pin)).insert(pin_box)
        cell.shapes(layout.layer(*label)).insert(
            pya.DText(name, pin_box.center().x, pin_box.center().y)
        )
    insert_pin_stacks(pdk, layout, cell, pya.DTrans())
    layout.write(os.path.join(out_dir, f"{SLOT}.gds"))

    with open(os.path.join(out_dir, f"{SLOT}.v"), "w") as f:
        f.write(
            f"""// Placeholder for a micro tile: black box with the micro tile ports
(* blackbox *)
module {SLOT} (
    inout  wire       VPWR,
    inout  wire       VGND,
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out
);
endmodule
"""
        )


# --- Micro tile checks ----------------------------------------------------------------


def micro_tile_layout_path(micro_dir: str, macro: str) -> str:
    for ext in ("oas", "gds"):
        path = os.path.join(micro_dir, f"{macro}.{ext}")
        if os.path.exists(path):
            return path
    raise MicroTileError(f"{macro}: no {macro}.oas or {macro}.gds in {micro_dir}")


def check_micro_tile(pdk: TechName, micro_dir: str, macro: str) -> List[str]:
    """Checks that a micro tile can be dropped into the generic container. Returns a list of
    problems (empty if none):
    - pins and power stripes at the template positions (LEF),
    - every input that reaches a gate has at least the gate area the placeholder declares,
    - nothing on the container's layers (pin access via and above),
    - nothing but the pins in the pin strip on the pin layer, where the tools draw the
      jumpers. The frozen container has no metal of its own over a slot on the layers below
      (test_micro_container.py), so the micro tile may use them up to its edge,
    - every LEF power port is drawn on the pin layer, on metal labeled with its own net and
      no other power net. LVS of the container does not see a VGND port on metal that is
      not VGND: Magic joins the grounds through the substrate."""
    t = MICRO_CONTAINER_TECH[pdk]
    errors = []
    with open(os.path.join(micro_dir, f"{macro}.lef")) as f:
        lef_text = f.read()
    if lef_interface(lef_text, pdk) != template_interface(pdk):
        errors.append(
            "pins or power stripes are not at the micro tile template positions"
        )
    macro_lef = re.search(
        rf"MACRO {re.escape(macro)}\n(.*?)END {re.escape(macro)}\n", lef_text, re.S
    )
    if macro_lef is None:
        return errors + [f"no macro {macro} in its LEF"]
    pin_layer = t.routing_layers[-1]
    pins = pya.Region()
    power_ports: Dict[str, List[Box]] = {net: [] for net in POWER_NETS}
    for name, pin in re.findall(
        r"  PIN (\S+)\n(.*?)  END \1\n", macro_lef.group(1), re.S
    ):
        rects = [
            cast(Box, tuple(float(v) for v in r.split()))
            for layer, r in re.findall(r"LAYER (\S+) ;\s+RECT ([\d.\s-]+?) ;", pin)
            if layer == pin_layer
        ]
        if "USE SIGNAL" not in pin:
            net = POWER_PIN_ALIASES.get(name, name)
            if net in power_ports:
                power_ports[net] += rects
            continue
        for r in rects:
            pins.insert(pya.DBox(*r).to_itype(0.001))
        gate = sum(float(v) for v in re.findall(r"ANTENNAGATEAREA ([\d.]+)", pin))
        if "DIRECTION INPUT" in pin and 0 < gate < t.input_gate_area - 1e-6:
            errors.append(
                f"input {name} drives {gate} um2 of gate, less than {t.input_gate_area}"
            )

    layout = pya.Layout()
    layout.read(micro_tile_layout_path(micro_dir, macro))
    top = layout.cell(macro)
    if top is None:
        return errors + [f"no cell {macro} in its layout"]
    if abs(layout.dbu - 0.001) > 1e-9:
        return errors + [f"database unit {layout.dbu}, expected 0.001"]
    for name in t.forbidden_layers:
        index = layout.find_layer(*t.gds[name])
        if index is not None and not top.bbox_per_layer(index).empty():
            errors.append(f"shapes on {name}, which belongs to the container")
    width, height = template_interface(pdk)[0]
    strip = pya.Region(
        pya.DBox(width - t.strip_width + t.strip_tolerance, 0, width, height).to_itype(
            layout.dbu
        )
    )
    index = layout.find_layer(*t.gds[pin_layer])
    metal = (
        pya.Region(top.begin_shapes_rec(index)).merged()
        if index is not None
        else pya.Region()
    )
    region = (metal & strip) - pins
    area = region.area() * layout.dbu**2
    if area > 1e-6:
        b = region.bbox().to_dtype(layout.dbu)
        errors.append(
            f"{area:.3f} um2 of {pin_layer} in the pin strip (x > {width - t.strip_width:.2f}) apart from the pins, around {b}"
        )
    # The container's power vias land on the LEF power ports
    labels: Dict[str, pya.Region] = {net: pya.Region() for net in POWER_NETS}
    for gds_layer in t.pin_label_gds[pin_layer]:
        index = layout.find_layer(*gds_layer)
        if index is None:
            continue
        for shape in top.shapes(index).each(pya.Shapes.STexts):
            net = POWER_PIN_ALIASES.get(shape.text_string, shape.text_string)
            if net in labels:
                p = pya.Point() + shape.text_pos
                labels[net].insert(pya.Box(p, p).enlarged(1, 1))
    for net, ports in power_ports.items():
        for s in ports:
            port = pya.Region(pya.DBox(*s).to_itype(layout.dbu))
            if not (port - metal).is_empty():
                errors.append(
                    f"{net} port {s} on {pin_layer} in the LEF is not all {pin_layer} in the layout"
                )
                continue
            under = metal.interacting(port)
            nets = sorted(
                n for n in POWER_NETS if not under.interacting(labels[n]).is_empty()
            )
            if nets != [net]:
                errors.append(
                    f"{net} port {s} on {pin_layer} in the LEF is on {pin_layer} labeled "
                    f"{', '.join(nets) or 'with no power net'} in the layout"
                )
    return errors


# --- Assembly -------------------------------------------------------------------------


def cell_signature(layout: pya.Layout, cell: pya.Cell):
    """Geometry and instances of a cell, to tell whether two cells are the same. Labels are
    left out: Magic versions differ in the labels they write for the same standard cell.
    """
    sig: List[tuple] = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        polygons = sorted(
            str(p) for p in pya.Region(cell.shapes(layer_index)).merged().each()
        )
        if polygons:
            sig.append((info.layer, info.datatype, tuple(polygons)))
    sig.append(
        tuple(
            sorted(
                f"{layout.cell(i.cell_index).name} {i.trans}" for i in cell.each_inst()
            )
        )
    )
    return tuple(sorted(sig, key=str))


def read_layout_into(
    layout: pya.Layout, path: str, macro: str, stdcell_prefix: str
) -> Tuple[pya.Cell, Dict[str, int]]:
    """Reads a micro tile layout into `layout`. Cells that already exist with the same content
    (the standard cells) are shared, other cells that exist with different content get a
    unique name. A standard cell that differs from the one already there is an error."""
    stats = {"shared": 0, "renamed": 0, "new": 0}
    src = pya.Layout()
    src.read(path)
    src_top = src.cell(macro)
    if src_top is None:
        raise MicroTileError(f"{path} has no cell {macro}")
    if layout.cell(macro) is not None:
        raise MicroTileError(f"cell {macro} is already in the container")
    assert abs(layout.dbu - src.dbu) < 1e-9
    mapping: Dict[int, int] = {}
    called = set(src_top.called_cells())
    for index in src.each_cell_bottom_up():
        if index != src_top.cell_index() and index not in called:
            continue
        cell = src.cell(index)
        existing = layout.cell(cell.name)
        target_name = cell.name
        if existing is not None:
            if cell_signature(src, cell) == cell_signature(layout, existing):
                mapping[index] = existing.cell_index()
                stats["shared"] += 1
                continue
            if cell.name.startswith(stdcell_prefix):
                raise MicroTileError(
                    f"{macro}: standard cell {cell.name} differs from the container's"
                )
            target_name = f"{cell.name}__{macro}"
            if layout.cell(target_name) is not None:
                raise MicroTileError(f"{macro}: cell name {target_name} is taken")
            stats["renamed"] += 1
        else:
            stats["new"] += 1
        new_cell = layout.create_cell(target_name)
        mapping[index] = new_cell.cell_index()
        for layer_index in src.layer_indexes():
            info = src.get_info(layer_index)
            target_layer = layout.layer(info.layer, info.datatype)
            new_cell.shapes(target_layer).insert(cell.shapes(layer_index))
        for inst in cell.each_inst():
            new_cell.insert(pya.CellInstArray(mapping[inst.cell_index], inst.trans))
    return layout.cell(mapping[src_top.cell_index()]), stats


def frozen_manifest(pdk: TechName) -> dict:
    with open(os.path.join(frozen_dir(pdk), "manifest.json")) as f:
        return json.load(f)


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


FROZEN_FILES = [
    f"{GENERIC}.oas",
    f"{GENERIC}.lef",
    f"{GENERIC}.v",
    f"{FILLER}.oas",
    f"{FILLER}.lef",
    f"{FILLER}.v",
    "pdk.json",
]


def freeze(pdk: TechName):
    path = os.path.join(frozen_dir(pdk), "manifest.json")
    manifest = frozen_manifest(pdk)
    manifest["sha256"] = {
        name: sha256(os.path.join(frozen_dir(pdk), name)) for name in FROZEN_FILES
    }
    with open(path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")


def verify_frozen(pdk: TechName):
    """The frozen views must be the ones the manifest lists."""
    manifest = frozen_manifest(pdk)
    for name, digest in manifest["sha256"].items():
        path = os.path.join(frozen_dir(pdk), name)
        if sha256(path) != digest:
            raise MicroTileError(f"{path} does not match its checksum in manifest.json")


def assemble(
    pdk: TechName, top: str, out_dir: str, micro_tiles: Sequence[Tuple[str, str]]
):
    """Makes the container `top` in `out_dir` (top.oas, top.lef, top.v) from the frozen
    generic container. micro_tiles: (directory, macro) per slot in select order; the slots
    after them get the filler."""
    t = get_tech(pdk)
    assert len(micro_tiles) <= len(t.slots)
    verify_frozen(pdk)
    frozen = frozen_dir(pdk)
    slots = list(micro_tiles) + [(frozen, FILLER)] * (len(t.slots) - len(micro_tiles))

    layout = pya.Layout()
    layout.read(os.path.join(frozen, f"{GENERIC}.oas"))
    top_cell = layout.cell(GENERIC)
    slot_cell = layout.cell(SLOT)
    assert top_cell is not None and slot_cell is not None
    dbu = layout.dbu

    by_slot = {}
    for inst in top_cell.each_inst():
        if inst.cell_index != slot_cell.cell_index():
            continue
        origin = (round(inst.bbox().left * dbu, 3), round(inst.bbox().bottom * dbu, 3))
        index = next(i for i, (location, _) in enumerate(t.slots) if location == origin)
        assert inst.trans.is_mirror() == (t.slots[index][1] == "FN")
        by_slot[index] = inst
    assert len(by_slot) == len(
        t.slots
    ), "the generic container does not have four slots"

    for index, (micro_dir, macro) in enumerate(slots):
        # The filler is read once and shared by its slots
        cell = layout.cell(macro) if macro == FILLER else None
        if cell is None:
            cell, stats = read_layout_into(
                layout,
                micro_tile_layout_path(micro_dir, macro),
                macro,
                t.stdcell_prefix,
            )
            logging.info(
                f"  slot {index}: {macro} ({stats['new']} new cells, {stats['shared']} shared, {stats['renamed']} renamed)"
            )
        else:
            logging.info(f"  slot {index}: {macro}")
        # The jumpers on the pins belong to the container: keep them when the slot goes
        insert_pin_stacks(pdk, layout, top_cell, by_slot[index].dcplx_trans)
        by_slot[index].cell_index = cell.cell_index()
    layout.delete_cell(slot_cell.cell_index())
    top_cell.name = top
    os.makedirs(out_dir, exist_ok=True)
    layout.write(os.path.join(out_dir, f"{top}.oas"))

    with open(os.path.join(frozen, f"{GENERIC}.lef")) as f:
        lef = f.read()
    with open(os.path.join(out_dir, f"{top}.lef"), "w") as f:
        f.write(re.sub(rf"\b{GENERIC}\b", top, lef))

    with open(os.path.join(frozen, f"{GENERIC}.v")) as f:
        netlist = f.read()
    netlist, count = re.subn(rf"\bmodule {GENERIC}\b", f"module {top}", netlist)
    assert count == 1
    for index, (_, macro) in enumerate(slots):
        netlist, count = re.subn(
            rf"\b{SLOT}(\s+)proj{index + 1}\b", rf"{macro}\1proj{index + 1}", netlist
        )
        assert count == 1, f"instance proj{index + 1} not found in the generic netlist"
    assert SLOT not in netlist
    with open(os.path.join(out_dir, f"{top}.v"), "w") as f:
        f.write(netlist)
    if len(micro_tiles) < len(t.slots):
        shutil.copy(
            os.path.join(frozen, f"{FILLER}.v"), os.path.join(out_dir, f"{FILLER}.v")
        )


# --- Signoff checks of an assembled container ------------------------------------------

MAGIC_SCRIPT = """crashbackups disable
locking disable
gds read {gds}
foreach f [lsort [glob {stdcell_spice}]] {{ readspice $f }}
load {top} -dereference
select top cell
drc style drc(full)
drc euclidean on
drc check
set count 0
foreach {{errtype coordlist}} [drc listall why] {{
    puts "DRC: $errtype ([llength $coordlist])"
    incr count [llength $coordlist]
}}
puts "DRC errors: $count"
drc off
cd {out_dir}
extract do local
extract no capacitance
extract no coupling
extract no resistance
extract no adjust
extract unique notopports
extract
ext2spice lvs
ext2spice -o {out_dir}/{top}.spice {top}.ext
antennacheck debug
antennacheck
puts "Antenna check done"
exit 0
"""


class ContainerCheckResult(TypedDict):
    drc_errors: int
    antenna_violations: int
    lvs_match: bool


def check_container(
    pdk: TechName, container_dir: str, top: str, netlists: Sequence[str], out_dir: str
) -> ContainerCheckResult:
    """Magic DRC (full rule deck), Magic antenna check and Netgen LVS of an assembled
    container, from its layout with the micro tiles in it, against the container netlist and
    the micro tile netlists. Needs magic and netgen (or $MAGIC and $NETGEN) and $PDK_ROOT.
    """
    t = get_tech(pdk)
    pdk_dir = os.path.join(os.environ["PDK_ROOT"], pdk)
    magic = os.environ.get("MAGIC", "magic")
    netgen = os.environ.get("NETGEN", "netgen")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    gds = os.path.join(out_dir, f"{top}.gds")
    layout = pya.Layout()
    layout.read(os.path.join(container_dir, f"{top}.oas"))
    layout.write(gds)

    script = os.path.join(out_dir, "check.tcl")
    with open(script, "w") as f:
        f.write(
            MAGIC_SCRIPT.format(
                gds=gds,
                stdcell_spice=os.path.join(pdk_dir, t.stdcell_spice),
                top=top,
                out_dir=out_dir,
            )
        )
    with open(os.path.join(out_dir, "magic.log"), "w") as log:
        subprocess.run(
            [
                magic,
                "-dnull",
                "-noconsole",
                "-rcfile",
                os.path.join(pdk_dir, f"libs.tech/magic/{pdk}.magicrc"),
                script,
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=out_dir,
            check=True,
        )
    with open(os.path.join(out_dir, "magic.log")) as f:
        magic_log = f.read()
    drc = re.search(r"^DRC errors: (\d+)", magic_log, re.M)
    if drc is None or "Antenna check done" not in magic_log:
        raise MicroTileError(f"{top}: Magic did not finish, see {out_dir}/magic.log")
    antenna = len(re.findall(r"Antenna violation detected", magic_log))

    lvs_script = os.path.join(out_dir, "lvs.tcl")
    with open(lvs_script, "w") as f:
        f.write(f"set circuit1 [readnet spice {out_dir}/{top}.spice]\n")
        f.write("set circuit2 [readnet verilog /dev/null]\n")
        for spice in sorted(glob.glob(os.path.join(pdk_dir, t.stdcell_spice))):
            f.write(f"readnet spice {spice} $circuit2\n")
        for netlist in netlists:
            f.write(f"readnet verilog {os.path.realpath(netlist)} $circuit2\n")
        f.write(
            f'lvs "$circuit1 {top}" "$circuit2 {top}" '
            f"{pdk_dir}/libs.tech/netgen/{pdk}_setup.tcl {out_dir}/lvs.rpt -blackbox -json\n"
        )
    with open(os.path.join(out_dir, "netgen.log"), "w") as log:
        subprocess.run(
            [netgen, "-batch", "source", lvs_script],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=out_dir,
            check=True,
        )
    with open(os.path.join(out_dir, "lvs.rpt")) as f:
        lvs_report = f.read()
    lvs_match = "Final result: Circuits match uniquely." in lvs_report
    result: ContainerCheckResult = {
        "drc_errors": int(drc.group(1)),
        "antenna_violations": antenna,
        "lvs_match": lvs_match,
    }
    with open(os.path.join(out_dir, "result.json"), "w") as f:
        json.dump(result, f, indent=2)
    return result


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "placeholder":
        write_placeholder(cast(TechName, sys.argv[2]), sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] == "freeze":
        freeze(cast(TechName, sys.argv[2]))
    else:
        print(__doc__)
        sys.exit(1)
