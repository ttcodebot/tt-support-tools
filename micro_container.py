# SPDX-License-Identifier: Apache-2.0

"""Micro tile containers, assembled without place and route (prototype, sky130A only).

A micro tile container is an ordinary 1x1 project that holds four micro tiles and muxes
between them. All micro tiles of a PDK have the same interface geometry: the outline, the
signal pins (DEF template) and the power stripes (micro_librelane_config). So the container
is hardened once, around four copies of a placeholder macro that has just that interface and
is fully obstructed on the micro tile's layers ("slot"). A container for four real micro
tiles is then made by swapping the placeholders for the real layouts.

    python micro_container.py placeholder <output dir>
        Writes the placeholder macro: tt_micro_tile_slot.lef / .gds / .v

    python micro_container.py assemble <generic container "final" dir> <top name> <output dir> \\
            <micro tile dir> <micro tile dir> <micro tile dir> <micro tile dir>
        Writes <top name>.gds / .oas / .lef / .v into the output dir. A micro tile dir holds
        <macro>.oas (or .gds), <macro>.lef and <macro>.v, like a project dir of a shuttle.
"""

import glob
import os
import re
import sys
from typing import Dict, List, Tuple

import klayout.db as pya
import yaml

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SLOT = "tt_micro_tile_slot"
GENERIC = "tt_um_micro_container"

# sky130A
PDK = "sky130A"
SITE = (0.46, 2.72)
MARGIN_SITES = (6, 1)  # left/right in sites, top/bottom in rows
LAYER_GDS = {  # drawing, pin, label
    "met3": ((70, 20), (70, 16), (70, 5)),
}
PRBOUNDARY = (235, 4)
OBS_LAYERS = ["li1", "met1", "met2", "met3"]
# Worst case antenna properties of a micro tile pin. Input: the smallest gate of the library.
# Output: no diffusion at all, as an output that is tied to a constant has none (tie cell), so
# the container must protect the gates it connects to micro tile outputs by itself.
INPUT_GATE_AREA = 0.126
# Free space above and below a pin inside the pin strip, on the pin layer: room for a via3
# enclosure that overhangs the 0.3 um pin (up to 0.04 um) plus the met3 spacing (0.3 um)
PIN_CLEARANCE = 0.45
# Largest met3 enclosure of a via3 (M3M4_PR: 0.38 x 0.32 um, M3M4_PR_R: 0.32 x 0.38 um)
PAD_SIZE = 0.38
# Locations and orientations of the four slots in the 1x1 container, in select order
SLOTS = [((0.0, 0.0), "N"), ((90.62, 0.0), "FN"), ((0.0, 51.68), "N"), ((90.62, 51.68), "FN")]


def micro_tile_interface():
    """Outline, signal pins and power stripes of a micro tile, from the tools' own data."""
    from tech import tech_map

    with open(os.path.join(SCRIPT_DIR, f"tech/{PDK}/tile_sizes.yaml")) as f:
        _, _, width, height = map(float, yaml.safe_load(f)["micro"].split())
    with open(os.path.join(SCRIPT_DIR, f"tech/{PDK}/def/tt_block_micro_pg.def")) as f:
        template = f.read()
    pins = []
    for name, direction, layer, x1, y1, x2, y2, ox, oy in re.findall(
        r"- (\S+) \+ NET \S+ \+ DIRECTION (\w+) \+ USE SIGNAL\s+\+ PORT\s+"
        r"\+ LAYER (\w+) \( (-?\d+) (-?\d+) \) \( (-?\d+) (-?\d+) \)\s+\+ PLACED \( (\d+) (\d+) \)",
        template,
    ):
        rect = tuple((int(o) + int(d)) / 1000 for o, d in ((ox, x1), (oy, y1), (ox, x2), (oy, y2)))
        pins.append((name, direction, layer, rect))
    assert len(pins) == 18

    pdn = tech_map[PDK].micro_librelane_config
    core = (
        MARGIN_SITES[0] * SITE[0],
        MARGIN_SITES[1] * SITE[1],
        width - MARGIN_SITES[0] * SITE[0],
        height - MARGIN_SITES[1] * SITE[1],
    )
    layer = pdn["PDN_HORIZONTAL_LAYER"]
    w, pitch, offset, spacing = (pdn[k] for k in ("PDN_HWIDTH", "PDN_HPITCH", "PDN_HOFFSET", "PDN_HSPACING"))
    stripes: Dict[str, List[Tuple[float, float, float, float]]] = {"VPWR": [], "VGND": []}
    y = core[1] + offset
    while y + w / 2 <= core[3]:
        stripes["VPWR"].append((core[0], round(y - w / 2, 3), core[2], round(y + w / 2, 3)))
        yg = y + w + spacing
        if yg + w / 2 <= core[3]:
            stripes["VGND"].append((core[0], round(yg - w / 2, 3), core[2], round(yg + w / 2, 3)))
        y += pitch
    return width, height, pins, layer, stripes


