#!/usr/bin/env python3
"""
Micro tile containers, assembled without place and route.

All the micro tiles of a PDK have the same interface geometry: the signal pins
come from the DEF template and the power stripes from micro_librelane_config, and
a micro tile must leave the layers of the container empty. So one generic container
can be hardened around four placeholder macros (`make-slot` writes the views of the
placeholder) and then be turned into any number of real containers by swapping the
placeholders for real micro tiles (`assemble`): a layout merge and a netlist edit.

  micro_tiles.py make-slot --lef <reference micro tile LEF> --out <dir>
  micro_tiles.py assemble --container <dir> --name <macro> --out <dir> <micro tile dir> x4

The directories have the layout of a shuttle's projects/<macro> directory.
"""

import argparse
import json
import logging
import os
import re
import shutil
import time
from typing import Dict, List, Optional

import klayout.db as pya
import yaml

SLOT_MACRO = "tt_micro_slot"
SLOTS_PER_CONTAINER = 4
# Signal ports of a micro tile, in the order of the DEF template
INPUT_PORTS = ["clk", "rst_n"] + [f"ui_in[{i}]" for i in range(8)]
OUTPUT_PORTS = [f"uo_out[{i}]" for i in range(8)]
# Worst case antenna annotations of the placeholder pins: the smallest gate and
# the smallest diffusion of the standard cell library
SLOT_ANTENNA = {"gf180mcuD": {"gate": 0.396, "diff": 0.361}}
# Layers a micro tile may use: the placeholder blocks all of them
MICRO_LAYERS = {"gf180mcuD": ["Metal1", "Metal2", "Metal3"]}
BOUNDARY_LAYER = {"gf180mcuD": (0, 0)}
STD_CELL_PREFIX = {"gf180mcuD": "gf180mcu_fd_sc_"}


class MicroTileError(Exception):
    pass


def lef_interface(lef: str) -> str:
    """The pin section of a LEF macro, without its name and antenna annotations."""
    name = re.search(r"^MACRO (\S+)", lef, flags=re.M).group(1)
    start = lef.index(f"MACRO {name}")
    end = lef.index("  OBS") if "  OBS" in lef else lef.index(f"END {name}")
    body = lef[start:end].replace(name, "MACRO_NAME")
    return re.sub(r"^\s*ANTENNA.*\n", "", body, flags=re.M)


def make_slot(reference_lef: str, out_dir: str, pdk: str):
    """Writes the LEF, GDS and Verilog views of the placeholder micro tile."""
    with open(reference_lef) as f:
        lef = f.read()
    name = re.search(r"^MACRO (\S+)", lef, flags=re.M).group(1)
    width, height = map(float, re.search(r"SIZE (\S+) BY (\S+) ;", lef).group(1, 2))
    header = lef[: lef.index(f"MACRO {name}")]
    body = lef_interface(lef).replace("MACRO_NAME", SLOT_MACRO)
    antenna = SLOT_ANTENNA[pdk]
    for port in INPUT_PORTS:
        body = body.replace(
            f"  PIN {port}\n    DIRECTION INPUT ;\n    USE SIGNAL ;\n",
            f"  PIN {port}\n    DIRECTION INPUT ;\n    USE SIGNAL ;\n    ANTENNAGATEAREA {antenna['gate']:.6f} ;\n",
        )
    for port in OUTPUT_PORTS:
        body = body.replace(
            f"  PIN {port}\n    DIRECTION OUTPUT ;\n    USE SIGNAL ;\n",
            f"  PIN {port}\n    DIRECTION OUTPUT ;\n    USE SIGNAL ;\n    ANTENNADIFFAREA {antenna['diff']:.6f} ;\n",
        )
    if body.count("ANTENNAGATEAREA") != len(INPUT_PORTS):
        raise MicroTileError("unexpected pin format in the reference LEF")
    obs = "  OBS\n"
    for layer in MICRO_LAYERS[pdk]:
        obs += f"      LAYER {layer} ;\n        RECT 0.000 0.000 {width:.3f} {height:.3f} ;\n"
    obs += "  END\n"
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"{SLOT_MACRO}.lef"), "w") as f:
        f.write(header + body + obs + f"END {SLOT_MACRO}\nEND LIBRARY\n\n")

    # Layout: only the outline. The real micro tile replaces it at assembly time.
    layout = pya.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell(SLOT_MACRO)
    layer = layout.layer(*BOUNDARY_LAYER[pdk])
    cell.shapes(layer).insert(pya.DBox(0, 0, width, height))
    layout.write(os.path.join(out_dir, f"{SLOT_MACRO}.gds"))

    with open(os.path.join(out_dir, f"{SLOT_MACRO}.v"), "w") as f:
        f.write(
            "// Placeholder for a micro tile: replaced by a real micro tile when a\n"
            "// container is assembled\n"
            "(* blackbox *)\n"
            f"module {SLOT_MACRO} (\n"
            "    input  wire       clk,\n"
            "    input  wire       rst_n,\n"
            "    inout  wire       VPWR,\n"
            "    inout  wire       VGND,\n"
            "    input  wire [7:0] ui_in,\n"
            "    output wire [7:0] uo_out\n"
            ");\n"
            "endmodule\n"
        )
    logging.info(f"wrote the views of {SLOT_MACRO} to {out_dir}")


