import json
import os
import re
import shutil
from types import SimpleNamespace

import klayout.db as pya
import pytest
import yaml

import micro_container as mc
from project_info import ProjectInfo
from shuttle import micro_tile_index_entries

PDK = "sky130A"


def micro_project(name: str, sort_id: int):
    info = SimpleNamespace(
        top_module=name, title=f"Title {name}", author="Author", tiles="micro"
    )
    return SimpleNamespace(
        info=info,
        sort_id=sort_id,
        git_url=f"https://github.com/example/{name}",
        commit_id="0" * 40,
        mux_address=None,
    )


def macros(group):
    return [m.info.top_module if m is not None else None for m in group]


def test_groups_in_submission_order():
    projects = [
        micro_project(f"tt_um_m{i}", sort_id)
        for i, sort_id in enumerate([50, 10, 40, 30, 20, 60])
    ]
    groups = mc.make_groups(projects)
    assert [macros(g) for g in groups] == [
        ["tt_um_m1", "tt_um_m4", "tt_um_m3", "tt_um_m2"],
        ["tt_um_m0", "tt_um_m5", None, None],
    ]
    assert [mc.group_name(i) for i in range(2)] == [
        "tt_um_micro_tiles_group_1",
        "tt_um_micro_tiles_group_2",
    ]


def test_groups_full_and_empty():
    assert mc.make_groups([]) == []
    projects = [micro_project(f"tt_um_m{i}", i) for i in range(8)]
    groups = mc.make_groups(projects)
    assert len(groups) == 2 and None not in groups[0] + groups[1]


def test_withdrawn_micro_tile_moves_the_ones_after_it():
    projects = [micro_project(f"tt_um_m{i}", i) for i in range(6)]
    before = [macros(g) for g in mc.make_groups(projects)]
    after = [macros(g) for g in mc.make_groups(projects[:2] + projects[3:])]
    assert before[0][:2] == after[0][:2]
    assert after == [
        ["tt_um_m0", "tt_um_m1", "tt_um_m3", "tt_um_m4"],
        ["tt_um_m5", None, None, None],
    ]


def test_index_entries():
    members = [micro_project("tt_um_a", 1), None, micro_project("tt_um_b", 2), None]
    container = SimpleNamespace(micro_tiles=members)
    info = {
        "macro": "tt_um_micro_tiles_group_1",
        "address": 37,
        "x": 1,
        "y": 4,
        "tiles": "1x1",
        "repo": "https://github.com/example/shuttle",
        "commit": "1" * 40,
    }
    entries = micro_tile_index_entries(container, info)  # type: ignore[arg-type]
    assert entries[0] == {
        **info,
        "type": "group",
        "subtile_addr_bits": 2,
        "micro_tiles": ["tt_um_a", None, "tt_um_b", None],
    }
    assert [(e["macro"], e["subtile_addr"]) for e in entries[1:]] == [
        ("tt_um_a", 0),
        ("tt_um_b", 2),
    ]
    for entry in entries[1:]:
        assert entry["type"] == "subtile"
        assert entry["address"] == 37
        assert entry["subtile_group"] == "tt_um_micro_tiles_group_1"
        assert entry["tiles"] == "micro"
    assert members[0].mux_address == 37


def test_group_project(tmp_path):
    members = [micro_project("tt_um_a", 7), micro_project("tt_um_b", 9), None, None]
    group_dir = mc.write_group_project(
        str(tmp_path), 0, members, PDK, "https://github.com/example/shuttle", "1" * 40
    )
    assert os.path.basename(group_dir) == "tt_um_micro_tiles_group_1"
    assert mc.is_group_dir(group_dir)
    with open(os.path.join(group_dir, "info.yaml")) as f:
        info = ProjectInfo(yaml.safe_load(f), {"1x1": "", "micro": ""})
    assert info.top_module == "tt_um_micro_tiles_group_1"
    assert info.tiles == "1x1" and not info.is_micro
    with open(os.path.join(group_dir, "commit_id.json")) as f:
        commit_id = json.load(f)
    assert commit_id["sort_id"] == 7
    assert commit_id["micro_tiles"] == ["tt_um_a", "tt_um_b", None, None]
    with open(os.path.join(group_dir, ".gitignore")) as f:
        assert f.read().splitlines()[-1] == "*"
    with open(os.path.join(group_dir, "docs", "info.md")) as f:
        doc = f.read()
    assert "| 1 | Title tt_um_b | Author | `tt_um_b` |" in doc
    assert f"| 3 | (empty: outputs low) | | `{mc.FILLER}` |" in doc

    # The assembled views stay while the group's micro tiles stay the same
    layout = os.path.join(group_dir, "tt_um_micro_tiles_group_1.oas")
    open(layout, "w").close()
    mc.write_group_project(str(tmp_path), 0, members, PDK, "x", "1" * 40)
    assert os.path.exists(layout)
    members[2] = micro_project("tt_um_c", 11)
    mc.write_group_project(str(tmp_path), 0, members, PDK, "x", "1" * 40)
    assert not os.path.exists(layout)

    os.makedirs(os.path.join(tmp_path, "tt_um_user_project"))
    mc.remove_stale_groups(str(tmp_path), [])
    assert sorted(os.listdir(tmp_path)) == ["tt_um_user_project"]


