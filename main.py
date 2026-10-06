import os

import uvicorn

from app.api import create_app
from app.db import connect, init_schema

DB_PATH = os.environ.get("CAMPAIGN_DB", "campaign.db")

_conn = connect(DB_PATH)
init_schema(_conn)
_conn.close()

app = create_app(DB_PATH)


@app.get("/")
def read_root():
    return {"service": "campaign-audience-engine", "status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=0)
