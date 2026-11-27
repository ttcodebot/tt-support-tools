import os

import klayout.db as pya
import yaml

from project import SCRIPT_DIR, micro_routing_obstructions

MICRO_WIDTH, MICRO_HEIGHT = 70.38, 51.68


def region(boxes):
    result = pya.Region()
    for box in boxes:
        result.insert(pya.DBox(*box).to_itype(0.001))
    return result.merged()


class TestMicroRoutingObstructions:
    keepout = yaml.safe_load(
        open(os.path.join(SCRIPT_DIR, "tech/sky130A/micro_edge_keepout.yaml"))
    )
    # The signal pins of the micro tile template, on met3 at the right edge
    pins = [(69.38, 2.91 + 2.72 * i, 70.38, 3.21 + 2.72 * i) for i in range(18)]

    def obstructions(self, layer):
        boxes = []
        for entry in micro_routing_obstructions("sky130A"):
            name, *box = entry.split()
            if name == layer:
                boxes.append(tuple(map(float, box)))
        return region(boxes)

    def test_obstructions_cover_the_bands(self):
        for layer, (left, bottom, right, top) in self.keepout["bands"].items():
            bands = region(
                [
                    (0, 0, left, MICRO_HEIGHT),
                    (0, 0, MICRO_WIDTH, bottom),
                    (MICRO_WIDTH - right, 0, MICRO_WIDTH, MICRO_HEIGHT),
                    (0, MICRO_HEIGHT - top, MICRO_WIDTH, MICRO_HEIGHT),
                ]
            )
            if layer == "met3":
                gap = self.keepout["pin_obstruction_gap"]
                x = MICRO_WIDTH - right
                bands -= region(
                    [(x, y1 - gap, x2, y2 + gap) for _, y1, x2, y2 in self.pins]
                )
            assert (self.obstructions(layer) ^ bands).is_empty(), layer

    def test_pins_can_be_reached_on_met3(self):
        # The router keeps the met3 spacing (0.3 um) from the obstructions: a wire as tall
        # as a pin reaches it from the left, and nothing taller fits next to the pin
        spacing = 0.3
        obstructions = self.obstructions("met3")
        for x1, y1, x2, y2 in self.pins:
            wire = region([(60, y1, x2, y2)])
            assert (wire.sized(int(spacing * 1000) - 1) & obstructions).is_empty()
            taller = region([(x1, y1 - 0.005, x2, y2)])
            assert not (taller.sized(int(spacing * 1000) - 1) & obstructions).is_empty()

    def test_no_obstructions_without_bands(self):
        assert micro_routing_obstructions("gf180mcuD") == []
