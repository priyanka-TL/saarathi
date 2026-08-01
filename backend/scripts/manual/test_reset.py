import os
import uuid
os.environ["SAARTHI_PERSISTENCE"] = "postgres"
from app.core.runtime import create_app

app = create_app()
with app.test_client() as client:
    # Chat 1
    resp1 = client.post("/api/chat", json={"message": "hello", "agent_name": "General Support Agent"})
    print("Chat 1:", resp1.json)
    
    # Reset
    resp2 = client.post("/api/reset", json={})
    print("Reset:", resp2.json)
    
    # Chat 2
    resp3 = client.post("/api/chat", json={"message": "world", "agent_name": "Technical Support Agent"})
    print("Chat 2:", resp3.json)
