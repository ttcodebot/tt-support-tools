import json
import os
from collections.abc import Iterable

import yaml


class ConfigFileError(Exception):
    pass


def read_json_config(file: str):
    config = json.load(open(file))
    config.pop("//", None)
    return config


def read_yaml_config(file: str):
    return yaml.safe_load(open(file))


def read_config(
    basename: str, formats: Iterable[str] = ("json",), design_dir: str | None = None
):
    for fmt in formats:
        file = f"{basename}.{fmt}"
        if os.path.exists(file):
            if fmt == "json":
                return read_json_config(file)
            elif fmt == "yaml":
                return read_yaml_config(file)
            else:
                raise ConfigFileError(f"Unexpected configuration file format: {fmt}")
    raise ConfigFileError(
        f"Could not file configuration file {basename}.{{{'|'.join(formats)}}}"
    )


def write_json_config(config: dict, file: str):
    with open(file, "w") as f:
        json.dump(config, f, indent=2)


def write_yaml_config(config: dict, file: str):
    with open(file, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)


def write_mk_config(config: dict, file: str):
    with open(file, "w") as f:
        for key, value in config.items():
            if type(value) in (list, tuple):
                value = " ".join(value)
            if type(value) is str:
                value = value.replace("dir::", "$(DESIGN_HOME)/")
            print(f"export {key} = {value}", file=f)


def write_config(config: dict, basename: str, formats: Iterable[str] = ("json",)):
    for fmt in formats:
        file = f"{basename}.{fmt}"
        if fmt == "json":
            write_json_config(config, file)
        elif fmt == "yaml":
            write_yaml_config(config, file)
        else:
            raise ConfigFileError("Unexpected configuration file format: {fmt}")


def merge_dicts(a: dict, b: dict, path=[]):
    for key in b:
        if key in a and isinstance(a[key], dict) and isinstance(b[key], dict):
            merge_dicts(a[key], b[key], path + [str(key)])
        else:
            a[key] = b[key]
    return a


# Deprecated names of the PDN_<name> variables that are not FP_PDN_<name>
_OTHER_DEPRECATED_NAMES = {
    "PDN_MULTILAYER": ["DESIGN_IS_CORE"],
    "PDN_VERTICAL_LAYER": ["FP_PDN_LOWER_LAYER"],
    "PDN_HORIZONTAL_LAYER": ["FP_PDN_UPPER_LAYER"],
}


def override_config(config: dict, overrides: dict):
    """
    Applies overrides to a LibreLane config. LibreLane reads the deprecated name of
    a variable in preference to its current name, so the deprecated names of the
    overriding keys are removed.
    """
    for key in overrides:
        if key.startswith("PDN_"):
            config.pop(f"FP_{key}", None)
        for name in _OTHER_DEPRECATED_NAMES.get(key, []):
            config.pop(name, None)
    config.update(overrides)
    return config
