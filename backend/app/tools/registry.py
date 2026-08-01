from typing import List, Dict, Any
from langchain_core.tools import BaseTool, tool

class UnknownToolError(Exception):
    def __init__(self, missing: List[str]):
        super().__init__(f"Unknown tools: {', '.join(missing)}")
        self.missing = missing

class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, name: str):
        def deco(fn_or_tool):
            # If it's already a BaseTool (e.g. wrapped in @tool), use it as is, otherwise wrap it
            t = fn_or_tool if isinstance(fn_or_tool, BaseTool) else tool(fn_or_tool)
            if name in self._tools:
                raise RuntimeError(f"duplicate tool {name!r}")
            self._tools[name] = t
            # Return the original decorated function/tool so chained decorators work
            return fn_or_tool
        return deco

    def resolve(self, names: List[str]) -> List[BaseTool]:
        missing = [n for n in names if n not in self._tools]
        if missing:
            raise UnknownToolError(missing)        # startup-time, not request-time
        return [self._tools[n] for n in names]

    def assert_all_known(self, names: List[str]) -> None:
        self.resolve(names)

    def catalogue(self) -> List[Dict[str, Any]]:
        return [{"name": n, "description": t.description}
                for n, t in sorted(self._tools.items())]

registry = ToolRegistry()
