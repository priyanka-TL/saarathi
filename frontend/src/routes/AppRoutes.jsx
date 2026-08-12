import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

import RequireAuth from './RequireAuth.jsx';

// Code-split, though with a single screen this mostly buys a smaller initial
// chunk than it does route-level laziness.
const ChatPage = lazy(() => import('../pages/ChatPage.jsx'));
const LoginPage = lazy(() => import('../pages/LoginPage.jsx'));
// const RegisterPage = lazy(() => import('../pages/RegisterPage.jsx'));
// Registration is folded into LoginPage for now rather than a separate
// screen -- /register disabled the same way its link is in LoginPage.jsx.
// Re-enable both together.

/**
 * /login is public; everything else requires a logged-in ELEVATE session
 * (RequireAuth redirects to /login otherwise).
 *
 * The conversation id deliberately does NOT live in the URL: it is per-tab
 * state in sessionStorage, and putting it in the path would change reload and
 * new-tab semantics (a duplicated tab would share a conversation).
 */
export default function AppRoutes() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        {/* <Route path="/register" element={<RegisterPage />} /> */}
        <Route
          path="/"
          element={
            <RequireAuth>
              <ChatPage />
            </RequireAuth>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
