import logging
from pathlib import Path

from freqtrade.configuration.detect_environment import running_in_docker
from freqtrade.constants import (
    USERPATH_STRATEGIES,
    Config,
)
from freqtrade.exceptions import OperationalException


logger = logging.getLogger(__name__)


def create_datadir(config: Config, datadir: str | None = None) -> Path:
    folder = Path(datadir) if datadir else Path(f"{config['user_data_dir']}/data")
    if not datadir:
        # set datadir
        exchange_name = config.get("exchange", {}).get("name", "").lower()
        folder = folder.joinpath(exchange_name)

    if not folder.is_dir():
        folder.mkdir(parents=True)
        logger.info(f"Created data directory: {datadir}")
    return folder


def chown_user_directory(directory: Path) -> None:
    """
    Use Sudo to change permissions of the home-directory if necessary
    Only applies when running in docker!
    """
    if running_in_docker():
        try:
            import subprocess  # noqa: S404, RUF100

            subprocess.check_output(["sudo", "chown", "-R", "ftuser:", str(directory.resolve())])
        except Exception:
            logger.warning(f"Could not chown {directory}")


def create_userdata_dir(directory: str, create_dir: bool = False) -> Path:
    """
    Create userdata directory structure.
    if create_dir is True, then the parent-directory will be created if it does not exist.
    Sub-directories will always be created if the parent directory exists.
    Raises OperationalException if given a non-existing directory.
    :param directory: Directory to check
    :param create_dir: Create directory if it does not exist.
    :return: Path object containing the directory
    """
    sub_dirs = [
        "backtest_results",
        "data",
        "logs",
        USERPATH_STRATEGIES,
    ]
    folder = Path(directory)
    chown_user_directory(folder)
    if not folder.is_dir():
        if create_dir:
            folder.mkdir(parents=True)
            logger.info(f"Created user-data directory: {folder}")
        else:
            raise OperationalException(
                f"Directory `{folder}` does not exist. "
                "Create the directory manually or point the config to an existing user_data_dir."
            )

    # Create required subdirectories
    for f in sub_dirs:
        subfolder = folder / f
        if not subfolder.is_dir():
            if subfolder.exists() or subfolder.is_symlink():
                raise OperationalException(
                    f"File `{subfolder}` exists already and is not a directory. "
                    "Freqtrade requires this to be a directory."
                )
            subfolder.mkdir(parents=False)
    return folder
