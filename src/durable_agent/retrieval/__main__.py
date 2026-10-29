"""Build the governance index: ``python -m durable_agent.retrieval`` (``make index`` runs this).

A package entry point, so ``index`` is imported once. Running ``-m durable_agent.retrieval.index``
loads that module twice (once via this package's ``__init__``) and Python warns about it.
"""

from __future__ import annotations

import sys

from durable_agent.retrieval.index import main

if __name__ == "__main__":
    sys.exit(main())
