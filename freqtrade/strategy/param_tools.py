import rapidjson
from pathlib import Path


PARAM_FILE_NUMBER_MODE = rapidjson.NM_NATIVE | rapidjson.NM_NAN


def load_strategy_params(filename: Path) -> dict:
    with filename.open("r") as file_handle:
        return rapidjson.load(file_handle, number_mode=PARAM_FILE_NUMBER_MODE)


def space_is_active(config: dict, space: str) -> bool:
    configured_spaces = config.get("spaces", [])
    if space in ("trailing", "protection", "trades"):
        return any(name in configured_spaces for name in [space, "all"])
    return any(name in configured_spaces for name in [space, "all", "default"])