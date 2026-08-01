from langchain_core.tools import tool
from ddgs import DDGS
from app.tools.registry import registry

@registry.register("youtube_search")
@tool
def youtube_search_tool(query: str) -> str:
    """
    Searches YouTube for videos related to the query and returns titles and links.
    Always use this tool if the user asks for a video, tutorial, or visual reference.
    """
    try:
        results = DDGS().videos(query, max_results=3)
        if not results:
            return "No YouTube videos found for this query."
        
        response = "Here are some relevant YouTube videos:\n"
        for idx, res in enumerate(results, 1):
            title = res.get('title', 'Unknown Title')
            link = res.get('content', '')
            if not link:
                link = res.get('url', 'No link available')
            response += f"{idx}. [{title}]({link})\n"
        return response
    except Exception as e:
        return f"Error searching YouTube: {str(e)}"

@registry.register("web_search")
@tool
def web_search_tool(query: str) -> str:
    """
    Searches the general web for information related to the query.
    Use this to find references, articles, and factual information.
    """
    try:
        results = DDGS().text(query, max_results=3)
        if not results:
            return "No search results found."
            
        response = "Here is what I found on the web:\n"
        for idx, res in enumerate(results, 1):
            title = res.get('title', 'Unknown Title')
            body = res.get('body', '')
            link = res.get('href', '')
            response += f"{idx}. **[{title}]({link})**\n   {body}\n"
        return response
    except Exception as e:
        return f"Error searching the web: {str(e)}"
