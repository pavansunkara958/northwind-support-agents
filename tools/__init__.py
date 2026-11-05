"""Tool package.

Importing this package registers every tool. That matters more than it looks:
`registry.invoke` returns `unknown_tool` for anything not registered, so a
caller that imported only `tools.registry` would get a plausible-looking
"no tool named refund_order" instead of an import error — a silent, confusing
failure that depends on import order.

Registration happens here so there is exactly one place it can go wrong.
"""
from tools import registry  # noqa: F401  (must be first: the others import it)
from tools import backends  # noqa: F401
from tools import knowledge_tools, order_tools, returns_tools, warranty_tools  # noqa: F401,E501

__all__ = ["registry", "backends", "knowledge_tools", "order_tools",
           "returns_tools", "warranty_tools"]
