"""Kleine hulpfuncties die door meerdere lagen worden gebruikt."""
import datetime as dt


def iso(t: dt.datetime) -> str:
    """Tijdstip als 2026-07-27T09:45:00Z, de vorm die frames.json gebruikt."""
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")
