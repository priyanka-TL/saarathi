import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

// Config is a function so loadEnv can read .env here -- import.meta.env does
// not exist in this file. Nothing below is a literal; see .env / .env.example.
export default defineConfig(({ mode }) => {
  // '' as the third argument would load EVERY variable, including secrets from
  // the shell. Keep the APPLICATION_ prefix so only client-safe values are read.
  const env = loadEnv(mode, process.cwd(), 'APPLICATION_');

  return {
    plugins: [react()],
    // Vite only exposes VITE_-prefixed vars to client code by default;
    // envPrefix is what actually widens import.meta.env, independent of the
    // prefix passed to loadEnv() above (that one only affects this file).
    envPrefix: 'APPLICATION_',
    server: {
      // The backend's FRONTEND_ORIGINS must list this origin or every request
      // is blocked by CORS. See FRONTEND_ORIGINS in backend/.env.
      port: Number(env.APPLICATION_DEV_PORT) || 5173,
      strictPort: true,
    },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: './src/test/setup.js',
    },
  };
});
