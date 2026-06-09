"""Local drop-in extractor plugins.

Every ``*.py`` file in this directory whose name does **not** start with ``_``
is auto-imported at startup by :func:`document_search.extractors.load_plugins`.
A drop-in module registers one or more extractors via a module-level
``register(register_extractor)`` hook. See ``docs/PLUGINS.md`` for the full guide.
"""