def template_interface():
    """What lef_interface() must return for every micro tile LEF."""
    width, height, pins, power_layer, stripes = micro_tile_interface()
    result = [(name, layer, tuple(round(v, 3) for v in r)) for name, _, layer, r in pins]
    result += [(net, power_layer, tuple(round(v, 3) for v in s)) for net in stripes for s in stripes[net]]
    return (width, height), sorted(result)


def slot_pin(r):
    """The slot's view of a pin: as tall as the largest via pad of the layer above, so that the
    router can land a via inside it (it only accepts vias whose enclosure is inside a macro
    pin). The real pin is thinner: a pad then overhangs it by at most (PAD_SIZE - pin) / 2."""
    x1, y1, x2, y2 = r
    if y2 - y1 >= PAD_SIZE:
        return r
    cy = (y1 + y2) / 2
    return (x1, round(cy - PAD_SIZE / 2, 3), x2, round(cy + PAD_SIZE / 2, 3))


def write_placeholder(out_dir: str):
    width, height, pins, power_layer, stripes = micro_tile_interface()
    os.makedirs(out_dir, exist_ok=True)

    # LEF: the interface, with everything else obstructed on the micro tile's layers
    rect = lambda r: "RECT " + " ".join(f"{v:.3f}" for v in r) + " ;"
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
            lef += ["    PORT", f"      LAYER {power_layer} ;", f"        {rect(stripe)}", "    END"]
        lef += [f"  END {net}"]
    for name, direction, layer, r in pins:
        lef += [f"  PIN {name}", f"    DIRECTION {direction} ;", "    USE SIGNAL ;"]
        if direction == "INPUT":
            lef += [f"    ANTENNAGATEAREA {INPUT_GATE_AREA:.6f} ;"]
        lef += [
            "    PORT",
            f"      LAYER {layer} ;",
            f"        {rect(slot_pin(r))}",
            "    END",
            f"  END {name}",
        ]
    lef += ["  OBS"]
    dbu = 0.001
    outline = pya.Region(pya.DBox(0, 0, width, height).to_itype(dbu))
    # The pin strip at the east edge belongs to the container: a micro tile has nothing but
    # its pins there. Only the pin layer is obstructed in the strip, away from the pins.
    #
    # Antenna: the container may reach a pin only from above, with a via3 (PIN_ACCESS_LAYER)
    # landing on it, never with a wire on the pin layer or a via from below. Then the
    # container's metal and the micro tile's metal are in one piece only once the layer above
    # the pin is etched, and the micro tile has no metal on that layer. Below that layer, a
    # piece of a net is either the micro tile's or the container's, as in their own checks.
    # - pin layer: the obstruction keeps PIN_CLEARANCE away from the pins inside the strip, so
    #   that a via enclosure that overhangs a pin is legal; a wire on the pin layer cannot reach a
    #   pin from outside the slot because of the container's ROUTING_OBSTRUCTIONS next to the
    #   slot edge (config.json);
    # - layer below the pin: obstructed under the pins, so no via can land on a pin from below.
    strip_x = min(p[3][0] for p in pins)
    inner = pya.Region(pya.DBox(0, 0, strip_x, height).to_itype(dbu))
    pin_layer = pins[0][2]
    below_pin = OBS_LAYERS[OBS_LAYERS.index(pin_layer) - 1]
    pin_boxes = pya.Region()
    for p in pins:
        x1, y1, x2, y2 = p[3]
        pin_boxes.insert(pya.DBox(strip_x, y1 - PIN_CLEARANCE, x2, y2 + PIN_CLEARANCE).to_itype(dbu))
    for layer in OBS_LAYERS:
        obs = inner
        if layer == below_pin:
            obs = inner + pin_boxes
        if layer == power_layer or any(p[2] == layer for p in pins):
            keep = pin_boxes.dup()
            for r in [s for net in stripes.values() for s in net]:
                keep.insert(pya.DBox(*r).to_itype(dbu))
            obs = outline - keep
        lef += [f"      LAYER {layer} ;"]
        for box in obs.decompose_trapezoids_to_region().each():
            b = box.bbox().to_dtype(dbu)
            lef += [f"        {rect((b.left, b.bottom, b.right, b.top))}"]
    lef += ["  END", f"END {SLOT}", "END LIBRARY", ""]
    with open(os.path.join(out_dir, f"{SLOT}.lef"), "w") as f:
        f.write("\n".join(lef))

    # GDS: outline, pins and power stripes only
    layout = pya.Layout()
    layout.dbu = dbu
    cell = layout.create_cell(SLOT)
    cell.shapes(layout.layer(*PRBOUNDARY)).insert(pya.DBox(0, 0, width, height))
    shapes = [(name, layer, slot_pin(r)) for name, _, layer, r in pins]
    shapes += [(net, power_layer, s) for net in stripes for s in stripes[net]]
    for name, layer, r in shapes:
        drawing, pin, label = LAYER_GDS[layer]
        box = pya.DBox(*r)
        cell.shapes(layout.layer(*drawing)).insert(box)
        cell.shapes(layout.layer(*pin)).insert(box)
        cell.shapes(layout.layer(*label)).insert(pya.DText(name, box.center().x, box.center().y))
    layout.write(os.path.join(out_dir, f"{SLOT}.gds"))

    # Verilog: black box with power pins
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


