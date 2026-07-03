import logging
import sys

logging.basicConfig(format=u'%(filename)s [LINE:%(lineno)d] #%(levelname)-8s [%(asctime)s]  %(message)s',
                    level=logging.INFO,
                    # level=logging.DEBUG,
                    stream=sys.stdout,
                    )

# Third-party libraries flood INFO: httpx logs one line per HTTP request (a
# 200-frame PTI = 400+ upload/delete lines) and google_genai announces AFC on
# every call. Keep only their warnings/errors; our own INFO logs are unaffected.
for _noisy in ("httpx", "httpcore", "google_genai", "google_genai.models"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
