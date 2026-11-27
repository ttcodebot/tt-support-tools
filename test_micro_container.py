import json
import os
import re
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


def test_frozen_container_leaves_the_slots_to_the_micro_tiles():
    """No container metal over a slot on the micro tile's routing layers and no cuts onto
    them: the power vias and the jumpers are drawn at assembly. The container's power
    stripes run over every slot, clear of the pin strip."""
    t = mc.MICRO_CONTAINER_TECH[PDK]
    mc.verify_frozen(PDK)
    layout = pya.Layout()
    layout.read(os.path.join(mc.frozen_dir(PDK), f"{mc.GENERIC}.oas"))
    top = layout.cell(mc.GENERIC)
    (width, height), _ = mc.template_interface(PDK)
    slots = [i for i in top.each_inst() if i.cell.name == mc.SLOT]
    assert len(slots) == len(t.slots)
    cuts = pya.Region(top.shapes(layout.find_layer(*t.stack_gds[t.power_cut_layer])))
    stripe_metal = pya.Region(
        top.shapes(layout.find_layer(*t.stack_gds[t.stripe_layer]))
    )
    for inst in slots:
        trans = inst.dcplx_trans
        slot = pya.Region(
            pya.DBox(0, 0, width, height).transformed(trans).to_itype(layout.dbu)
        )
        for name in t.routing_layers:
            own = pya.Region(top.shapes(layout.find_layer(*t.routing_gds[name])))
            assert (own & slot).is_empty(), name
        assert (cuts & slot).is_empty()
        index = next(
            i
            for i in range(len(t.slots))
            if trans == pya.DCplxTrans(mc.slot_trans(PDK, i))
        )
        stripes = mc.slot_power_stripes(PDK, index)
        assert [len(stripes[net]) for net in mc.POWER_NETS] == [3, 3]
        for net in mc.POWER_NETS:
            for s in stripes[net]:
                box = pya.DBox(*s)
                assert box.right < width - t.strip_width
                drawn = pya.Region(box.transformed(trans).to_itype(layout.dbu))
                assert (drawn - stripe_metal).is_empty()


def test_filler_fits():
    assert mc.check_micro_tile(PDK, mc.frozen_dir(PDK), mc.FILLER) == []


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
    assert errors[0] == "signal pins are not at the micro tile template positions"


def edit_power_stripes(src_dir, macro, dst_dir, new_macro, edits):
    """A copy of micro tile `macro` named `new_macro`, with some of its met3 power stripes
    changed: `edits` maps the box of an existing stripe to a list of new boxes (none:
    removed, two: copied). The via2 cuts under a stripe move with it vertically when they
    stay on it. On the filler, whose met2 power stripes run the full height and whose only
    other met3 is its pins and the uo_out wires from x = 67.685, a stripe that ends at
    x = 67.0 or avoids those wires keeps it DRC and LVS clean."""
    layout = pya.Layout()
    layout.read(os.path.join(src_dir, f"{macro}.oas"))
    top = layout.cell(macro)
    drawing, pin, label = mc.MICRO_CONTAINER_TECH[PDK].layer_gds["met3"]
    via2 = layout.find_layer(69, 44)
    with open(os.path.join(src_dir, f"{macro}.lef")) as f:
        lef = f.read()
    for old, new_boxes in edits.items():
        old_box = pya.DBox(*old)
        for layer in (drawing, pin):
            shapes = layout.find_layer(*layer)
            found = [s for s in top.shapes(shapes).each() if s.dbbox() == old_box]
            assert len(found) == 1, (layer, old)
            top.shapes(shapes).erase(found[0])
            for b in new_boxes:
                top.shapes(shapes).insert(pya.DBox(*b))
        labels = layout.find_layer(*label)
        for s in list(top.shapes(labels).each()):
            if s.is_text() and old_box.contains(s.dtext.position()):
                text = s.dtext
                top.shapes(labels).erase(s)
                for b in new_boxes:
                    center = pya.DBox(*b).center()
                    top.shapes(labels).insert(
                        pya.DText(text.string, center.x, center.y)
                    )
        under = [
            s for s in top.shapes(via2).each() if old_box.contains(s.dbbox().center())
        ]
        for s in under:
            cut = s.dbbox()
            top.shapes(via2).erase(s)
            for b in new_boxes:
                moved = cut.moved(0, b[1] - old[1])
                inner = pya.DBox(*b).enlarged(-0.065, -0.065)
                if inner.contains(moved.p1) and inner.contains(moved.p2):
                    top.shapes(via2).insert(moved)
        old_rect = "RECT " + " ".join(f"{v:.3f}" for v in old) + " ;"
        assert lef.count(old_rect) == 1, old_rect
        new_rects = [
            "RECT " + " ".join(f"{v:.3f}" for v in b) + " ;" for b in new_boxes
        ]
        lef = lef.replace(old_rect, "\n      LAYER met3 ;\n        ".join(new_rects))
    top.name = new_macro
    os.makedirs(dst_dir, exist_ok=True)
    layout.write(os.path.join(dst_dir, f"{new_macro}.oas"))
    with open(os.path.join(dst_dir, f"{new_macro}.lef"), "w") as f:
        f.write(re.sub(rf"\b{macro}\b", new_macro, lef))
    with open(os.path.join(src_dir, f"{macro}.v")) as f:
        netlist = f.read()
    with open(os.path.join(dst_dir, f"{new_macro}.v"), "w") as f:
        f.write(re.sub(rf"\b{macro}\b", new_macro, netlist))
    return dst_dir, new_macro