def lef_interface(lef_text: str):
    """Pin geometry of a LEF macro on the layers a container connects to, for comparison."""
    result = []
    for name, body in re.findall(r"  PIN (\S+)\n(.*?)  END \1\n", lef_text, re.S):
        for layer, r in re.findall(r"LAYER (\S+) ;\s+RECT ([\d. ]+) ;", body):
            if layer in LAYER_GDS:
                result.append((name, layer, tuple(round(float(v), 3) for v in r.split())))
    size = re.search(r"SIZE ([\d.]+) BY ([\d.]+)", lef_text).groups()
    return tuple(map(float, size)), sorted(result)


def cell_signature(layout: pya.Layout, cell: pya.Cell):
    """Geometry and instances of a cell, to tell whether two cells are the same. Labels are
    left out: Magic versions differ in the labels they write for the same standard cell."""
    sig = []
    for layer_index in layout.layer_indexes():
        info = layout.get_info(layer_index)
        polygons = sorted(str(p) for p in pya.Region(cell.shapes(layer_index)).merged().each())
        if polygons:
            sig.append((info.layer, info.datatype, tuple(polygons)))
    sig.append(tuple(sorted(f"{layout.cell(i.cell_index).name} {i.trans}" for i in cell.each_inst())))
    return tuple(sorted(sig, key=str))


def read_layout_into(layout: pya.Layout, path: str, macro: str) -> Tuple[pya.Cell, Dict[str, int]]:
    """Reads a micro tile layout into `layout`. Cells that already exist with the same content
    (the standard cells) are shared, cells that exist with different content get a unique name."""
    stats = {"shared": 0, "renamed": 0, "new": 0}
    src = pya.Layout()
    src.read(path)
    src_top = src.cell(macro)
    assert src_top is not None, f"{path} has no cell {macro}"
    assert layout.dbu == src.dbu
    mapping: Dict[int, int] = {}
    # children before parents
    for index in src.each_cell_bottom_up():
        cell = src.cell(index)
        if not (index == src_top.cell_index() or index in src_top.called_cells()):
            continue
        existing = layout.cell(cell.name)
        target_name = cell.name
        if existing is not None:
            # compare with instances resolved by name
            if cell_signature(src, cell) == cell_signature(layout, existing):
                mapping[index] = existing.cell_index()
                stats["shared"] += 1
                continue
            target_name = f"{cell.name}__{macro}"
            assert layout.cell(target_name) is None
            stats["renamed"] += 1
        else:
            stats["new"] += 1
        new_cell = layout.create_cell(target_name)
        mapping[index] = new_cell.cell_index()
        for layer_index in src.layer_indexes():
            info = src.get_info(layer_index)
            target_layer = layout.layer(info.layer, info.datatype)
            for shape in cell.shapes(layer_index).each():
                new_cell.shapes(target_layer).insert(shape)
        for inst in cell.each_inst():
            new_cell.insert(pya.CellInstArray(mapping[inst.cell_index], inst.trans))
    return layout.cell(mapping[src_top.cell_index()]), stats