def test_placeholder_matches_frozen_slot(tmp_path):
    """The frozen container was hardened around this placeholder: it must still follow the
    micro tile DEF template and power grid."""
    mc.write_placeholder(PDK, str(tmp_path))
    frozen = mc.frozen_dir(PDK)
    for ext in ("lef", "v"):
        with (
            open(os.path.join(tmp_path, f"{mc.SLOT}.{ext}")) as a,
            open(os.path.join(frozen, f"{mc.SLOT}.{ext}")) as b,
        ):
            assert a.read() == b.read()
    signatures = []
    for path in (
        os.path.join(tmp_path, f"{mc.SLOT}.gds"),
        os.path.join(frozen, f"{mc.SLOT}.gds"),
        os.path.join(frozen, f"{mc.GENERIC}.oas"),
    ):
        layout = pya.Layout()
        layout.read(path)
        signatures.append(mc.cell_signature(layout, layout.cell(mc.SLOT)))
    assert signatures[0] == signatures[1] == signatures[2]


def own_region(layout, top, layer, skip):
    """Shapes of `top` and its subcells on `layer`, without the cells in `skip`."""
    index = layout.find_layer(*layer)
    if index is None:
        return pya.Region()
    it = top.begin_shapes_rec(index)
    it.unselect_cells([c.cell_index() for c in skip])
    return pya.Region(it).merged()


def test_frozen_container_leaves_the_slots_to_the_micro_tiles():
    """Inside a slot the container has nothing on a micro tile's layers but the power vias,
    inside the power stripes; outside, it keeps edge_clearance from the slot outline."""
    t = mc.MICRO_CONTAINER_TECH[PDK]
    mc.verify_frozen(PDK)
    layout = pya.Layout()
    layout.read(os.path.join(mc.frozen_dir(PDK), f"{mc.GENERIC}.oas"))
    top = layout.cell(mc.GENERIC)
    slot_cell = layout.cell(mc.SLOT)
    (width, height), interface = mc.template_interface(PDK)
    stripes = pya.Region()
    for name, _, r in interface:
        if name in ("VPWR", "VGND"):
            stripes.insert(pya.DBox(*r).to_itype(layout.dbu))
    slots = [i for i in top.each_inst() if i.cell.name == mc.SLOT]
    origins = []
    for inst in slots:
        trans = inst.dcplx_trans
        tile = pya.DBox(0, 0, width, height).transformed(trans)
        origins.append(
            ((round(tile.left, 3), round(tile.bottom, 3)), trans.is_mirror())
        )
        outline = pya.Region(tile.to_itype(layout.dbu))
        power = stripes.transformed(inst.cplx_trans)
        for name in t.routing_layers + ("via3",):
            gds = t.routing_gds.get(name, t.stack_gds.get(name))
            own = own_region(layout, top, gds, [slot_cell])
            inside = own & outline
            if name in ("met3", "via3"):
                assert (inside - power).is_empty(), name
            else:
                assert inside.is_empty(), name
            if name in t.edge_clearance:
                d = round(t.edge_clearance[name] / layout.dbu)
                band = outline.sized(d) - outline
                assert (own & band).is_empty(), name
    assert sorted(origins) == sorted(
        (location, orientation == "FN") for location, orientation in t.slots
    )