# Power stripes of the filler: the tools' default micro tile power grid
VPWR_LOW = (2.76, 9.02, 67.62, 10.02)
VPWR_HIGH = (2.76, 30.78, 67.62, 31.78)
VGND_LOW = (2.76, 19.9, 67.62, 20.9)
VGND_HIGH = (2.76, 41.66, 67.62, 42.66)


def counts(vias):
    return {net: (len(v), sum(len(c) for _, c in v)) for net, v in vias.items()}


def test_power_vias_of_the_default_grid():
    """The default stripes get the vias LibreLane's PDN drew into the previous frozen
    container: 4x2 cuts on each of 6 crossings per net, in every slot."""
    with open(os.path.join(mc.frozen_dir(PDK), f"{mc.FILLER}.lef")) as f:
        shapes = mc.lef_power_shapes(f.read(), PDK)
    assert shapes == {"VPWR": [VPWR_LOW, VPWR_HIGH], "VGND": [VGND_LOW, VGND_HIGH]}
    assert mc.power_rule_errors(PDK, shapes) == []
    for index in range(4):
        vias = mc.power_vias(PDK, shapes, index)
        assert counts(vias) == {"VPWR": (6, 48), "VGND": (6, 48)}
    crossing, cuts = mc.power_vias(PDK, shapes, 0)["VPWR"][0]
    assert crossing == (9.32, 9.02, 10.92, 10.02)
    assert cuts[0] == (9.42, 9.22, 9.62, 9.42)
    assert cuts[-1] == (10.62, 9.62, 10.82, 9.82)


def test_via_array_fits_and_stays_on_grid():
    rule = mc.MICRO_CONTAINER_TECH[PDK].power_via
    for crossing in [(0, 0, 1600, 800), (3, 7, 1003, 1250), (0, 0, 799, 2000)]:
        box = pya.Box(*crossing)
        inner = box.enlarged(-100, -100)
        cuts = mc.via_array(rule, box)
        assert cuts
        for c in cuts:
            assert c.left % 5 == 0 and c.bottom % 5 == 0
            assert c.width() == c.height() == 200
            assert inner.contains(c.p1) and inner.contains(c.p2)
    assert mc.via_array(rule, pya.Box(0, 0, 1600, 350)) == []


def test_moved_extra_and_wide_stripes(tmp_path):
    """A VPWR stripe moved and a 1.6 um one added low down, a VGND stripe moved and made
    0.8 um wide: accepted, and connected where they cross the container's stripes."""
    tile_dir, macro = edit_power_stripes(
        mc.frozen_dir(PDK),
        mc.FILLER,
        str(tmp_path / "tile"),
        "tt_um_test_moved",
        {
            VPWR_LOW: [(2.76, 14.22, 67.62, 15.22), (2.76, 3.0, 67.62, 4.6)],
            VGND_HIGH: [(2.76, 46.0, 67.0, 46.8)],
        },
    )
    assert mc.check_micro_tile(PDK, tile_dir, macro) == []
    with open(os.path.join(tile_dir, f"{macro}.lef")) as f:
        shapes = mc.lef_power_shapes(f.read(), PDK)
    # 4x2 cuts per crossing of a 1 um or 0.8 um stripe, 4x4 for the 1.6 um one
    for index in range(4):
        vias = mc.power_vias(PDK, shapes, index)
        assert counts(vias) == {"VPWR": (9, 6 * 8 + 3 * 16), "VGND": (6, 48)}
    top = "tt_um_micro_tiles_group_8"
    out = tmp_path / "out"
    mc.assemble(PDK, top, str(out), [None, (tile_dir, macro), None, None])
    layout = pya.Layout()
    layout.read(str(out / f"{top}.oas"))
    cell = layout.cell(top)
    drawn = pya.Region(cell.shapes(layout.find_layer(70, 44)))
    trans = mc.slot_trans(PDK, 1)
    for crossings in mc.power_vias(PDK, shapes, 1).values():
        for _, cuts in crossings:
            for c in cuts:
                via = pya.Region(pya.DBox(*c).transformed(trans).to_itype(0.001))
                assert (via - drawn).is_empty()
    with open(out / f"{top}.v") as f:
        netlist = f.read()
    assert re.search(
        rf"^ {macro} proj2 \(\.VGND\(VGND\),\s+\.VPWR\(VPWR\),\s+\.clk", netlist, re.M
    )


