"""Compatibility entry point for legacy setuptools invocations.

Project metadata lives in ``pyproject.toml``.  Keeping this metadata-free
shim lets older tooling continue to run ``python setup.py`` without creating a
second source of truth.
"""

from setuptools import setup

setup()
