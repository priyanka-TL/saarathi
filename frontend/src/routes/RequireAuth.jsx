import { Navigate } from 'react-router-dom';

import { useAuth } from '../context/AuthContext.jsx';

/**
 * Gates a route behind login. Redirects to /login rather than rendering
 * anything of the protected page -- there is no partial/loading state to
 * show, since AuthContext resolves synchronously from localStorage.
 */
export default function RequireAuth({ children }) {
  const { isAuthenticated } = useAuth();
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return children;
}
