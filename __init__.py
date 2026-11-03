from gsuid_core.sv import Plugins

Plugins(
    name="GsOnebotFunPlugin",
    prefix=[],
    force_prefix=[],
    allow_empty_prefix=True,
)

from . import (  # noqa: E402
    ai,  # noqa: E402,F401
    zanwo,  # noqa: E402,F401
    zaoan_wanan,  # noqa: E402,F401
)
