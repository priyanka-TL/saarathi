import ErrorBoundary from './components/common/ErrorBoundary.jsx';
import { AuthProvider } from './context/AuthContext.jsx';
import { ConversationProvider } from './context/ConversationContext.jsx';
import { ProfileProvider } from './context/ProfileContext.jsx';
import AppRoutes from './routes/AppRoutes.jsx';

export default function App() {
  return (
    <ErrorBoundary>
      <AuthProvider>
        {/*
          INSIDE AuthProvider, because it reads the token to decide whether to
          fetch at all -- and it only fetches when authenticated, which is what
          keeps /login free of a profile call.
        */}
        <ProfileProvider>
          <ConversationProvider>
            <AppRoutes />
          </ConversationProvider>
        </ProfileProvider>
      </AuthProvider>
    </ErrorBoundary>
  );
}
