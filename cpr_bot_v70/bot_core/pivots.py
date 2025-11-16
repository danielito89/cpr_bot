import logging
from decimal import Decimal, ROUND_DOWN

def calculate_pivots_from_data(h, l, c, tick_size, cpr_width_threshold=0.2):
    """
    Calcula los pivotes Camarilla + CPR a partir de los datos HLC del día anterior.
    Devuelve un diccionario de pivotes.
    """
    try:
        if l == 0:
            raise Exception("El Low (L) del día anterior es cero.")

        piv = (h + l + c) / 3.0
        rng = h - l
        r4 = c + (h - l) * 1.1 / 2
        r3 = c + (h - l) * 1.1 / 4
        r2 = c + (h - l) * 1.1 / 6
        r1 = c + (h - l) * 1.1 / 12
        s1 = c - (h - l) * 1.1 / 12
        s2 = c - (h - l) * 1.1 / 6
        s3 = c - (h - l) * 1.1 / 4
        s4 = c - (h - l) * 1.1 / 2
        r5 = (h / l) * c
        r6 = r5 + 1.168 * (r5 - r4)
        s5 = c - (r5 - c)
        s6 = c - (r6 - c)
        bc = (h + l) / 2.0
        tc = (piv - bc) + piv
        cw = abs(tc - bc) / piv * 100 if piv != 0 else 0

        lvls = {
            "P": piv, "BC": bc, "TC": tc, "width": cw, 
            "is_ranging_day": cw > cpr_width_threshold,
            "H1": r1, "H2": r2, "H3": r3, "H4": r4, "H5": r5, "H6": r6,
            "L1": s1, "L2": s2, "L3": s3, "L4": s4, "L5": s5, "L6": s6,
            "Y_H": h, "Y_L": l, "Y_C": c,
        }

        # Cuantizar los niveles
        quantized_pivots = {}
        for k, v in lvls.items():
            if k not in ("width", "is_ranging_day"):
                try:
                    if isinstance(v, (int, float)) and tick_size:
                        quantized_pivots[k] = float(Decimal(str(v)).quantize(Decimal(str(tick_size)), rounding=ROUND_DOWN))
                    else:
                        quantized_pivots[k] = v
                except Exception:
                    quantized_pivots[k] = float(v)
            else:
                quantized_pivots[k] = v

        return quantized_pivots

    except Exception as e:
        logging.error(f"Error en calculate_pivots_from_data: {e}")
        return None
