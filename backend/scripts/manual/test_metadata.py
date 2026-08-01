from app.models.orm import Conversation, ConversationMessage

for table in [Conversation.__table__, ConversationMessage.__table__]:
    print(f"--- {table.name} ---")
    for c in table.constraints:
        print(f"{type(c).__name__}: {c.name}")
    for idx in table.indexes:
        print(f"Index: {idx.name}")
