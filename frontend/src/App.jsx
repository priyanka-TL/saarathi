import ErrorBoundary from './components/common/ErrorBoundary.jsx';
import { ConversationProvider } from './context/ConversationContext.jsx';
import AppRoutes from './routes/AppRoutes.jsx';

export default function App() {
  return (
    <ErrorBoundary>
      <ConversationProvider>
        <AppRoutes />
      </ConversationProvider>
    </ErrorBoundary>
  );
}
