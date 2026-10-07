import os

import uvicorn

from ent_agent_sdk.observability import configure_logging
from invoke_router.app import ROUTER_PORT, create_app


def main() -> None:
    configure_logging("invoke-router")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("ROUTER_PORT", ROUTER_PORT)), log_config=None)


if __name__ == "__main__":
    main()
