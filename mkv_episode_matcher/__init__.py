"""MKV Episode Matcher package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("mkv-episode-matcher")
except PackageNotFoundError:
    # An unpackaged source tree has no trustworthy release identity.
    __version__ = "0+unknown"