def assemble(generic_final: str, top: str, out_dir: str, micro_dirs: List[str]):
    """Makes a container for the given micro tiles out of the hardened generic container."""
    assert len(micro_dirs) == len(SLOTS)
    os.makedirs(out_dir, exist_ok=True)
    slot_interface = template_interface()

    layout = pya.Layout()
    layout.read(os.path.join(generic_final, "gds", f"{GENERIC}.gds"))
    top_cell = layout.cell(GENERIC)
    slot_cell = layout.cell(SLOT)
    dbu = layout.dbu

    macros = []
    for micro_dir in micro_dirs:
        (lef_file,) = glob.glob(os.path.join(micro_dir, "tt_um_*.lef"))
        macro = os.path.basename(lef_file)[: -len(".lef")]
        assert macro not in macros and macro != top, f"duplicate macro {macro}"
        with open(lef_file) as f:
            if lef_interface(f.read()) != slot_interface:
                raise ValueError(f"{macro}: pins or power stripes are not at the micro tile template positions")
        macros.append(macro)

    by_slot = {}
    for inst in top_cell.each_inst():
        if inst.cell_index != slot_cell.cell_index():
            continue
        origin = (round(inst.bbox().left * dbu, 3), round(inst.bbox().bottom * dbu, 3))
        index = next(i for i, (location, _) in enumerate(SLOTS) if location == origin)
        assert inst.trans.is_mirror() == (SLOTS[index][1] == "FN")
        by_slot[index] = inst
    assert len(by_slot) == len(SLOTS), "the generic container does not have four slots"

    for index, (micro_dir, macro) in enumerate(zip(micro_dirs, macros)):
        path = os.path.join(micro_dir, f"{macro}.oas")
        if not os.path.exists(path):
            path = os.path.join(micro_dir, f"{macro}.gds")
        cell, stats = read_layout_into(layout, path, macro)
        by_slot[index].cell_index = cell.cell_index()
        print(f"slot {index}: {macro} ({stats['new']} new cells, {stats['shared']} shared, {stats['renamed']} renamed)")
    layout.delete_cell(slot_cell.cell_index())
    top_cell.name = top
    layout.write(os.path.join(out_dir, f"{top}.gds"))
    layout.set_property("TT_PDK", PDK)
    layout.write(os.path.join(out_dir, f"{top}.oas"))

    with open(os.path.join(generic_final, "lef", f"{GENERIC}.lef")) as f:
        lef = f.read()
    with open(os.path.join(out_dir, f"{top}.lef"), "w") as f:
        f.write(re.sub(rf"\b{GENERIC}\b", top, lef))

    with open(os.path.join(generic_final, "pnl", f"{GENERIC}.pnl.v")) as f:
        netlist = f.read()
    netlist, count = re.subn(rf"\bmodule {GENERIC}\b", f"module {top}", netlist)
    assert count == 1
    for index, macro in enumerate(macros):
        netlist, count = re.subn(rf"\b{SLOT}(\s+)proj{index + 1}\b", rf"{macro}\1proj{index + 1}", netlist)
        assert count == 1, f"instance proj{index + 1} not found in the generic netlist"
    assert SLOT not in netlist
    with open(os.path.join(out_dir, f"{top}.v"), "w") as f:
        f.write(netlist)
    return macros


GROUP_PREFIX = "tt_um_micro_tiles_group_"
GROUP_SIZE = len(SLOTS)
GROUP_MARKER = "micro_tile_group"
PIN_KEYS = [f"{bus}[{i}]" for bus in ("ui", "uo") for i in range(8)]


