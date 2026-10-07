import os

import uvicorn

from ent_agent_sdk.observability import configure_logging
from model_router.app import ROUTER_PORT, create_app


def main() -> None:
    configure_logging("model-router")
    port = int(os.environ.get("ROUTER_PORT", ROUTER_PORT))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_config=None)


if __name__ == "__main__":
    main()
