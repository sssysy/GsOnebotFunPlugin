from gsuid_core.sv import Plugins

Plugins(
    name="GsOnebotFunPlugin",
    prefix=[],
    force_prefix=[],
    allow_empty_prefix=True,
)

from . import ai, zanwo, config, zaoan_wanan, poke_feedback, iwara_subscribe  # noqa: E402,F401
