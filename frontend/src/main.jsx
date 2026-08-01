import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';

// Order matters: the verbatim stylesheet first, then the additive #root rule.
import './styles/style.css';
import './styles/root.css';

import App from './App.jsx';

/*
 * NOTE: deliberately NOT wrapped in <StrictMode>.
 *
 * StrictMode double-invokes effects in development. ChatPage's boot effect
 * fires loadAgents / loadRecentConversations / loadConversationHistory, and the
 * capability buttons call resetConversation() -- which POSTs /api/reset and
 * creates a conversation. Double-invocation would double-fire all of that and,
 * in the reset case, create two conversations per click.
 *
 * If StrictMode is wanted later, gate the boot effect on a didBootRef first.
 */
createRoot(document.getElementById('root')).render(
  <BrowserRouter>
    <App />
  </BrowserRouter>,
);
