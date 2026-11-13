from gsuid_core.sv import Plugins

Plugins(
    name="GsOnebotFunPlugin",
    prefix=[],
    force_prefix=[],
    allow_empty_prefix=True,
)

from . import ai, poke_feedback, zanwo, zaoan_wanan  # noqa: E402,F401
