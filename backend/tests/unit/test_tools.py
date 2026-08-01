import pytest
from langchain_core.tools import BaseTool, tool
from app.tools.registry import ToolRegistry, UnknownToolError

def test_tool_registry_registration_and_resolve():
    registry = ToolRegistry()
    
    @registry.register("test_tool")
    @tool
    def my_tool(x: int) -> int:
        """My test tool."""
        return x
        
    # Resolve single known tool
    resolved = registry.resolve(["test_tool"])
    assert len(resolved) == 1
    assert isinstance(resolved[0], BaseTool)
    assert resolved[0].name == "my_tool"
    assert resolved[0].description == "My test tool."
    
    # Assert all known succeeds
    registry.assert_all_known(["test_tool"])

def test_tool_registry_unknown_raises():
    registry = ToolRegistry()
    
    with pytest.raises(UnknownToolError) as exc:
        registry.resolve(["non_existent"])
        
    assert exc.value.missing == ["non_existent"]
    assert "non_existent" in str(exc.value)

def test_tool_registry_duplicate_registration_raises():
    registry = ToolRegistry()
    
    @registry.register("dup")
    def tool1(x: int) -> int:
        """Tool 1."""
        return x
        
    with pytest.raises(RuntimeError) as exc:
        @registry.register("dup")
        def tool2(x: int) -> int:
            """Tool 2."""
            return x
            
    assert "duplicate tool 'dup'" in str(exc.value)

def test_tool_registry_catalogue():
    registry = ToolRegistry()
    
    @registry.register("b_tool")
    @tool
    def b_tool():
        """B description."""
        pass
        
    @registry.register("a_tool")
    @tool
    def a_tool():
        """A description."""
        pass
        
    cat = registry.catalogue()
    # Catalogue must be sorted by name
    assert cat == [
        {"name": "a_tool", "description": "A description."},
        {"name": "b_tool", "description": "B description."},
    ]
