"""Phase 0 of the Kalshi bet router: read-only fill audit and sport classification.

This package can authenticate to a Kalshi account, read fills, resolve public
market metadata and classify each fill by sport.  It deliberately cannot write
anywhere: there is no downstream routing, no ledger, no persistence.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