class MicroTile:
    """A hardened micro tile in a projects/<macro> style directory."""

    def __init__(self, path: str):
        self.path = path
        with open(os.path.join(path, "info.yaml")) as f:
            self.info = yaml.safe_load(f)
        project = self.info["project"]
        if project.get("tiles") != "micro":
            raise MicroTileError(f"{path} is not a micro tile")
        if "wokwi_id" in project:
            self.macro = f"tt_um_wokwi_{project['wokwi_id']}"
        else:
            self.macro = project["top_module"]
        self.commit_id: Dict[str, str] = {}
        commit_id_file = os.path.join(path, "commit_id.json")
        if os.path.exists(commit_id_file):
            with open(commit_id_file) as f:
                self.commit_id = json.load(f)
        self.pdk_info = None
        pdk_file = os.path.join(path, "pdk.json")
        if os.path.exists(pdk_file):
            with open(pdk_file) as f:
                self.pdk_info = json.load(f)

    def file(self, ext: str) -> str:
        return os.path.join(self.path, f"{self.macro}.{ext}")

    def layout_file(self) -> str:
        for ext in ("oas", "gds"):
            if os.path.exists(self.file(ext)):
                return self.file(ext)
        raise MicroTileError(f"{self.macro}: no layout")

    def subproject(self) -> dict:
        project = self.info["project"]
        pinout = {
            pin: name
            for pin, name in self.info.get("pinout", {}).items()
            if pin.startswith(("ui[", "uo["))
        }
        return {
            "macro": self.macro,
            "title": project["title"],
            "author": project["author"],
            "discord": project.get("discord", ""),
            "description": project["description"],
            "clock_hz": project.get("clock_hz", 0),
            "repo": self.commit_id.get("repo", ""),
            "commit": self.commit_id.get("commit", ""),
            "pinout": pinout,
        }


def check_micro_tile(tile: MicroTile, slot_lef: str, layout: pya.Layout, pdk: str):
    """A micro tile fits a slot if its interface is exactly that of the placeholder."""
    with open(tile.file("lef")) as f:
        lef = f.read()
    if lef_interface(lef) != lef_interface(slot_lef):
        raise MicroTileError(
            f"{tile.macro}: the pins in the LEF differ from the micro tile template"
        )
    if layout.top_cell().name != tile.macro:
        raise MicroTileError(f"{tile.macro}: wrong top cell {layout.top_cell().name}")
    with open(tile.file("v")) as f:
        modules = re.findall(r"^\s*module\s+(\S+?)\s*[(;]", f.read(), flags=re.M)
    if modules != [tile.macro]:
        raise MicroTileError(f"{tile.macro}: unexpected modules in netlist: {modules}")


