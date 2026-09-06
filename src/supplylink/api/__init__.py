"""HTTP API.

The application object lives in :mod:`supplylink.api.routes` rather than in a
module called ``app``: ``supplylink.api.app`` would resolve both to a module and
to the FastAPI instance re-exported here, which breaks attribute patching and
confuses anyone reading an import.
"""

from supplylink.api.routes import app

__all__ = ["app"]
