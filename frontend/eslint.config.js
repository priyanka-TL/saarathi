import js from '@eslint/js';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';

// Only the identifiers this codebase actually references (verified via grep)
// -- avoids pulling in the `globals` package as an extra devDependency for a
// handful of well-known browser/DOM/timer names.
const browserGlobals = {
  window: 'readonly',
  document: 'readonly',
  console: 'readonly',
  navigator: 'readonly',
  fetch: 'readonly',
  URL: 'readonly',
  Event: 'readonly',
  setInterval: 'readonly',
  clearInterval: 'readonly',
  setTimeout: 'readonly',
  clearTimeout: 'readonly',
  sessionStorage: 'readonly',
  localStorage: 'readonly',
  globalThis: 'readonly',
  // Voice. MediaRecorder and the AudioContext behind computeRms are reached
  // through `window.` so they need no entry, but these three are referenced
  // bare: `new Blob(...)`, `new Audio(...)`, `MediaRecorder.isTypeSupported`.
  Blob: 'readonly',
  Audio: 'readonly',
  MediaRecorder: 'readonly',
};

export default [
  { ignores: ['dist/**', 'node_modules/**'] },
  js.configs.recommended,
  react.configs.flat.recommended,
  react.configs.flat['jsx-runtime'], // React 19's automatic JSX runtime -- no `import React` needed
  reactHooks.configs['recommended-latest'],
  {
    settings: { react: { version: 'detect' } },
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: browserGlobals,
    },
    rules: {
      // This codebase has no `prop-types` dependency and doesn't use it
      // anywhere -- enabling this rule would only ever flag every existing
      // component, never something meant to be fixed.
      'react/prop-types': 'off',
    },
  },
  {
    // Node-context config files (not shipped to the browser).
    files: ['vite.config.js'],
    languageOptions: {
      globals: { ...browserGlobals, process: 'readonly' },
    },
  },
];
