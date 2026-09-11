"""C9-05: measuring Continuity deterministically.

`02_ARCHITECTURE.md` §18 is explicit that Continuity is not evaluated by asking
a model how good a migration was. Every metric here is arithmetic over two
things: what a labelled fixture says should have happened, and what Continuity's
own stored records say did.

The pieces:

* `labels.py` — the labelled case format, and loading a set of them.
* `fixtures.py` — turning a case into a real git checkout and a real provider
  adapter, so the code under measurement is the production code.
* `harness.py` — running one case through that production code and reading back
  what it recorded.
* `metrics.py` — the §18 metric catalogue, computed from those observations.
* `report.py` — rendering it, including the Integration Health formula.

Run it with `uv run python -m backend.evaluation`.
"""