def merge_micro_tile(
    target: pya.Layout, source: pya.Layout, macro: str, pdk: str
) -> pya.Cell:
    """
    Copies a micro tile into the layout of the container and returns its top cell.
    Standard cells with the same name are shared (the micro tiles and the container
    are built with the same PDK version); any other name clash is an error.
    """
    if target.dbu != source.dbu:
        raise MicroTileError(f"{macro}: database unit {source.dbu} != {target.dbu}")
    mapping: Dict[int, int] = {}
    for cell in source.each_cell():
        existing = target.cell(cell.name)
        if existing is not None:
            if not cell.name.startswith(STD_CELL_PREFIX[pdk]):
                raise MicroTileError(f"{macro}: cell name clash: {cell.name}")
            mapping[cell.cell_index()] = existing.cell_index()
        else:
            mapping[cell.cell_index()] = target.create_cell(cell.name).cell_index()
    layer_map = {
        index: target.layer(source.get_info(index)) for index in source.layer_indexes()
    }
    for cell in source.each_cell():
        new_cell = target.cell(mapping[cell.cell_index()])
        if not new_cell.is_empty():
            continue  # shared standard cell, already there
        for index, new_index in layer_map.items():
            new_cell.shapes(new_index).insert(cell.shapes(index))
        for inst in cell.each_inst():
            array = inst.cell_inst
            array.cell_index = mapping[inst.cell_index]
            new_cell.insert(array)
    return target.cell(mapping[source.top_cell().cell_index()])


