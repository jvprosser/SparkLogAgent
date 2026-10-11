"""stdio MCP server. Tool arguments never include the workload password."""

from __future__ import annotations

import logging
import sys

from mcp.server.mcpserver import MCPServer

from spark_logs.client import ClientError, gateway_status, get_application_logs, list_container_logs

logger = logging.getLogger("sparklogagent.mcp")


def create_server() -> MCPServer:
    server = MCPServer("spark-logs")

    @server.tool(name="gateway_status", structured_output=False)
    def gateway_status_tool() -> str:
        """Check the connection to the log utility and report git versions.

        Call this to verify the gateway is reachable before listing container
        logs. Returns this server's git commit, whether the utility accepted
        the request, and the utility's git commit.
        """
        return gateway_status()

    @server.tool(name="list_container_logs", structured_output=False)
    def list_container_logs_tool(application_id: str) -> str:
        """List container log files for a completed YARN application.

        application_id is the YARN id, for example application_1700000000000_0001.
        Call this before fetching log text so you can choose a container.
        """
        return _call(lambda: list_container_logs(application_id))

    @server.tool(name="get_application_logs", structured_output=False)
    def get_application_logs_tool(
        application_id: str,
        container_id: str | None = None,
        log_type: str | None = None,
        pattern: str | None = None,
        tail_lines: int = 200,
        app_owner: str | None = None,
    ) -> str:
        """Fetch decoded stdout or stderr for a completed Spark application.

        Use this after History Server tools show a failed or slow stage and you
        need the raw exception or warning. log_type is stdout or stderr.
        app_owner is the YARN user that submitted the application when that
        user is not the workflow principal. Returns a capped tail.
        """
        return _call(
            lambda: get_application_logs(
                application_id,
                container_id=container_id,
                log_type=log_type,
                pattern=pattern,
                tail_lines=tail_lines,
                app_owner=app_owner,
            )
        )

    return server


def _call(func: object) -> str:
    try:
        return func()  # type: ignore[operator]
    except ClientError as exc:
        logger.info("log tool failed: %s", exc)
        return str(exc)


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    create_server().run(transport="stdio")
