def create_mcp_server():
    from core.mcp_scheduler.server import create_mcp_server as _create_mcp_server

    return _create_mcp_server()


__all__ = ["create_mcp_server"]