def assemble(
    container_dir: str,
    name: str,
    out_dir: str,
    tile_dirs: List[Optional[str]],
    pdk: str,
    shuttle: str = "",
    slot_lef_file: Optional[str] = None,
    write_gds: bool = False,
):
    """Builds the container `name` in out_dir from the generic container and four micro tiles."""
    with open(os.path.join(container_dir, "info.yaml")) as f:
        container_info = yaml.safe_load(f)
    generic = container_info["project"]["top_module"]
    if slot_lef_file is None:
        slot_lef_file = os.path.join(container_dir, f"{SLOT_MACRO}.lef")
    with open(slot_lef_file) as f:
        slot_lef = f.read()
    tiles = [MicroTile(path) if path else None for path in tile_dirs]
    macros = [tile.macro for tile in tiles if tile]
    if len(set(macros)) != len(macros):
        raise MicroTileError(f"duplicate micro tiles: {macros}")

    os.makedirs(os.path.join(out_dir, "docs"), exist_ok=True)

    # Layout: put each micro tile where its placeholder is
    layout = pya.Layout()
    layout_file = os.path.join(container_dir, f"{generic}.oas")
    if not os.path.exists(layout_file):
        layout_file = os.path.join(container_dir, f"{generic}.gds")
    layout.read(layout_file)
    top = layout.top_cell()
    if top.name != generic:
        raise MicroTileError(f"unexpected top cell {top.name} in {layout_file}")
    slot_cell = layout.cell(SLOT_MACRO)
    slots = sorted(
        (inst for inst in top.each_inst() if inst.cell_index == slot_cell.cell_index()),
        key=lambda inst: (inst.bbox().bottom, inst.bbox().left),
    )
    if len(slots) != len(tiles):
        raise MicroTileError(f"{len(slots)} slots for {len(tiles)} micro tiles")
    for slot, tile in zip(slots, tiles):
        if tile is None:
            continue  # empty slot: the placeholder outline stays
        source = pya.Layout()
        source.read(tile.layout_file())
        check_micro_tile(tile, slot_lef, source, pdk)
        cell = merge_micro_tile(layout, source, tile.macro, pdk)
        top.insert(pya.CellInstArray(cell.cell_index(), slot.cell_inst.trans))
        slot.delete()
    if all(tiles):
        layout.delete_cell(slot_cell.cell_index())
    top.name = name
    layout.set_property("TT_PDK", pdk)
    layout.write(os.path.join(out_dir, f"{name}.oas"))
    if write_gds:
        layout.write(os.path.join(out_dir, f"{name}.gds"))

    # LEF: the abstract of the generic container under the new name
    with open(os.path.join(container_dir, f"{generic}.lef")) as f:
        lef = f.read()
    with open(os.path.join(out_dir, f"{name}.lef"), "w") as f:
        f.write(lef.replace(generic, name))

    # Netlist: the slots become the real micro tiles, whose netlists are appended
    with open(os.path.join(container_dir, f"{generic}.v")) as f:
        netlist = f.read()
    marker = netlist.find("\n// Macro:")
    if marker >= 0:
        netlist = netlist[:marker]
    netlist = netlist.replace(f"module {generic} ", f"module {name} ")
    # The slots in the netlist are instances proj1..projN, in select order
    for index, tile in enumerate(tiles):
        if tile is None:
            continue
        netlist, count = re.subn(
            rf"\b{SLOT_MACRO} (proj{index + 1}) \(", rf"{tile.macro} \1 (", netlist
        )
        if count != 1:
            raise MicroTileError(f"slot proj{index + 1} not found in the netlist")
    with open(os.path.join(out_dir, f"{name}.v"), "w") as f:
        f.write(netlist)
        for tile in tiles:
            if tile is None:
                continue
            with open(tile.file("v")) as tile_netlist:
                f.write(f"\n// Macro: {tile.macro}\n")
                f.write(tile_netlist.read())

    # Description of the micro tiles: subprojects.json and their docs
    projects = {}
    doc_lines = [
        "## How it works",
        "",
        "This is a container for four micro tiles: small, separately hardened designs with a clock, a reset,",
        "eight inputs and eight outputs. `uio[1:0]` selects one of them. The selected micro tile gets the clock,",
        "the reset and the inputs of the project and drives its outputs. The others are held in reset.",
        "",
        "| `uio[1:0]` | Micro tile | Author |",
        "| --- | --- | --- |",
    ]
    for index, tile in enumerate(tiles):
        if tile is None:
            projects[str(index)] = None
            doc_lines.append(f"| {index} | (empty) | |")
            continue
        projects[str(index)] = tile.subproject()
        docs_dir = os.path.join(out_dir, "docs", tile.macro)
        shutil.rmtree(docs_dir, ignore_errors=True)
        shutil.copytree(os.path.join(tile.path, "docs"), docs_dir)
        shutil.copy(os.path.join(tile.path, "info.yaml"), docs_dir)
        project = tile.info["project"]
        doc_lines.append(
            f"| {index} | [{project['title']}]({tile.macro}/info.md) | {project['author']} |"
        )
    doc_lines += [
        "",
        "## How to test",
        "",
        "Set `uio[1:0]` to the number of the micro tile, then follow the instructions of that micro tile.",
        "",
        "## External hardware",
        "",
        "See the micro tiles.",
        "",
    ]
    with open(os.path.join(out_dir, "docs", "info.md"), "w") as f:
        f.write("\n".join(doc_lines))
    with open(os.path.join(out_dir, "subprojects.json"), "w") as f:
        json.dump(
            {"shuttle": shuttle, "index_bits": 2, "projects": projects}, f, indent=2
        )
        f.write("\n")

    # info.yaml of the container
    info = container_info
    info["project"]["top_module"] = name
    info["project"]["title"] = "Micro tiles: " + ", ".join(
        tile.info["project"]["title"] for tile in tiles if tile
    )
    info["project"][
        "description"
    ] = "Container for four micro tiles, selected with uio[1:0]"
    info["pinout"]["uio[0]"] = "micro tile select [0]"
    info["pinout"]["uio[1]"] = "micro tile select [1]"
    with open(os.path.join(out_dir, "info.yaml"), "w") as f:
        yaml.safe_dump(info, f, sort_keys=False)

    # Provenance: the container build, plus the commit of every micro tile
    with open(os.path.join(container_dir, "commit_id.json")) as f:
        commit_id = json.load(f)
    commit_id["micro_tiles"] = {
        str(index): tile.commit_id for index, tile in enumerate(tiles) if tile
    }
    with open(os.path.join(out_dir, "commit_id.json"), "w") as f:
        json.dump(commit_id, f, indent=2)
    for name_ in ["pdk.json", "LICENSE"]:
        source = os.path.join(container_dir, name_)
        if os.path.exists(source):
            shutil.copy(source, out_dir)
    stats = os.path.join(container_dir, "stats")
    if os.path.isdir(stats):
        shutil.copytree(stats, os.path.join(out_dir, "stats"), dirs_exist_ok=True)
    logging.info(f"assembled {name}: {', '.join(macros)}")