def test_slots_are_apart():
    """Two micro tiles never face each other closer than slot_gap, which is at least the
    spacing of every metal layer."""
    t = mc.MICRO_CONTAINER_TECH[PDK]
    (width, height), _ = mc.template_interface(PDK)
    boxes = [pya.DBox(x, y, x + width, y + height) for (x, y), _ in t.slots]
    assert t.slot_gap >= max(t.edge_clearance.values())
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            gap = max(
                b.left - a.right, a.left - b.right, b.bottom - a.top, a.bottom - b.top
            )
            assert gap >= t.slot_gap - 1e-9


def test_jumpers_copy_the_pins_inside_the_slot():
    """Inside the micro tile outline a jumper is exactly its pin; within edge_clearance of the
    outline it is as tall as the pin; everything else of it is further away."""
    t = mc.MICRO_CONTAINER_TECH[PDK]
    width, height, pins, _, _ = mc.micro_tile_interface(PDK)
    dbu = 0.001
    outline = pya.Region(pya.DBox(0, 0, width, height).to_itype(dbu))
    for _, _, layer, r in pins:
        shapes = mc.pin_stack(PDK, r)[1]
        met3 = pya.Region()
        for name, box in shapes:
            if name == "met3":
                met3.insert(pya.DBox(*box).to_itype(dbu))
            else:
                # via3 and met4: a micro tile has nothing there
                assert box[0] >= width + t.edge_clearance["met3"] - 1e-9
        met3.merge()
        pin = pya.Region(pya.DBox(*r).to_itype(dbu))
        assert ((met3 & outline) ^ pin).is_empty()
        d = round(t.edge_clearance["met3"] / dbu)
        near = met3 & (outline.sized(d) - outline)
        x1, y1, x2, y2 = r
        extension = pya.DBox(x2, y1, x2 + t.edge_clearance["met3"], y2)
        assert (near ^ pya.Region(extension.to_itype(dbu))).is_empty()


def test_tile_metal_at_the_outline_is_accepted(tmp_path):
    """No keep-out inside a micro tile: met3 right next to a pin and at the pin edge passes."""
    frozen = mc.frozen_dir(PDK)
    shutil.copy(os.path.join(frozen, f"{mc.FILLER}.lef"), tmp_path)
    layout = pya.Layout()
    layout.read(os.path.join(frozen, f"{mc.FILLER}.oas"))
    cell = layout.cell(mc.FILLER)
    met3 = layout.layer(70, 20)
    cell.shapes(met3).insert(pya.DBox(65.0, 3.51, 70.38, 5.0))
    cell.shapes(met3).insert(pya.DBox(60.0, 0.0, 70.38, 2.61))
    layout.write(os.path.join(tmp_path, f"{mc.FILLER}.oas"))
    assert mc.check_micro_tile(PDK, str(tmp_path), mc.FILLER) == []


def test_interface_check_rejects_missing_pins_and_shapes_outside(tmp_path):
    frozen = mc.frozen_dir(PDK)
    shutil.copy(os.path.join(frozen, f"{mc.FILLER}.lef"), tmp_path)
    layout = pya.Layout()
    layout.read(os.path.join(frozen, f"{mc.FILLER}.oas"))
    cell = layout.cell(mc.FILLER)
    met3 = layout.layer(70, 20)
    cut = pya.Region(pya.DBox(70.0, 2.0, 70.38, 4.0).to_itype(layout.dbu))
    kept = pya.Region(cell.shapes(met3)) - cut
    cell.shapes(met3).clear()
    cell.shapes(met3).insert(kept)
    cell.shapes(layout.layer(69, 20)).insert(pya.DBox(70.0, 20.0, 70.5, 21.0))
    layout.write(os.path.join(tmp_path, f"{mc.FILLER}.oas"))
    errors = mc.check_micro_tile(PDK, str(tmp_path), mc.FILLER)
    assert len(errors) == 2
    assert errors[0].startswith("shapes on 69/20 outside the micro tile outline")
    assert errors[1].startswith("clk: met3 at (69.38, 2.91, 70.38, 3.21) (LEF)")


def test_filler_fits():
    assert mc.check_micro_tile(PDK, mc.frozen_dir(PDK), mc.FILLER) == []


