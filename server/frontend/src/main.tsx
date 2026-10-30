import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/geist'
import '@fontsource-variable/geist-mono'
import '@/styles/globals.css'
import App from '@/app/App'
import { AppErrorBoundary } from '@/components/AppErrorBoundary'
import { installGlobalErrorReporting } from '@/lib/errorReport'
import { ToastProvider, Toaster } from '@/components/ui/toast'
import { TooltipProvider } from '@/components/ui/tooltip'
import { DeveloperModeProvider } from '@/context/DeveloperModeContext'
import { ThemeProvider } from '@/context/ThemeContext'
import { PrefsProvider } from '@/context/PrefsContext'

// Uncaught errors, unhandled promise rejections and failed lazy chunks are sent to
// the owner-only Debug > Errors tab. Nothing reloads the page by itself.
installGlobalErrorReporting()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppErrorBoundary>
    <ThemeProvider>
      <PrefsProvider>
        <DeveloperModeProvider>
          <TooltipProvider>
            <ToastProvider>
              <App />
              <Toaster />
            </ToastProvider>
          </TooltipProvider>
        </DeveloperModeProvider>
      </PrefsProvider>
    </ThemeProvider>
    </AppErrorBoundary>
  </StrictMode>,
)
