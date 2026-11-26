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
    """No container metal over a slot on the micro tile's routing layers, apart from the
    power connection on the pin layer and nothing at all in the pin strip."""
    t = mc.MICRO_CONTAINER_TECH[PDK]
    mc.verify_frozen(PDK)
    layout = pya.Layout()
    layout.read(os.path.join(mc.frozen_dir(PDK), f"{mc.GENERIC}.oas"))
    top = layout.cell(mc.GENERIC)
    (width, height), _ = mc.template_interface(PDK)
    slots = [i for i in top.each_inst() if i.cell.name == mc.SLOT]
    assert len(slots) == len(t.slots)
    for inst in slots:
        trans = inst.dcplx_trans
        slot = pya.Region(
            pya.DBox(0, 0, width, height).transformed(trans).to_itype(layout.dbu)
        )
        strip = pya.Region(
            pya.DBox(width - t.strip_width, 0, width, height)
            .transformed(trans)
            .to_itype(layout.dbu)
        )
        for name in t.routing_layers:
            own = pya.Region(top.shapes(layout.find_layer(*t.routing_gds[name])))
            if name == t.routing_layers[-1]:
                assert (own & strip).is_empty()
            else:
                assert (own & slot).is_empty(), name


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
