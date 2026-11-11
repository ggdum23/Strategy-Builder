"""Designer-pack builder.

Not implemented. Named local runs use raw prices and real dates and do not
anonymize. A later phase writes chunked packs and keeps scale factors in an
operator sidecar that is never placed in a designer tree.
"""


def build_pack(*_args, **_kwargs):
    raise NotImplementedError(
        "Pack building is a later phase. Named runs do not anonymize."
    )
