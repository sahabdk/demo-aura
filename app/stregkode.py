"""Stregkode-afkodning: find EAN/stregkode i et Telegram-foto (zxing-cpp)."""
import io


def find_stregkode(billede_bytes):
    """Returnerer stregkodens indhold (fx EAN-13-nummer) eller None hvis ingen findes.
    zxing-cpp klarer selv rotation, skæve vinkler og de fleste stregkode-formater."""
    from PIL import Image
    import zxingcpp
    img = Image.open(io.BytesIO(billede_bytes)).convert("L")
    for r in zxingcpp.read_barcodes(img):
        if r.text:
            return r.text
    return None
