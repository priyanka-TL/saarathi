import { lazy, Suspense } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';

// Code-split, though with a single screen this mostly buys a smaller initial
// chunk than it does route-level laziness.
const ChatPage = lazy(() => import('../pages/ChatPage.jsx'));

/**
 * One real route.
 *
 * The conversation id deliberately does NOT live in the URL: it is per-tab
 * state in sessionStorage, and putting it in the path would change reload and
 * new-tab semantics (a duplicated tab would share a conversation).
 */
export default function AppRoutes() {
  return (
    <Suspense fallback={null}>
      <Routes>
        <Route path="/" element={<ChatPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
