"""CFR Complaint Intelligence V2."""

import os

# Behind a TLS-inspecting proxy, CFR_TRUSTSTORE=1 makes every client verify against
# the OS trust store. It must run before botocore/urllib3 are imported: they keep a
# reference to ssl.SSLContext, and injecting afterwards makes boto's TLS setup recurse
# forever. This package __init__ is the earliest hook for `python -m cfr_v2.*`.
if os.environ.get("CFR_TRUSTSTORE", "").lower() in ("1", "true", "yes"):
    import truststore

    truststore.inject_into_ssl()
