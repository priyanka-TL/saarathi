from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

def test():
    # Use standard local PG connection, modify if saarathi-poc uses something else
    # Let's import from app.core.runtime or just read from os.environ
    import os
    db_url = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
    # Actually wait, maybe docker-compose creates the DB
    # Let's just use docker compose exec db psql for testing or rely on the alembic config
    
    # Or we can just let uv run pytest tests run the DB tests, but we don't have tests for this
    pass

if __name__ == "__main__":
    from dotenv import load_dotenv
    import os
    load_dotenv()
    db_url = os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/saarthi")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    session.execute(text("DELETE FROM agent_configurations"))
    session.execute(text("DELETE FROM agents"))
    session.commit()

    try:
        session.execute(text("INSERT INTO agents (key, name, description, agent_type, is_default) VALUES ('a1', 'A1', 'D1', 'llm', true)"))
        session.execute(text("INSERT INTO agents (key, name, description, agent_type, is_default) VALUES ('a2', 'A2', 'D2', 'llm', true)"))
        session.commit()
        print("FAILED: Allowed two default agents")
    except Exception as e:
        session.rollback()
        print(f"SUCCESS: Rejected two default agents")

    res = session.execute(text("INSERT INTO agents (key, name, description, agent_type, is_default) VALUES ('a1', 'A1', 'D1', 'llm', true) RETURNING id"))
    agent_id = res.scalar()
    session.commit()

    try:
        cfg = '{"agent_type": "llm", "key": "a1", "name": "A1", "prompt": "hi", "model": {"name": "test"}}'
        session.execute(text(f"INSERT INTO agent_configurations (agent_id, version, source, checksum, config, is_active) VALUES ('{agent_id}', 1, 'yaml', 'c1', '{cfg}', true)"))
        session.execute(text(f"INSERT INTO agent_configurations (agent_id, version, source, checksum, config, is_active) VALUES ('{agent_id}', 2, 'yaml', 'c2', '{cfg}', true)"))
        session.commit()
        print("FAILED: Allowed two active configs")
    except Exception as e:
        session.rollback()
        print(f"SUCCESS: Rejected two active configs")

    from app.services.agent_registry import AgentRegistry
    registry = AgentRegistry(ttl_s=60.0)
    registry.reload(session)
    registry.maybe_reload(session)
    
    print("DONE")
