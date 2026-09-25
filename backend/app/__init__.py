"""YouTube Downloader backend package.

The version lives here rather than in pyproject.toml because the Docker
image copies ``app/`` without installing the package, so there is no
distribution metadata to read at runtime. pyproject reads this value
back through setuptools' dynamic version, which keeps the two from
drifting apart.
"""

__version__ = "0.4.1"