def make_groups(project_dir: str, micro_projects, shuttle_id: str, repo_url: str, commit: str):
    """Groups the micro tile projects of a shuttle four at a time, in submission order, and
    writes a project directory for the container of every group: info.yaml, docs, commit_id.json
    and subprojects.json, plus projects/groups.json. The layout, LEF and netlist of a container
    are written later by assemble_groups(), once the generic container is hardened.

    Returns a list of (container macro name, micro tile projects)."""
    import json
    import shutil

    # Container directories are generated: start from scratch, but keep the layout, LEF and
    # netlist that assemble() wrote for a container whose micro tiles are still the same
    # (every configure.py command gets here, also the ones that run after --copy-macros)
    assembled = {}
    for entry in sorted(os.listdir(project_dir)):
        group_dir = os.path.join(project_dir, entry)
        marker = os.path.join(group_dir, "commit_id.json")
        if os.path.exists(marker) and json.load(open(marker)).get(GROUP_MARKER):
            with open(os.path.join(group_dir, "subprojects.json")) as f:
                old_members = [p["macro"] for p in json.load(f)["projects"].values()]
            keep = [f"{entry}.{ext}" for ext in ("gds", "oas", "lef", "v")]
            assembled[entry] = (old_members, keep)
            for item in os.listdir(group_dir):
                path = os.path.join(group_dir, item)
                if os.path.isdir(path):
                    shutil.rmtree(path)
                elif item not in keep:
                    os.remove(path)

    micro_projects = sorted(micro_projects, key=lambda p: p.sort_id)
    groups = []
    for start in range(0, len(micro_projects), GROUP_SIZE):
        members = micro_projects[start : start + GROUP_SIZE]
        name = f"{GROUP_PREFIX}{start // GROUP_SIZE + 1}"
        if len(members) < GROUP_SIZE:
            # TODO: fill the empty slots with a filler micro tile (outputs tied low)
            raise NotImplementedError(f"{name} has {len(members)} micro tiles: filler micro tiles are not implemented")
        group_dir = os.path.join(project_dir, name)
        os.makedirs(os.path.join(group_dir, "docs"), exist_ok=True)
        old_members, keep = assembled.pop(name, ([], []))
        if old_members != [m.info.top_module for m in members]:
            for item in keep:
                if os.path.exists(os.path.join(group_dir, item)):
                    os.remove(os.path.join(group_dir, item))

        subprojects = {}
        rows = []
        for index, project in enumerate(members):
            macro = project.info.top_module
            shutil.copytree(os.path.join(project.local_dir, "docs"), os.path.join(group_dir, "docs", macro))
            shutil.copy(os.path.join(project.local_dir, "info.yaml"), os.path.join(group_dir, "docs", macro, "info.yaml"))
            pins = project.info.pinout.ui + project.info.pinout.uo
            subprojects[str(index)] = {
                "macro": macro,
                "title": project.info.title,
                "author": project.info.author,
                "discord": getattr(project.info, "discord", "") or "",
                "description": project.info.description,
                "clock_hz": project.info.clock_hz,
                "repo": project.git_url,
                "commit": project.commit_id,
                "pinout": dict(zip(PIN_KEYS, pins)),
            }
            rows.append(f"| {index} | [{project.info.title}]({macro}/info.md) | {project.info.author} | `{macro}` |")
        with open(os.path.join(group_dir, "subprojects.json"), "w") as f:
            json.dump({"shuttle": shuttle_id, "index_bits": 2, "projects": subprojects}, f, indent=2)
            f.write("\n")
        with open(os.path.join(group_dir, "docs", "info.md"), "w") as f:
            f.write(GROUP_DOC.format(rows="\n".join(rows)))
        pinout = {f"ui[{i}]": f"micro tile ui[{i}]" for i in range(8)}
        pinout.update({f"uo[{i}]": f"micro tile uo[{i}]" for i in range(8)})
        pinout.update({f"uio[{i}]": f"sel[{i}]" if i < 2 else "" for i in range(8)})
        info = {
            "project": {
                "title": f"Micro tiles group {start // GROUP_SIZE + 1}",
                "author": "Tiny Tapeout",
                "discord": "",
                "description": "Four micro tile projects in a single Tiny Tapeout tile",
                "language": "Verilog",
                "clock_hz": 0,
                "tiles": "1x1",
                "top_module": name,
                "source_files": ["project.v"],
            },
            "pinout": pinout,
            "yaml_version": 6,
        }
        with open(os.path.join(group_dir, "info.yaml"), "w") as f:
            yaml.safe_dump(info, f, sort_keys=False)
        with open(os.path.join(group_dir, "commit_id.json"), "w") as f:
            json.dump(
                {
                    "app": "Tiny Tapeout micro tile container",
                    "repo": repo_url,
                    "commit": commit,
                    "sort_id": members[0].sort_id,
                    GROUP_MARKER: True,
                },
                f,
                indent=2,
            )
            f.write("\n")
        groups.append((name, members))

    # Containers that no longer exist
    for name in assembled:
        shutil.rmtree(os.path.join(project_dir, name))

    with open(os.path.join(project_dir, "groups.json"), "w") as f:
        json.dump({"shuttle": shuttle_id, "groups": [name for name, _ in groups]}, f, indent=2)
        f.write("\n")
    return groups


GROUP_DOC = """## How it works

Four micro tile projects, combined into a single Tiny Tapeout tile.

Each micro tile is a separately hardened macro with a reduced interface: `clk`, `rst_n`, `ui_in[7:0]` and `uo_out[7:0]`. The container muxes the tile's pins to the selected one. The other micro tiles get no clock and are held in reset.

| `uio[1:0]` | Micro tile | Author | Macro |
| ---------- | ---------- | ------ | ----- |
{rows}

## How to test

Select a micro tile with `uio[1:0]`, then follow the instructions of that micro tile.

## External hardware

None
"""


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "placeholder":
        write_placeholder(sys.argv[2])
    elif len(sys.argv) == 9 and sys.argv[1] == "assemble":
        assemble(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:9])
    else:
        print(__doc__)
        sys.exit(1)
