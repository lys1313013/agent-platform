import { initTheme } from '@/stores/themeStore';
import AntdProvider from '@/components/AntdProvider';

// Apply theme before React renders to avoid flash
initTheme();

import React from 'react';
import ReactDOM from 'react-dom/client';
import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import UnsavedChangesGuard from '@/components/UnsavedChangesGuard';
import App from './App';
import './styles/globals.css';

const router = createBrowserRouter([{ path: '*', element: (
  <AntdProvider><UnsavedChangesGuard /><App /></AntdProvider>
) }]);
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><RouterProvider router={router} /></React.StrictMode>,
);