def test_too_few_crossings_are_rejected(tmp_path):
    """VPWR stripes that reach only the first container VPWR stripe, and none when the
    micro tile is mirrored."""
    tile_dir, macro = edit_power_stripes(
        mc.frozen_dir(PDK),
        mc.FILLER,
        str(tmp_path),
        "tt_um_test_short",
        {
            VPWR_LOW: [(2.76, 9.02, 15.0, 10.02)],
            VPWR_HIGH: [(2.76, 30.78, 15.0, 31.78)],
        },
    )
    errors = mc.check_micro_tile(PDK, tile_dir, macro)
    assert len(errors) == 1
    assert errors[0].startswith(
        "VPWR connects to the container in 0 places in slot 1 (mirrored), at least 2 "
        "needed: the container's met4 VPWR stripes cross the micro tile at "
        "x = 61.76-63.36, 39.68-41.28, 17.60-19.20 um there"
    )
    with pytest.raises(mc.MicroTileError, match="VPWR does not fit the power rule"):
        mc.assemble(
            PDK,
            "tt_um_x",
            str(tmp_path / "out"),
            [None, (tile_dir, macro)] + [None] * 2,
        )


def filler_with_lef(tmp_path, replacements):
    """The filler with some text of its LEF replaced."""
    frozen = mc.frozen_dir(PDK)
    with open(os.path.join(frozen, f"{mc.FILLER}.lef")) as f:
        lef = f.read()
    for old, new in replacements:
        assert old in lef
        lef = lef.replace(old, new)
    with open(os.path.join(tmp_path, f"{mc.FILLER}.lef"), "w") as f:
        f.write(lef)
    os.symlink(
        os.path.join(frozen, f"{mc.FILLER}.oas"),
        os.path.join(tmp_path, f"{mc.FILLER}.oas"),
    )
    return str(tmp_path)


def test_lef_power_port_without_metal_is_rejected(tmp_path):
    tile_dir = filler_with_lef(
        tmp_path,
        [("RECT 2.760 41.660 67.620 42.660", "RECT 2.760 44.660 67.620 45.660")],
    )
    assert mc.check_micro_tile(PDK, tile_dir, mc.FILLER) == [
        "VGND port (2.76, 44.66, 67.62, 45.66) on met3 in the LEF is not all met3 in the layout"
    ]


def test_lef_power_port_on_the_other_net_is_rejected(tmp_path):
    """LVS of the container would catch the VPWR vias on VGND metal, but not VGND vias on
    metal that is not connected to VGND: Magic joins the grounds through the substrate.
    """
    vpwr, vgnd = "RECT 2.760 9.020 67.620 10.020", "RECT 2.760 19.900 67.620 20.900"
    tile_dir = filler_with_lef(tmp_path, [(vpwr, "@"), (vgnd, vpwr), ("@", vgnd)])
    assert mc.check_micro_tile(PDK, tile_dir, mc.FILLER) == [
        "VPWR port (2.76, 19.9, 67.62, 20.9) on met3 in the LEF is on met3 labeled VGND in the layout",
        "VGND port (2.76, 9.02, 67.62, 10.02) on met3 in the LEF is on met3 labeled VPWR in the layout",
    ]


def test_assemble_fillers(tmp_path):
    top = "tt_um_micro_tiles_group_9"
    slots = mc.assemble(PDK, top, str(tmp_path), [None] * 4)
    assert slots == [mc.FILLER] * 4
    layout = pya.Layout()
    layout.read(os.path.join(tmp_path, f"{top}.oas"))
    assert layout.cell(mc.SLOT) is None and layout.cell(mc.GENERIC) is None
    top_cell = layout.cell(top)
    assert sum(1 for i in top_cell.each_inst() if i.cell.name == mc.FILLER) == 4
    # In the container's top cell, over the slots: two via3 per jumper, one jumper per signal
    # pin, and the power vias (6 crossings of 4x2 cuts per net)
    via3 = pya.Region(top_cell.shapes(layout.find_layer(70, 44)))
    slots = pya.Region()
    for index in range(4):
        box = pya.DBox(0, 0, 70.38, 51.68).transformed(mc.slot_trans(PDK, index))
        slots.insert(box.to_itype(layout.dbu))
    assert (via3 & slots).count() == 4 * (18 * 2 + 2 * 6 * 8)
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
