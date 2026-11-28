from typing import List, Literal, NotRequired, TypedDict


class ShuttleIndexProject(TypedDict):
    macro: str
    address: int
    x: int
    y: int
    tiles: str
    repo: str
    commit: str
    analog_pins: NotRequired[tuple[int, ...]]
    # Micro tile containers ("group") and their micro tiles ("subtile"). The subtile
    # address is the micro tile's slot: the value of uio_in[1:0] that selects it.
    type: NotRequired[Literal["group", "subtile"]]
    subtile_addr: NotRequired[int]
    subtile_addr_bits: NotRequired[int]
    subtile_group: NotRequired[str]


class ShuttleIndexLayout(TypedDict):
    muxes: List[List[Literal["analog", "digital"]]]


class ShuttleIndex(TypedDict):
    """TypedDict for Tiny Tapeout's shuttle_index.json file."""

    name: str
    repo: str
    commit: str
    commit_date: int
    version: int
    layout: ShuttleIndexLayout
    projects: List[ShuttleIndexProject]
