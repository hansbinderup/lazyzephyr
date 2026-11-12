"""lazyzephyr: lazygit-style terminal UI for Zephyr."""

from importlib.metadata import PackageNotFoundError, version

# The version lives in pyproject.toml only (release-please bumps it there).
try:
    __version__ = version("lazyzephyr")
except PackageNotFoundError:  # run from a source tree that isn't installed
    __version__ = "0.1.0"
