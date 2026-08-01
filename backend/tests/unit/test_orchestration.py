import uuid
import pytest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services.orchestration import OrchestrationService, TurnInput
from app.agents.protocol import AgentTurn

# A mock for the agent spec to avoid deep instantiation
class MockSpec:
    agent_type = "llm"
    limits = None
    memory = Mock(max_messages=5)

    # FeaturesSpec, read at step 13. Missing, this test failed with an
    # AttributeError before ever reaching its own assertion, so the "no
    # transaction held during the handler" invariant it exists to protect was
    # not actually being checked at all.
    class Features:
        record_tool_executions = False
    features = Features()
    class Routing:
        pin_session = False
        direct_selectable = True
        # RoutingSpec always supplies these; the stub has to as well now that
        # Gate 1 checks for an exit command before honouring an explicit key.
        exit_keywords = ["/exit"]
    routing = Routing()

class MockAgent:
    id = uuid.uuid4()
    key = "mock_agent"
    spec = MockSpec()
    config_id = uuid.uuid4()
    checksum = "mock_checksum"

class MockHandler:
    def __init__(self, session):
        self.session = session
        self.transaction_was_active = True
        
    def handle(self, ctx):
        # The key assertion: the transaction MUST NOT be active during the handler call
        self.transaction_was_active = self.session.in_transaction()
        return AgentTurn(
            text="Mock response",
            terminal=False,
            latency_ms=100
        )

def test_no_transaction_held_during_handler():
    # 1. Setup in-memory DB and session
    engine = create_engine("sqlite:///:memory:")
    Session = sessionmaker(bind=engine)
    session = Session()
    
    # 2. Setup mocks
    mock_registry = Mock()
    mock_agent = MockAgent()
    mock_registry.get.return_value = mock_agent
    mock_registry.get_by_key_exact.return_value = mock_agent
    mock_registry.default.return_value = mock_agent
    mock_registry.routable.return_value = [mock_agent]
    
    mock_handler = MockHandler(session)
    mock_factory = Mock()
    mock_factory.build.return_value = mock_handler
    
    mock_llm_factory = Mock()
    
    # 3. Instantiate Service
    service = OrchestrationService(
        session=session,
        registry=mock_registry,
        handler_factory=mock_factory,
        llm_factory=mock_llm_factory
    )
    
    # We need to mock the repo calls that hit the DB since SQLite in memory has no tables
    with patch.object(service._conversations, 'get_or_create') as mock_goc, \
         patch.object(service._conversations, 'next_seq_for_update') as mock_nsfu, \
         patch.object(service._messages, 'insert') as mock_ins, \
         patch.object(service._messages, 'recent') as mock_rec, \
         patch.object(service._sessions, 'open_for') as mock_of, \
         patch.object(service._conversations, 'replace_placeholder_title'), \
         patch.object(service._conversations, 'set_placeholder_title'), \
         patch.object(service._conversations, 'touch') as mock_touch:
             
        # Configure mocks to return valid data types
        mock_conv = Mock(id=uuid.uuid4())
        mock_goc.return_value = mock_conv
        mock_nsfu.return_value = 1
        mock_ins.return_value = Mock(id=uuid.uuid4())
        mock_rec.return_value = []
        mock_of.return_value = None # no session
        
        # We must start a transaction to simulate normal Flask/SQLAlchemy request lifecycle
        session.begin()
        assert session.in_transaction() is True
        
        ctx_in = TurnInput(
            request_id="req_123",
            conversation_id=None,
            user=Mock(tenant_code="test", user_id="test", active_org_id="test"),
            text="hello",
            agent_key="mock_agent"
        )
        
        # 4. Execute
        result = service.handle_turn(ctx_in)
        
        # 5. Assert
        assert mock_handler.transaction_was_active is False, "Transaction was held open during handler execution!"
        
        # Verify the sequence was followed
        mock_goc.assert_called_once()
        mock_nsfu.assert_called()
        mock_ins.assert_called()
