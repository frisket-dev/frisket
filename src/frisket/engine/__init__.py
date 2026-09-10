"""Engine layer: the execution core.

Holds the project store, executor, runner, job queue and workers, projections,
recovery, the sandbox, subprocess worker entrypoints, the receipt index, and
code-identity stamping. The engine must not depend on the web/team layers
beyond the explicitly excluded legacy edges in the boundary configs, and must
not grow new dependencies on the features layer. Import members by submodule
path (``frisket.engine.store``, ...).
"""

__all__: list[str] = []
