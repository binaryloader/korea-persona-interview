"""Bundled prompt templates available via ``importlib.resources``.

The canonical system prompt lives at the project root ``prompts/system_prompt.txt``
so users can edit the template freely. This package ships the same file as a
fallback for ``pip install`` users who do not have the project tree on disk.
``src.interview._load_system_prompt_template`` checks the configured path first
and reads from this package only when the configured file is missing.
"""
