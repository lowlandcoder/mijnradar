"""Generieke kleurschalen: waarden omzetten naar een RGBA-beeld.

Een kleurschaal is een lijst kleurstops in de vorm [(waarde, (r, g, b, a)), ...],
oplopend op waarde. Tussen twee stops wordt lineair gemengd. Waarden onder de
eerste stop blijven volledig doorzichtig.

De schaalverdeling is per laag verschillend. Neerslagintensiteit loopt over
enkele ordes van grootte en wordt daarom logaritmisch verdeeld; een grootheid
als de zonindex, die van 0 tot 1 loopt, is lineair.
"""
import numpy as np


def naar_rgba(waarden: np.ndarray, stops: list, log: bool = True) -> np.ndarray:
    """Zet een waardenraster om naar een RGBA-beeld volgens de kleurstops."""
    beeld = np.zeros(waarden.shape + (4,), dtype=np.uint8)
    drempels = np.array([s[0] for s in stops], dtype=np.float64)
    kleuren = np.array([s[1] for s in stops], dtype=np.float64)
    binnen = waarden >= drempels[0]
    if not binnen.any():
        return beeld
    geknipt = np.clip(waarden[binnen], drempels[0], drempels[-1])
    posities = np.arange(len(drempels), dtype=np.float64)
    if log:
        positie = np.interp(np.log10(geknipt), np.log10(drempels), posities)
    else:
        positie = np.interp(geknipt, drempels, posities)
    onder = np.floor(positie).astype(int)
    boven = np.minimum(onder + 1, len(drempels) - 1)
    frac = (positie - onder)[:, None]
    beeld[binnen] = (kleuren[onder] * (1 - frac) + kleuren[boven] * frac).astype(np.uint8)
    return beeld


def hex_stops(stops: list) -> list[list]:
    """Kleurstops als [waarde, "#rrggbb"], voor de legenda in de browser.

    De pagina bouwt de legenda hiermee op, zodat de kleuren altijd overeenkomen
    met wat de server rendert. Vroeger stonden ze dubbel: in de renderer en nog
    eens vast in index.html.
    """
    return [[waarde, "#%02x%02x%02x" % tuple(kleur[:3])] for waarde, kleur in stops]
