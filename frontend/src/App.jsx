import ErrorBoundary from './components/common/ErrorBoundary.jsx';
import { AuthProvider } from './context/AuthContext.jsx';
import { ConversationProvider } from './context/ConversationContext.jsx';
import AppRoutes from './routes/AppRoutes.jsx';

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        <ConversationProvider>
          <AppRoutes />
        </ConversationProvider>
      </AuthProvider>
    </ErrorBoundary>
  );
}
