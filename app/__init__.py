import os

__version__ = "0.14.0"
# Nightly images say which build they are, e.g. 1.2.3+nightly.20261121.ab12cd3
if os.environ.get("APP_BUILD"):
    __version__ += "+" + os.environ["APP_BUILD"]