def assemble_shuttle_containers(config: dict):
    """
    Shuttle step: groups the micro tiles in the projects directory four at a time, in
    submission order, and assembles a container project for each group from the
    generic container of the shuttle (config key micro_tile_container).

    The generated project directories (tt_um_micro_tiles_group_<n>) are ordinary 1x1
    projects for the rest of the flow. Their commit_id.json lists the micro tiles.
    """
    project_dir = config["project_dir"]
    container_dir = config.get("micro_tile_container", "micro_tiles/container")
    tiles = []
    for entry in sorted(os.listdir(project_dir)):
        path = os.path.join(project_dir, entry)
        info_file = os.path.join(path, "info.yaml")
        commit_id_file = os.path.join(path, "commit_id.json")
        if not os.path.exists(info_file) or not os.path.exists(commit_id_file):
            continue
        with open(commit_id_file) as f:
            commit_id = json.load(f)
        if "micro_tiles" in commit_id:
            shutil.rmtree(path)  # a container from an earlier run: generated again
            continue
        if commit_id.get("skip", False):
            continue
        with open(info_file) as f:
            if yaml.safe_load(f)["project"].get("tiles") != "micro":
                continue
        tiles.append((commit_id["sort_id"], path))
    groups_file = os.path.join(project_dir, "groups.json")
    if not tiles:
        return
    if not os.path.isdir(container_dir):
        raise MicroTileError(
            f"the shuttle has micro tiles but no generic container in {container_dir}"
        )
    tiles.sort()
    group_names = []
    for start in range(0, len(tiles), SLOTS_PER_CONTAINER):
        group = tiles[start : start + SLOTS_PER_CONTAINER]
        name = f"tt_um_micro_tiles_group_{len(group_names) + 1}"
        paths: List[Optional[str]] = [path for _, path in group]
        paths += [None] * (SLOTS_PER_CONTAINER - len(group))
        out_dir = os.path.join(project_dir, name)
        started = time.time()
        assemble(
            container_dir, name, out_dir, paths, config["pdk"], shuttle=config["id"]
        )
        # The container takes the place of its first micro tile in the shuttle order
        with open(os.path.join(out_dir, "commit_id.json")) as f:
            commit_id = json.load(f)
        commit_id["sort_id"] = group[0][0]
        with open(os.path.join(out_dir, "commit_id.json"), "w") as f:
            json.dump(commit_id, f, indent=2)
        logging.info(f"{name} assembled in {time.time() - started:.1f} s")
        group_names.append(name)
    with open(groups_file, "w") as f:
        json.dump({"groups": group_names}, f, indent=2)
        f.write("\n")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdk", default="gf180mcuD")
    sub = parser.add_subparsers(dest="command", required=True)
    slot = sub.add_parser("make-slot")
    slot.add_argument("--lef", required=True)
    slot.add_argument("--out", required=True)
    asm = sub.add_parser("assemble")
    asm.add_argument("--container", required=True)
    asm.add_argument("--name", required=True)
    asm.add_argument("--out", required=True)
    asm.add_argument("--shuttle", default="")
    asm.add_argument("tiles", nargs="+")
    args = parser.parse_args()
    if args.command == "make-slot":
        make_slot(args.lef, args.out, args.pdk)
    else:
        tiles = [None if tile == "-" else tile for tile in args.tiles]
        assemble(
            args.container,
            args.name,
            args.out,
            tiles,
            args.pdk,
            args.shuttle,
            write_gds=True,
        )


if __name__ == "__main__":
    main()
