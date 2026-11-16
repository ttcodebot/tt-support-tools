"""Checks that make a micro tile safe to drop into a generic container without an antenna check.

The generic container reaches a micro tile's signal pins only with a via from the layer above
the pin layer (see micro_container.py). Its antenna result then holds for any micro tile that:

- has nothing above the pin layer (forbidden layers check),
- keeps the pin strip (the last STRIP_WIDTH um at the pin edge) empty on its routing layers,
  apart from the pins themselves: the container's via pads overhang the pins there and its
  wires use the strip below the pin layer,
- has, on every input that reaches a gate, at least the gate area the placeholder declares
  (the generic container was checked against that gate area).
"""

import logging
import re

import klayout.db as pya
from precheck_failure import PrecheckFailure

STRIP_WIDTH = 1.0
# Wire ends that land on a pin may be a little wider than the pin, and cell or wire edges may
# poke into the strip: allowed within EDGE_TOLERANCE of the strip's inner edge only. The
# container's via pads keep 0.3 um (met3 spacing) + EDGE_TOLERANCE from that edge.
EDGE_TOLERANCE = 0.05
# Routing layers of a micro tile (the last one carries the pins)
MICRO_ROUTING_LAYERS = {
    "sky130A": ["li1.drawing", "met1.drawing", "met2.drawing", "met3.drawing"],
    "gf180mcuD": ["Metal1", "Metal2", "Metal3"],
}
# Gate area the placeholder declares on every input: the smallest gate of the library
MICRO_MIN_INPUT_GATE = {"sky130A": 0.126, "gf180mcuD": 0.396}


def micro_container_interface_check(gds: str, lef: str, toplevel: str, tech: str, layers):
    if tech not in MICRO_ROUTING_LAYERS:
        raise PrecheckFailure(f"No micro tile container rules for {tech}")
    text = open(lef).read()
    macro = re.search(rf"MACRO {re.escape(toplevel)}\n(.*?)END {re.escape(toplevel)}\n", text, re.S)
    if macro is None:
        raise PrecheckFailure(f"Macro {toplevel} not found in {lef}")
    body = macro.group(1)
    width, height = map(float, re.search(r"SIZE (\S+) BY (\S+) ;", body).groups())

    # Signal pins and their gate area
    pin_layer = MICRO_ROUTING_LAYERS[tech][-1].split(".")[0]
    pins = []
    errors = []
    for name, pin in re.findall(r"  PIN (\S+)\n(.*?)  END \1\n", body, re.S):
        if "USE SIGNAL" not in pin:
            continue
        for layer, r in re.findall(r"LAYER (\S+) ;\s+RECT ([\d.\s-]+?) ;", pin):
            if layer == pin_layer:
                pins.append(pya.DBox(*map(float, r.split())))
        gate = sum(float(v) for v in re.findall(r"ANTENNAGATEAREA ([\d.]+)", pin))
        if "DIRECTION INPUT" in pin and 0 < gate < MICRO_MIN_INPUT_GATE[tech] - 1e-6:
            errors.append(f"input {name} drives {gate} um2 of gate, less than {MICRO_MIN_INPUT_GATE[tech]}")

    # Pin strip
    layout = pya.Layout()
    layout.read(gds)
    top = layout.cell(toplevel)
    strip = pya.Region(pya.DBox(width - STRIP_WIDTH + EDGE_TOLERANCE, 0, width, height).to_itype(layout.dbu))
    pin_region = pya.Region()
    for box in pins:
        pin_region.insert(box.to_itype(layout.dbu))
    for layer_name in MICRO_ROUTING_LAYERS[tech]:
        info = layers[layer_name]
        index = layout.find_layer(info.layer, info.data_type)
        if index is None:
            continue
        region = pya.Region(top.begin_shapes_rec(index)).merged() & strip
        if layer_name == MICRO_ROUTING_LAYERS[tech][-1]:
            region -= pin_region
        area = region.area() * layout.dbu**2
        logging.info(f"* {layer_name}: {area:.4f} um2 in the pin strip apart from the pins")
        if area > 1e-6:
            b = region.bbox().to_dtype(layout.dbu)
            errors.append(f"{layer_name} has {area:.3f} um2 in the pin strip (x > {width - STRIP_WIDTH:.2f}), around {b}")
    if errors:
        raise PrecheckFailure("Micro tile container interface: " + "; ".join(errors))
    return True
