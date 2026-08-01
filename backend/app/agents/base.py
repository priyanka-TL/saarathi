from abc import ABC, abstractmethod
from typing import List, Any
from app.llm import get_llm
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

class BaseAgent(ABC):
    """
    Abstract base class for all agents.
    Enforces a common interface for processing requests using LangChain.
    """
    
    def __init__(self, name: str, description: str, system_prompt: str = "", tools: List[Any] = None):
        self.name = name
        self.description = description
        self.system_prompt = system_prompt
        self.tools = tools or []
        
        # Initialize the LLM
        self.llm = get_llm()
        
        if self.tools:
            self.has_tools = True
            self.chain = None
        else:
            self.has_tools = False
            
            # -------------------------------------------------------------
            # LCEL (LangChain Expression Language) Pipeline
            # -------------------------------------------------------------
            # If the agent has no tools, we build a simple LCEL chain.
            # This pipeline takes a prompt, feeds it to the LLM, and uses
            # a StrOutputParser to convert the raw LLM output into a string.
            prompt = ChatPromptTemplate.from_messages([
                ("system", self.system_prompt),
                ("placeholder", "{history}"),
                ("human", "{request}")
            ])
            self.chain = prompt | self.llm | StrOutputParser()
        
    def process(self, request: str, history: list = None) -> str:
        """
        Process the user's request and return a response using the LCEL chain or custom tool executor.
        """
        try:
            if self.has_tools:
                from langchain_core.messages import ToolMessage
                
                # Initialize conversation history with system instructions
                messages = [SystemMessage(content=self.system_prompt)]
                
                # Append past conversation history (if any)
                if history:
                    for msg in history:
                        if msg.get("role") == "user":
                            messages.append(HumanMessage(content=msg.get("content", "")))
                        else:
                            messages.append(AIMessage(content=msg.get("content", "")))
                
                # Finally, append the new user request
                messages.append(HumanMessage(content=request))
                
                # Bind our custom tools (like web search) directly to the LLM
                llm_with_tools = self.llm.bind_tools(self.tools)
                
                # We use a loop to handle multi-step reasoning (e.g. LLM searches -> gets results -> searches again)
                max_iterations = 3
                last_tool_result = ""
                
                for _ in range(max_iterations):
                    # Step 1: Ask the LLM what to do
                    response = llm_with_tools.invoke(messages)
                    
                    # Step 2: If the LLM didn't call any tools, it means it generated a final answer!
                    if not response.tool_calls:
                        return response.content or "I couldn't generate a clear answer."
                    
                    # Step 3: The LLM asked to use a tool. Add its request to the history.
                    messages.append(response)
                    
                    # Step 4: Execute each tool requested by the LLM
                    for tool_call in response.tool_calls:
                        tool_func = next((t for t in self.tools if t.name == tool_call['name']), None)
                        if tool_func:
                            try:
                                # Run the actual Python function
                                result = tool_func.invoke(tool_call['args'])
                            except Exception as e:
                                result = f"Error: {str(e)}"
                            last_tool_result = str(result)
                            
                            # Append the tool's result back into the history so the LLM can read it
                            messages.append(ToolMessage(content=last_tool_result, tool_call_id=tool_call['id']))
                
                # Fallback: If we exceed max iterations and the LLM is stuck in an infinite tool-calling loop,
                # we force it to generate a final response, or we just dump the raw data to the user.
                final_response = llm_with_tools.invoke(messages)
                if not final_response.content:
                    return f"Here is the raw data I found:\n{last_tool_result}"
                    
                return final_response.content
            else:
                # ---------------------------------------------------------
                # Standard Execution (No Tools)
                # ---------------------------------------------------------
                # Convert existing simple history dicts to LangChain format tuples
                formatted_history = []
                if history:
                    for msg in history:
                        if msg.get("role") == "user":
                            formatted_history.append(("human", msg.get("content", "")))
                        else:
                            formatted_history.append(("ai", msg.get("content", "")))
                            
                return self.chain.invoke({
                    "request": request,
                    "history": formatted_history
                })
        except Exception as e:
            import traceback
            traceback.print_exc()
            return f"I encountered an error processing your request: {str(e)}"