def test_interface_check_rejects_nwell_at_the_seam_and_deep_nwell(tmp_path):
    frozen = mc.frozen_dir(PDK)
    shutil.copy(os.path.join(frozen, f"{mc.FILLER}.lef"), tmp_path)
    layout = pya.Layout()
    layout.read(os.path.join(frozen, f"{mc.FILLER}.oas"))
    cell = layout.cell(mc.FILLER)
    # nwell at the pin edge is fine (the container's own nwell is 1.65 um away)
    cell.shapes(layout.layer(64, 20)).insert(pya.DBox(69.0, 20.0, 70.38, 22.0))
    layout.write(os.path.join(tmp_path, f"{mc.FILLER}.oas"))
    assert mc.check_micro_tile(PDK, str(tmp_path), mc.FILLER) == []
    cell.shapes(layout.layer(64, 20)).insert(pya.DBox(10.0, 51.0, 12.0, 51.68))
    cell.shapes(layout.layer(64, 18)).insert(pya.DBox(20.0, 20.0, 24.0, 24.0))
    layout.write(os.path.join(tmp_path, f"{mc.FILLER}.oas"))
    errors = mc.check_micro_tile(PDK, str(tmp_path), mc.FILLER)
    assert len(errors) == 2
    assert errors[0].startswith("shapes on dnwell")
    assert errors[1].startswith("nwell within 0.435 um of the top or bottom edge")


def test_interface_check_rejects_moved_pins(tmp_path):
    frozen = mc.frozen_dir(PDK)
    with open(os.path.join(frozen, f"{mc.FILLER}.lef")) as f:
        lef = f.read()
    with open(os.path.join(tmp_path, f"{mc.FILLER}.lef"), "w") as f:
        f.write(lef.replace("RECT 69.380 2.910", "RECT 69.380 2.920", 1))
    os.symlink(
        os.path.join(frozen, f"{mc.FILLER}.oas"),
        os.path.join(tmp_path, f"{mc.FILLER}.oas"),
    )
    assert "RECT 69.380 2.910" in lef
    errors = mc.check_micro_tile(PDK, str(tmp_path), mc.FILLER)
    assert errors[0] == (
        "pins or power stripes are not at the micro tile template positions"
    )


def test_assemble_fillers(tmp_path):
    top = "tt_um_micro_tiles_group_9"
    slots = mc.assemble(PDK, top, str(tmp_path), [None] * 4)
    assert slots == [mc.FILLER] * 4
    layout = pya.Layout()
    layout.read(os.path.join(tmp_path, f"{top}.oas"))
    assert layout.cell(mc.SLOT) is None and layout.cell(mc.GENERIC) is None
    top_cell = layout.cell(top)
    assert sum(1 for i in top_cell.each_inst() if i.cell.name == mc.FILLER) == 4
    # Two via3 per jumper, one jumper per signal pin, in the container's top cell
    via3 = top_cell.shapes(layout.find_layer(70, 44))
    pin_vias = [
        s for s in via3.each() if s.box.width() == 200 and s.box.height() == 200
    ]
    assert len(pin_vias) >= 4 * 18 * 2
    # The container's own met3 in a slot: the pins (jumpers) and the power vias
    (width, height), interface = mc.template_interface(PDK)
    filler = layout.cell(mc.FILLER)
    met3 = own_region(layout, top_cell, (70, 20), [filler])
    for inst in [i for i in top_cell.each_inst() if i.cell.name == mc.FILLER]:
        trans = inst.cplx_trans
        tile = pya.Region(pya.DBox(0, 0, width, height).to_itype(layout.dbu))
        expected = pya.Region()
        for _, _, r in interface:
            expected.insert(pya.DBox(*r).to_itype(layout.dbu))
        assert (
            (met3 & tile.transformed(trans)) - expected.transformed(trans)
        ).is_empty()
    with open(os.path.join(tmp_path, f"{top}.v")) as f:
        netlist = f.read()
    assert re.findall(rf"^ {mc.FILLER} proj\d", netlist, re.M) == [
        f" {mc.FILLER} proj{i}" for i in range(1, 5)
    ]
    assert f"module {top} " in netlist
    with open(os.path.join(tmp_path, f"{top}.lef")) as f:
        assert f"MACRO {top}\n" in f.read()
    assert os.path.exists(os.path.join(tmp_path, f"{mc.FILLER}.v"))


def test_unsupported_pdk():
    with pytest.raises(mc.MicroTileError):
        mc.get_tech("gf180mcuD")
