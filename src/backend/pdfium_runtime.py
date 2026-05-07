from __future__ import annotations

import threading


# PDFium has shown occasional native crashes on Windows when multiple request
# threads render/open documents concurrently. Keep all in-process PDFium work
# serialized.
PDFIUM_LOCK = threading.RLock()
