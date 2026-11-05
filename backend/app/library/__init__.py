"""Library services that are shared between the Founder HTTP surface (app/routers/library.py)
and Life's own authorized, read-only access paths — kept out of the router so the exact same
authorization + integrity logic serves both callers (see app/library/originals.py)."""
