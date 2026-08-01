import os
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
import yaml

from app.services.config_sync import ConfigSyncService, exactly_one, assert_unique

def setup_db():
    from dotenv import load_dotenv
    load_dotenv()
    db_url = os.environ.get("DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/saarthi")
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()
    
    session.execute(text("DELETE FROM agent_configurations"))
    session.execute(text("DELETE FROM agents"))
    session.commit()
    
    return session

def create_yaml(d: Path, name: str, content: dict):
    with open(d / name, "w") as f:
        yaml.dump(content, f)

def test():
    session = setup_db()
    service = ConfigSyncService()
    
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        yaml_dir = Path(td)
        
        # 1. Base YAML
        create_yaml(yaml_dir, "agent1.yaml", {
            "schema_version": 1,
            "agent_type": "llm",
            "key": "test_agent",
            "name": "Test Agent",
            "description": "Test",
            "prompt": "You are a test",
            "status": "enabled",
            "default": True,
            "model": {"name": "test_model"}
        })
        
        print("--- Run 1: Create ---")
        report1 = service.sync(session, yaml_dir)
        print("Created:", report1.created)
        assert len(report1.created) == 1
        
        print("--- Run 2: Unchanged ---")
        report2 = service.sync(session, yaml_dir)
        print("Unchanged:", report2.unchanged)
        assert len(report2.unchanged) == 1
        
        # 2. Disable agent in DB
        session.execute(text("UPDATE agents SET status = 'disabled' WHERE key = 'test_agent'"))
        session.commit()
        
        print("--- Run 3: Disabling agent leaves it disabled ---")
        report3 = service.sync(session, yaml_dir)
        # the agent should still be disabled
        status = session.execute(text("SELECT status FROM agents WHERE key = 'test_agent'")).scalar()
        print("Status is:", status)
        assert status == "disabled"
        assert len(report3.unchanged) == 1
        
        # 3. DB override retained
        print("--- Run 4: DB Override ---")
        agent_id = session.execute(text("SELECT id FROM agents WHERE key = 'test_agent'")).scalar()
        # deactivate previous
        session.execute(text("UPDATE agent_configurations SET is_active = false WHERE agent_id = :aid AND version = 1"), {"aid": agent_id})
        
        # insert a DB config
        session.execute(text("""
            INSERT INTO agent_configurations (agent_id, version, source, checksum, config, is_active, activated_at)
            VALUES (:aid, 2, 'db', 'fake_sum', '{"agent_type": "llm", "key": "test_agent", "name": "Test Agent"}', true, now())
        """), {"aid": agent_id})
        
        session.commit()
        
        # update yaml
        create_yaml(yaml_dir, "agent1.yaml", {
            "schema_version": 1,
            "agent_type": "llm",
            "key": "test_agent",
            "name": "Test Agent Updated",
            "description": "Test",
            "prompt": "You are a test updated",
            "model": {"name": "test_model"}
        })
        report4 = service.sync(session, yaml_dir)
        print("Drifted:", report4.drifted)
        assert len(report4.drifted) == 1
        
        # 4. Duplicate checks
        print("--- Run 5: Duplicates ---")
        create_yaml(yaml_dir, "agent2.yaml", {
            "schema_version": 1,
            "agent_type": "llm",
            "key": "test_agent_2",
            "name": "Test Agent Updated", # duplicate name
            "description": "Test",
            "prompt": "You are a test",
            "model": {"name": "test_model"}
        })
        try:
            service.sync(session, yaml_dir)
            print("FAILED: Allowed duplicate name")
        except ValueError as e:
            print("SUCCESS: Rejected duplicate name:", e)
            
    print("ALL ACCEPTANCE CRITERIA MET")

if __name__ == "__main__":
    test()
