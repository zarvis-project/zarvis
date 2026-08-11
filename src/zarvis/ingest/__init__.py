"""Ingest: observe, resolve, record. Nothing else.

Ingest is read-only against the product and writes only to `zarvis.signal`. It
does not score, draft, or contact anyone — those are separate stages so that a
bad read can never become a bad send.
"""
